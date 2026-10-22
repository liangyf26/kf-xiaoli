"""Jev API决策引擎：按 JEV_CHANNELS 渠道链访问 TypeSafe Jev（System One）结构化决策。

Jev是System One结构化决策模型（返回typed choice），不走chat/completions。
渠道链（2026-10-22 用户定案）：TypeSafe 官方(/v1/systemone) → OpenCode 免费 →
OpenCode 付费 → OpenRouter(/alpha/decisions)；某渠道超时/401/402/429/5xx/解析失败
记日志（渠道名+错误）换下一个，全挂才回退低置信度结果（不抛异常）。
"""
import logging
import time
from typing import Any, Dict

import httpx

from backend.config import settings
from backend.decision_layer.base import (
    VALID_EMOTIONS,
    DecisionEngine,
    DecisionResult,
    enforce_emotion_contract,
)

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
EMOTION_OPTIONS = VALID_EMOTIONS

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

# 渠道健康计数（进程内）：{渠道名: {"ok": n, "fail": n, "last_error": str}}，/metrics 暴露
_CHANNEL_STATS: Dict[str, Dict[str, Any]] = {}

# 占位符 key（.env.example 模板值）：不算已配置，不触发 legacy 回退、不发请求
PLACEHOLDER_KEYS = ("", "your-jev-api-key-here")


def _record_channel(name: str, ok: bool, error: str | None) -> None:
    stats = _CHANNEL_STATS.setdefault(
        name, {"ok": 0, "fail": 0, "last_error": "", "configured": True})
    if ok:
        stats["ok"] += 1
    else:
        stats["fail"] += 1
        stats["last_error"] = (error or "")[:200]


def channel_stats() -> Dict[str, Dict[str, Any]]:
    """只读副本：渠道名 → {ok, fail, last_error, configured}，供 /metrics 输出。

    configured=False 表示该渠道在 JEV_CHANNELS 里但配置不全被跳过（未发过请求）。
    """
    return {name: dict(stats) for name, stats in _CHANNEL_STATS.items()}


def reset_channel_stats() -> None:
    """清零渠道健康计数（/metrics/reset 一并清；configured 标记保留）。"""
    for stats in _CHANNEL_STATS.values():
        stats["ok"] = 0
        stats["fail"] = 0
        stats["last_error"] = ""


class JevChannel:
    """单个 Jev 渠道：独立 base_url / endpoint / model / api_key。"""

    def __init__(self, name: str, base_url: str, endpoint: str, model: str, api_key: str):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key

    def url(self) -> str:
        return self.base_url + self.endpoint


def _build_channels() -> list:
    """按 JEV_CHANNELS 顺序从 settings 组渠道链；任一配置项为空则跳过该渠道。

    向后兼容（审查 P1-1）：一个渠道都组不出来且旧 JEV_API_KEY 是真实 key 时，
    回退 legacy OpenRouter 单渠道（/alpha/decisions），避免老 .env 静默失去 jev。
    占位符（模板值）不算真实 key，不触发回退、不发请求（审查 P2-1）。
    """
    channels = []
    for name in [c.strip() for c in (settings.JEV_CHANNELS or "").split(",") if c.strip()]:
        prefix = "JEV_%s_" % name.upper()
        base = getattr(settings, prefix + "BASE_URL", "")
        endpoint = getattr(settings, prefix + "ENDPOINT", "")
        model = getattr(settings, prefix + "MODEL", "")
        key = getattr(settings, prefix + "API_KEY", "")
        # 配置过的渠道（含因配置不全跳过的）都登记健康计数，/metrics 才能看出
        # 「配置缺失被跳过」而不是「没这个渠道」（审查 P2-2）；configured 每次
        # 组链都刷新，运行时切换引擎重建渠道后标记与当前配置保持一致
        configured = bool(base and endpoint and model and key)
        stats = _CHANNEL_STATS.setdefault(name, {"ok": 0, "fail": 0, "last_error": ""})
        stats["configured"] = configured
        if base and endpoint and model and key:
            channels.append(JevChannel(name, base, endpoint, model, key))
        else:
            logger.warning("Jev 渠道 %s 配置不全（base/endpoint/model/key），跳过", name)
    if not channels and settings.JEV_API_KEY not in PLACEHOLDER_KEYS:
        logger.warning(
            "Jev 渠道链组不出任何渠道，回退 legacy 单渠道（%s，已废弃，建议迁移到 JEV_CHANNELS 渠道链）",
            settings.JEV_API_BASE)
        channels.append(JevChannel(
            "legacy", settings.JEV_API_BASE, "/alpha/decisions",
            settings.JEV_MODEL, settings.JEV_API_KEY))
    return channels

