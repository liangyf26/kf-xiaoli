"""Jev API决策引擎：经OpenRouter /api/alpha/decisions端点访问TypeSafe Jev结构化决策模型。

Jev是System One结构化决策模型（返回typed choice），不走chat/completions。
契约（实测确认）：POST {base}/alpha/decisions，body含model/state/questions；
question判别键为type（noul/choice/score），choice与score需instructions+criteria，
noul仅需instructions。score返回criteria分档索引值（0~档数-1）。
调用失败（超时/认证/网络/解析）时回退低置信度结果，保证任何输入都有输出。
"""
import time
from typing import Any, Dict

import httpx

from backend.config import settings
from backend.decision_layer.base import DecisionEngine, DecisionResult

INTENT_OPTIONS = (
    "price_inquiry",
    "product_comparison",
    "technical_support",
    "usage_guide",
    "troubleshooting",
    "purchase_process",
    "unclear",
)
EMOTION_OPTIONS = ("neutral", "positive", "urgent", "dissatisfied", "complaint_risk")

# score分档数：API限制最多10档；档索引∈[0, N-1]，归一化=score/(N-1)映射到0-1
SCORE_BANDS = 10


def _score_bands(descriptions: list) -> list:
    """生成score问题的分档criteria（恰好SCORE_BANDS档，均分0-100区间）。"""
    assert len(descriptions) == SCORE_BANDS, f"score分档描述必须{SCORE_BANDS}个"
    step = 100 // SCORE_BANDS
    bands = []
    for i, desc in enumerate(descriptions):
        low, high = i * step, (i + 1) * step
        band: Dict[str, Any] = {"description": desc}
        if i > 0:
            band["min"] = low
        if i < SCORE_BANDS - 1:
            band["max"] = high
        bands.append(band)
    return bands


CONFIDENCE_DESCRIPTIONS = [
    "完全无法判断", "几乎没有把握", "略有线索", "能猜测方向", "有一定把握",
    "把握过半", "比较有把握", "把握较大", "很确定", "非常确定",
]
COMPLEXITY_DESCRIPTIONS = [
    "寒暄或无技术内容", "简单产品咨询", "一般操作咨询", "使用步骤指导", "配置类问题",
    "功能异常排查", "网络质量问题", "多设备组网问题", "复杂网络技术问题", "高度复杂疑难问题",
]


# 问题定义（TypeSafe System One契约：type判别键，choice/score需instructions+criteria）
QUESTIONS: Dict[str, Any] = {
    "intent": {
        "type": "choice",
        "options": list(INTENT_OPTIONS),
        "instructions": "判断SDWAN产品客服对话中用户消息的主要意图，从选项中选择最匹配的一个",
        "criteria": {
            "price_inquiry": "用户询问价格、费用、资费",
            "product_comparison": "用户比较产品或询问是否支持某功能",
            "technical_support": "用户遇到连接、登录等技术问题",
            "usage_guide": "用户询问怎么使用、如何操作",
            "troubleshooting": "用户反馈卡顿、慢、掉线等性能问题",
            "purchase_process": "用户询问购买、下单流程",
            "unclear": "无法归入以上任何类别",
        },
    },
    "needs_clarification": {
        "type": "noul",
        "instructions": "消息是否过于模糊、缺少上下文，需要向用户澄清提问",
    },
    "intent_confidence": {
        "type": "score",
        "instructions": "意图判断的置信程度（0=完全无法判断，100=非常确定）",
        "criteria": _score_bands(CONFIDENCE_DESCRIPTIONS),
    },
    "user_emotion": {
        "type": "choice",
        "options": list(EMOTION_OPTIONS),
        "instructions": "判断用户的情绪状态",
        "criteria": {
            "neutral": "情绪平静，正常咨询",
            "positive": "满意、感谢等积极情绪",
            "urgent": "着急、催促、强调紧急",
            "dissatisfied": "不满、失望、抱怨（尚未要求投诉）",
            "complaint_risk": "明确要投诉、举报、给差评或追责",
        },
    },
    "technical_complexity": {
        "type": "score",
        "instructions": "问题涉及的技术复杂程度（0=简单寒暄，100=复杂技术问题）",
        "criteria": _score_bands(COMPLEXITY_DESCRIPTIONS),
    },
    "escalate_to_human": {
        "type": "noul",
        "instructions": "结合情绪与问题复杂度，判断是否应该转接人工客服",
    },
}

