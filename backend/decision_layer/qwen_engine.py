"""Qwen意图识别引擎：直接用已接入的主LLM（Qwen3.8）做结构化决策。

与生成层共用QwenClient；Few-shot提示词约束Qwen只输出JSON（字段与DecisionResult一致）。
10秒超时或JSON解析/校验失败时降级：intent=unclear、置信度0、engine="qwen_failed"，
任何情况下不抛异常（决策失败不阻塞对话）。
"""
import asyncio
import logging
import time
from typing import Any, Dict

from backend.config import settings
from backend.decision_layer.base import DecisionEngine, DecisionResult
from backend.llm.client import QwenClient
from backend.llm.parser import parse_json_object

logger = logging.getLogger(__name__)

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

# 决策超时（秒）：意图识别应快于生成，超时即降级
DEFAULT_TIMEOUT_SECONDS = 10.0

# Few-shot示例（紧凑单行，控制prompt长度以降低决策延迟）
_FEWSHOT = """用户：直播线路多少钱一个月
输出：{"intent": "price_inquiry", "intent_confidence": 0.9, "needs_clarification": false, "technical_complexity": 20, "user_emotion": "neutral", "escalate_to_human": false}
用户：tiktok登不上怎么办
输出：{"intent": "technical_support", "intent_confidence": 0.85, "needs_clarification": false, "technical_complexity": 60, "user_emotion": "neutral", "escalate_to_human": false}
用户：看视频有点卡
输出：{"intent": "troubleshooting", "intent_confidence": 0.8, "needs_clarification": false, "technical_complexity": 60, "user_emotion": "neutral", "escalate_to_human": false}
用户：怎么安装客户端
输出：{"intent": "usage_guide", "intent_confidence": 0.9, "needs_clarification": false, "technical_complexity": 40, "user_emotion": "neutral", "escalate_to_human": false}
用户：怎么购买
输出：{"intent": "purchase_process", "intent_confidence": 0.85, "needs_clarification": false, "technical_complexity": 30, "user_emotion": "neutral", "escalate_to_human": false}
用户：能不能直播
输出：{"intent": "product_comparison", "intent_confidence": 0.7, "needs_clarification": false, "technical_complexity": 50, "user_emotion": "neutral", "escalate_to_human": false}
用户：啊这
输出：{"intent": "unclear", "intent_confidence": 0.3, "needs_clarification": true, "technical_complexity": 10, "user_emotion": "neutral", "escalate_to_human": false}
用户：你们这个服务太差了，非常失望
输出：{"intent": "unclear", "intent_confidence": 0.5, "needs_clarification": false, "technical_complexity": 30, "user_emotion": "dissatisfied", "escalate_to_human": false}
用户：再不解决我就投诉你们
输出：{"intent": "unclear", "intent_confidence": 0.5, "needs_clarification": false, "technical_complexity": 30, "user_emotion": "complaint_risk", "escalate_to_human": true}"""


def _coerce_bool(value: Any) -> bool:
    """宽松布尔解析：接受True/False与"true"/"false"/1/0等字符串。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "是")


class QwenEngine(DecisionEngine):
    """Qwen主LLM决策引擎（Few-shot JSON输出，超时/解析失败降级qwen_failed）。"""

    def __init__(self, llm_client: QwenClient | None = None, timeout_seconds: float | None = None):
        """llm_client可注入替身（测试用）；timeout_seconds默认10秒。"""
        self.llm_client = llm_client or QwenClient(
            base_url=settings.MODEL_API_BASE, model=settings.MODEL_NAME
        )
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else DEFAULT_TIMEOUT_SECONDS

    async def decide(self, message: str, context: Dict[str, Any]) -> DecisionResult:
        start = time.monotonic()
        prompt = self._build_prompt(message, context)
        try:
            result = await asyncio.wait_for(
                self.llm_client.generate([{"role": "user", "content": prompt}]),
                timeout=self.timeout_seconds,
            )
            return self._parse_result(str(result.get("response", "")), int((time.monotonic() - start) * 1000))
        except Exception as exc:  # noqa: BLE001 超时/解析失败/调用失败统一降级，不抛异常
            logger.warning("Qwen决策回退: %s", str(exc)[:160])
            return self._fallback_result(int((time.monotonic() - start) * 1000))

    def _build_prompt(self, message: str, context: Dict[str, Any]) -> str:
        """决策提示词：角色+输出schema+Few-shot+当前消息（历史仅作轻量上下文）。"""
        history = context.get("history") or []
        recent = ""
        if history:
            last = history[-1]
            recent = f"\n（上一轮{ '用户' if last.get('role') == 'user' else '客服'}：{str(last.get('content', ''))[:60]}）"
        return (
            "你是SDWAN专线客服机器人的意图识别模块。判断用户消息的意图、情绪与处理需求，"
            "严格只输出一行JSON，不要输出任何其他内容。字段定义：\n"
            'intent ∈ price_inquiry/product_comparison/technical_support/usage_guide/'
            'troubleshooting/purchase_process/unclear\n'
            "intent_confidence ∈ 0-1；needs_clarification ∈ true/false；"
            "technical_complexity ∈ 0-100\n"
            "user_emotion ∈ neutral/positive/urgent/dissatisfied（不满）/complaint_risk（要投诉举报）；"
            "complaint_risk时escalate_to_human必须为true\n\n"
            f"{_FEWSHOT}{recent}\n\n用户：{message}\n输出："
        )

    def _parse_result(self, text: str, latency_ms: int) -> DecisionResult:
        """解析并校验Qwen输出；任何字段非法都抛异常（由decide统一降级）。"""
        data = parse_json_object(text)
        if data is None:
            raise ValueError(f"Qwen输出无JSON对象: {text[:80]!r}")
        intent = str(data.get("intent", "")).strip()
        if intent not in INTENT_OPTIONS:
            raise ValueError(f"非法intent: {intent!r}")

        emotion = str(data.get("user_emotion", "")).strip()
        if emotion not in EMOTION_OPTIONS:
            raise ValueError(f"非法user_emotion: {emotion!r}")

        confidence = float(data.get("intent_confidence", 0.0))
        if confidence > 1.0:
            confidence = confidence / 100.0  # 容错：Qwen偶发返回0-100制，归一化到0-1
        confidence = min(1.0, max(0.0, confidence))
        complexity = int(min(100, max(0, round(float(data.get("technical_complexity", 30))))))
        escalate = _coerce_bool(data.get("escalate_to_human", False))
        if emotion == "complaint_risk":
            escalate = True  # 投诉风险一律转人工（提示词+解析双保险）

        return DecisionResult(
            intent=intent,
            intent_confidence=confidence,
            needs_clarification=_coerce_bool(data.get("needs_clarification", False)),
            clarification_reason=str(data.get("clarification_reason", "") or ""),
            technical_complexity=complexity,
            user_emotion=emotion,
            escalate_to_human=escalate,
            latency_ms=max(0, latency_ms),
            engine="qwen",
            raw_response={"text": text[:500]},
        )

    def _fallback_result(self, latency_ms: int) -> DecisionResult:
        """超时/解析失败降级：unclear+澄清（与KevEngine回退策略一致）。"""
        return DecisionResult(
            intent="unclear",
            intent_confidence=0.0,
            needs_clarification=True,
            clarification_reason="Qwen决策失败降级",
            technical_complexity=50,
            user_emotion="neutral",
            escalate_to_human=False,
            latency_ms=max(0, latency_ms),
            engine="qwen_failed",
        )