# needs_clarification阈值校准（Phase 4评估集实测，2026-10-09）：
# jev-1.13对具体短问题的clarify_noul系统性偏高（10个可答评估问题0.40-0.89，
# 0.5阈值下7/10被误澄清），真模糊对照"咋整"为0.95。校准为0.9：
# 保留真模糊澄清，具体问题放行至生成层（prompt澄清指示兜底）。
# escalate的noul区分度良好（良性0.18-0.43，愤怒样本>0.5），维持0.5不变。
JEV_CLARIFY_NOUL_THRESHOLD = 0.9


class JevEngine(DecisionEngine):
    """Jev决策引擎：按 JEV_CHANNELS 渠道链依次尝试，全挂才回退低置信度。

    兼容旧用法：显式传 api_key/base_url/model 时走单渠道（base_url + /alpha/decisions，
    与 2026-10-22 前行为一致，单测注入用）；无参构造才启用渠道链（生产路径）。
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None):
        self.questions = QUESTIONS
        if api_key is None and base_url is None:
            self.channels = _build_channels()
            self.base_url = self.channels[0].base_url if self.channels else ""
            self.model = self.channels[0].model if self.channels else settings.JEV_MODEL
            self._legacy = False
        else:
            # 旧签名：单渠道，路径固定 /alpha/decisions（现有已实测可用的 OpenRouter 契约）
            self.channels = [JevChannel(
                "legacy", base_url or settings.JEV_API_BASE,
                "/alpha/decisions", model or settings.JEV_MODEL, api_key or "")]
            self.base_url = self.channels[0].base_url
            self.model = self.channels[0].model
            self._legacy = True

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
        errors = []
        for channel in self.channels:
            result = await self._try_channel(channel, state, start, errors)
            if result is not None:
                return result
        reason = ("Jev 全渠道失败: " + "；".join(errors)) if errors else "Jev 无可用渠道"
        return self._fallback_result(reason, int((time.monotonic() - start) * 1000))

    async def _try_channel(self, channel: JevChannel, state: Dict[str, Any],
                           start: float, errors: list) -> DecisionResult | None:
        """单渠道一次尝试：成功返回结果，失败记录健康计数后返回 None 换下一个。"""
        try:
            async with httpx.AsyncClient(timeout=JEV_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    channel.url(),
                    json={"model": channel.model, "state": state, "questions": self.questions},
                    headers={
                        "Authorization": f"Bearer {channel.api_key}",
                        "Content-Type": "application/json",
                        # HTTP头仅允许ASCII，应用名用英文
                        "X-Title": "SDWAN-Customer-Service-Bot",
                    },
                )
            if response.status_code >= 400:
                # 带上响应体便于定位契约问题（zod校验错误在body里）
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:400]}")
            data = response.json()
            latency_ms = int((time.monotonic() - start) * 1000)
            result = self._result_from_answers(data.get("answers", {}), latency_ms, raw=data)
            if result.engine == "jev_failed":
                # answers 结构解析失败视同渠道失败：换下一个渠道
                raise RuntimeError(f"answers 解析失败: {result.clarification_reason[:160]}")
            _record_channel(channel.name, True, None)
            # 评测要逐题归因到渠道：raw_response 是调试字段，不进模型契约
            result.raw_response = {**(result.raw_response or {}), "channel": channel.name}
            return result
        except Exception as exc:  # noqa: BLE001 超时/认证/额度/5xx/解析失败都换下一个渠道
            detail = str(exc)[:300]
            _record_channel(channel.name, False, detail)
            if "HTTP 401" in detail or "HTTP 403" in detail:
                # 审查 P1-2：认证失败单列 ERROR（渠道名+原因），不与超时/5xx混成一句
                logger.error("Jev 渠道 %s 认证失败（401/403，key无效或过期，需到对应平台重新签发）: %s",
                             channel.name, detail)
            else:
                logger.warning("Jev 渠道 %s 失败，换下一个: %s", channel.name, detail)
            errors.append("%s: %s" % (channel.name, detail[:80]))
            return None

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
            raw_emotion = str(answers["user_emotion"]["choice"])
        except (KeyError, ValueError, TypeError) as exc:
            return self._fallback_result(f"Jev answers解析失败: {exc}", latency_ms)

        # 情绪契约：非法值（如旧negative）降级neutral；complaint_risk强制转人工
        emotion, escalate = enforce_emotion_contract(raw_emotion, escalate)

        return DecisionResult(
            intent=intent,
            intent_confidence=intent_confidence,
            needs_clarification=needs_clarification,
            clarification_reason="Jev判断需要澄清" if needs_clarification else "",
            technical_complexity=complexity,
            user_emotion=emotion,
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
