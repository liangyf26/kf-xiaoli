"""规则引擎：关键词意图识别 + 澄清判断 + 情绪识别 + 复杂度评估。

目标响应时间<100ms，任何输入都有确定输出，作为决策层的兜底方案。
"""
import time
from typing import Any, Dict, Tuple

from backend.decision_layer.base import DecisionEngine, DecisionResult

# 意图关键词字典（与TDD 2.3.2一致）
INTENT_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "price_inquiry": ("多少钱", "价格", "费用", "收费", "元", "钱"),
    "technical_support": ("连不上", "不上", "登不上", "错误", "失败", "问题", "故障"),
    "usage_guide": ("怎么", "如何", "怎样", "咋", "咋整", "用法", "使用"),
    "product_comparison": ("能不能", "可以", "支持", "有没有", "区别", "对比"),
    "troubleshooting": ("卡", "慢", "掉线", "断开", "不稳定"),
    "purchase_process": ("购买", "买", "订购", "下单"),
}

# 无关键词时的兜底意图与置信度
UNCLEAR_INTENT = "unclear"
UNCLEAR_CONFIDENCE = 0.3

# 各意图的技术复杂度
COMPLEXITY_BY_INTENT: Dict[str, int] = {
    "price_inquiry": 20,
    "usage_guide": 40,
    "technical_support": 60,
    "product_comparison": 50,
    "troubleshooting": 60,
    "purchase_process": 30,
    UNCLEAR_INTENT: 10,
}

# 情绪关键词
EMOTION_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "negative": ("不行", "垃圾", "差", "坑", "骗", "退款", "投诉", "烦", "生气"),
    "urgent": ("紧急", "赶紧", "快", "马上", "急"),
    "positive": ("好", "不错", "谢谢", "感谢", "棒"),
}

# 触发澄清的置信度阈值
CLARIFY_CONFIDENCE_THRESHOLD = 0.6
# 触发澄清的消息长度上限（字）
SHORT_MESSAGE_LENGTH = 5


class RuleBasedEngine(DecisionEngine):
    """基于规则的决策引擎（兜底方案，<100ms）。"""

    def __init__(self):
        self.intent_keywords = {k: list(v) for k, v in INTENT_KEYWORDS.items()}
        self.emotion_keywords = {k: list(v) for k, v in EMOTION_KEYWORDS.items()}

    async def decide(self, message: str, context: Dict[str, Any]) -> DecisionResult:
        start = time.monotonic()

        intent, confidence = self._match_intent(message)
        needs_clarification, clarify_reason = self._check_clarification(message, confidence, context)
        complexity = self._estimate_complexity(intent)
        emotion = self._detect_emotion(message)
        escalate = self._should_escalate(emotion, complexity, context)

        return DecisionResult(
            intent=intent,
            intent_confidence=confidence,
            needs_clarification=needs_clarification,
            clarification_reason=clarify_reason,
            technical_complexity=complexity,
            user_emotion=emotion,
            escalate_to_human=escalate,
            latency_ms=int((time.monotonic() - start) * 1000),
            engine="rule",
        )

    def _match_intent(self, message: str) -> Tuple[str, float]:
        """关键词计分匹配，返回(意图, 置信度)。无命中返回unclear。"""
        scores: Dict[str, int] = {}
        for intent, keywords in self.intent_keywords.items():
            score = sum(1 for kw in keywords if kw and kw in message)
            if score > 0:
                scores[intent] = score

        if not scores:
            return UNCLEAR_INTENT, UNCLEAR_CONFIDENCE

        best_intent = max(scores, key=scores.get)
        # 命中越多关键词置信度越高，上限0.95
        confidence = min(0.95, 0.5 + scores[best_intent] * 0.2)
        return best_intent, confidence

    def _check_clarification(self, message: str, confidence: float, context: Dict[str, Any]) -> Tuple[bool, str]:
        """澄清判断：已澄清2次不再澄清 > 有上下文 > 消息过短 > 置信度不足。"""
        clarification_count = int(context.get("clarification_count", 0) or 0)
        if clarification_count >= 2:
            return False, ""

        # 有对话上下文（历史或上一轮意图）时不需要澄清
        if context.get("previous_intent") or context.get("history"):
            return False, ""

        if len(message.strip()) < SHORT_MESSAGE_LENGTH:
            return True, "消息过短，缺少上下文"

        if confidence < CLARIFY_CONFIDENCE_THRESHOLD:
            return True, "无法明确识别意图"

        return False, ""

    def _estimate_complexity(self, intent: str) -> int:
        """按意图映射技术复杂度。"""
        return COMPLEXITY_BY_INTENT.get(intent, 30)

    def _detect_emotion(self, message: str) -> str:
        """情绪识别：negative > urgent > positive > neutral。"""
        for emotion, keywords in self.emotion_keywords.items():
            if any(kw in message for kw in keywords):
                return emotion
        return "neutral"

    def _should_escalate(self, emotion: str, complexity: int, context: Dict[str, Any]) -> bool:
        """转人工判断：负面情绪、紧急且高复杂度、反复澄清仍未识别。"""
        if emotion == "negative":
            return True
        if emotion == "urgent" and complexity >= 60:
            return True
        if int(context.get("clarification_count", 0) or 0) >= 3:
            return True
        return False
