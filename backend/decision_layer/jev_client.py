"""Jev API决策引擎：调用TypeSafe API做意图识别。

无API key或调用失败时回退低置信度结果（intent=unclear），保证任何输入都有输出。
"""
import time
from typing import Any, Dict

import httpx

from backend.config import settings
from backend.decision_layer.base import DecisionEngine, DecisionResult

# 问题定义（与TDD 2.3.3一致，7种意图）
QUESTIONS: Dict[str, Any] = {
    "intent": {
        "kind": "choice",
        "options": [
            "price_inquiry",
            "product_comparison",
            "technical_support",
            "usage_guide",
            "troubleshooting",
            "purchase_process",
            "unclear",
        ],
    },
    "needs_clarification": {"kind": "noul"},
    "intent_confidence": {"kind": "score", "min": 0, "max": 100},
    "user_emotion": {"kind": "choice", "options": ["neutral", "positive", "negative", "urgent"]},
    "technical_complexity": {"kind": "score", "min": 0, "max": 100},
    "escalate_to_human": {"kind": "noul"},
}

# API调用超时（秒）
JEV_TIMEOUT_SECONDS = 5.0


class JevEngine(DecisionEngine):
    """Jev API决策引擎。"""

    def __init__(self, api_key: str, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = base_url or settings.JEV_API_BASE
        self.questions = QUESTIONS

    async def decide(self, message: str, context: Dict[str, Any]) -> DecisionResult:
        start = time.monotonic()

        state = {
            "message": message,
            "conversation_history": context.get("history", [])[-10:],
            "covered_topics": context.get("covered_topics", []),
            "previous_intent": context.get("previous_intent"),
            "clarification_count": context.get("clarification_count", 0),
        }

        try:
            async with httpx.AsyncClient(timeout=JEV_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{self.base_url}/evaluate",
                    json={"state": state, "questions": self.questions},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                response.raise_for_status()
                data = response.json()

            return self._result_from_answers(data, int((time.monotonic() - start) * 1000))
        except Exception as exc:  # noqa: BLE001 超时/网络/解析失败统一回退
            return self._fallback_result(f"Jev API错误: {exc}", int((time.monotonic() - start) * 1000))

    def _result_from_answers(self, data: Dict[str, Any], latency_ms: int) -> DecisionResult:
        """解析TypeSafe answers结构为DecisionResult。"""
        answers = data.get("answers", {})
        intent = answers["intent"]["choice"]
        confidence = answers["intent_confidence"]["score"] / 100.0
        needs_clarification = bool(answers["needs_clarification"]["noul"])
        return DecisionResult(
            intent=intent,
            intent_confidence=confidence,
            needs_clarification=needs_clarification,
            clarification_reason="Jev判断需要澄清" if needs_clarification else "",
            technical_complexity=answers["technical_complexity"]["score"],
            user_emotion=answers["user_emotion"]["choice"],
            escalate_to_human=bool(answers["escalate_to_human"]["noul"]),
            raw_response=data,
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