# API调用超时（秒）
JEV_TIMEOUT_SECONDS = 5.0

# needs_clarification阈值校准（Phase 4评估集实测，2026-10-09）：
# jev-1.13对具体短问题的clarify_noul系统性偏高（10个可答评估问题0.40-0.89，
# 0.5阈值下7/10被误澄清），真模糊对照"咋整"为0.95。校准为0.9：
# 保留真模糊澄清，具体问题放行至生成层（prompt澄清指示兜底）。
# escalate的noul区分度良好（良性0.18-0.43，愤怒样本>0.5），维持0.5不变。
JEV_CLARIFY_NOUL_THRESHOLD = 0.9


class JevEngine(DecisionEngine):
    """Jev决策引擎（OpenRouter decisions端点）。"""

    def __init__(self, api_key: str, base_url: str | None = None, model: str | None = None):
        self.api_key = api_key
        self.base_url = base_url or settings.JEV_API_BASE
        self.model = model or settings.JEV_MODEL
        self.questions = QUESTIONS

    async def decide(self, message: str, context: Dict[str, Any]) -> DecisionResult:
        start = time.monotonic()

        state: Dict[str, Any] = {
            "message": message,
            "context": "SDWAN专线产品客服对话",
            "previous_intent": context.get("previous_intent") or "",
            "clarification_count": int(context.get("clarification_count", 0) or 0),
            "conversation_history": [
                {"role": m.get("role", ""), "content": m.get("content", "")}
                for m in context.get("history", [])[-10:]
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=JEV_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{self.base_url}/alpha/decisions",
                    json={"model": self.model, "state": state, "questions": self.questions},
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        # HTTP头仅允许ASCII，应用名用英文
                        "X-Title": "SDWAN-Customer-Service-Bot",
                    },
                )
                if response.status_code >= 400:
                    # 带上响应体便于定位契约问题（zod校验错误在body里）
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:400]}")
                data = response.json()

            return self._result_from_answers(data.get("answers", {}), int((time.monotonic() - start) * 1000), raw=data)
        except Exception as exc:  # noqa: BLE001 超时/认证/网络/解析失败统一回退
            return self._fallback_result(f"Jev API错误: {exc}", int((time.monotonic() - start) * 1000))

    def _result_from_answers(self, answers: Dict[str, Any], latency_ms: int, raw: Dict[str, Any] | None = None) -> DecisionResult:
        """解析TypeSafe answers结构为DecisionResult。

        - choice: answers[x].choice；noul: answers[x].noul>=0.5为真
        - score: answers[x].score为分档索引值，归一化到0-1（复杂度再映射回0-100）
        解析失败（缺字段/类型错误）时返回低置信度回退结果。
        """
        try:
            intent_answer = answers["intent"]
            intent = intent_answer["choice"]
            if intent not in INTENT_OPTIONS:
                intent = "unclear"

            intent_confidence_raw = float(answers["intent_confidence"]["score"])
            intent_confidence = min(1.0, max(0.0, intent_confidence_raw / (SCORE_BANDS - 1)))

            needs_clarification = float(answers["needs_clarification"]["noul"]) >= JEV_CLARIFY_NOUL_THRESHOLD

            complexity_raw = float(answers["technical_complexity"]["score"])
            complexity = int(min(100, max(0, round(complexity_raw / (SCORE_BANDS - 1) * 100))))

            escalate = float(answers["escalate_to_human"]["noul"]) >= 0.5
        except (KeyError, ValueError, TypeError) as exc:
            return self._fallback_result(f"Jev answers解析失败: {exc}", latency_ms)

        return DecisionResult(
            intent=intent,
            intent_confidence=intent_confidence,
            needs_clarification=needs_clarification,
            clarification_reason="Jev判断需要澄清" if needs_clarification else "",
            technical_complexity=complexity,
            user_emotion=str(answers["user_emotion"]["choice"]),
            escalate_to_human=escalate,
            raw_response=raw or {},
            latency_ms=latency_ms,
            engine="jev",
        )

    def _fallback_result(self, reason: str, latency_ms: int) -> DecisionResult:
        """调用失败时返回低置信度结果，保证调用方总能拿到DecisionResult。"""
        return DecisionResult(
            intent="unclear",
            intent_confidence=0.0,
            needs_clarification=True,
            clarification_reason=reason,
            technical_complexity=50,
            user_emotion="neutral",
            escalate_to_human=False,
            latency_ms=latency_ms,
            engine="jev_failed",
        )
