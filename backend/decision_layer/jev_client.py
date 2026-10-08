"""Jev API决策引擎：经OpenRouter访问TypeSafe Jev结构化决策模型。

Jev是TypeSafe的System One结构化决策模型（返回typed choice而非散文），
经OpenRouter的OpenAI兼容chat/completions接口调用，返回JSON决策。
调用失败（超时/认证/网络）时回退低置信度结果（intent=unclear），保证任何输入都有输出。
"""
import json
import time
from typing import Any, Dict

import httpx

from backend.config import settings
from backend.decision_layer.base import DecisionEngine, DecisionResult

# 问题定义（决策任务schema，与TDD 2.3.3一致，7种意图）
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


def _decision_prompt(message: str, context: Dict[str, Any]) -> str:
    """构建决策任务prompt（schema来自QUESTIONS定义）。"""
    intent_options = " | ".join(QUESTIONS["intent"]["options"])
    emotion_options = " | ".join(QUESTIONS["user_emotion"]["options"])
    history = context.get("history", [])[-10:]
    history_text = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in history) or "（无）"

    return f"""客服场景决策任务。根据对话上下文和用户最新消息，返回JSON决策结果，不要输出JSON以外的任何内容。

对话历史:
{history_text}

previous_intent: {context.get("previous_intent") or "无"}
clarification_count: {context.get("clarification_count", 0)}

用户最新消息: {message}

返回JSON格式:
{{"intent": "{intent_options}之一", "intent_confidence": 0到1, "needs_clarification": true或false, "clarification_reason": "原因", "technical_complexity": 0到100, "user_emotion": "{emotion_options}之一", "escalate_to_human": true或false}}

JSON:"""


class JevEngine(DecisionEngine):
    """Jev决策引擎（OpenRouter chat/completions通道）。"""

    def __init__(self, api_key: str, base_url: str | None = None, model: str | None = None):
        self.api_key = api_key
        self.base_url = base_url or settings.JEV_API_BASE
        self.model = model or settings.JEV_MODEL
        self.questions = QUESTIONS

    async def decide(self, message: str, context: Dict[str, Any]) -> DecisionResult:
        start = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=JEV_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "user", "content": _decision_prompt(message, context)},
                        ],
                        "temperature": 0.1,
                        "max_tokens": 200,
                    },
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "HTTP-Referer": "https://github.com/liangyf26/kf-xiaoli",
                        # HTTP头仅允许ASCII，应用名用英文
                        "X-Title": "SDWAN-Customer-Service-Bot",
                    },
                )
                response.raise_for_status()
                data = response.json()

            content = data["choices"][0]["message"]["content"]
            return self._parse_chat_response(content, int((time.monotonic() - start) * 1000), raw=data)
        except Exception as exc:  # noqa: BLE001 超时/认证/网络/解析失败统一回退
            return self._fallback_result(f"Jev API错误: {exc}", int((time.monotonic() - start) * 1000))

    def _parse_chat_response(self, content: str, latency_ms: int, raw: Dict[str, Any] | None = None) -> DecisionResult:
        """解析模型返回的JSON决策（容忍```json代码块包裹与越界值）。"""
        try:
            payload = content.split("```json")[-1].split("```")[0]
            result = json.loads(payload)

            intent = result.get("intent", "unclear")
            if intent not in QUESTIONS["intent"]["options"]:
                intent = "unclear"
            confidence = float(result.get("intent_confidence", 0.5))
            confidence = min(1.0, max(0.0, confidence))
            needs_clarification = bool(result.get("needs_clarification", False))

            return DecisionResult(
                intent=intent,
                intent_confidence=confidence,
                needs_clarification=needs_clarification,
                clarification_reason=str(result.get("clarification_reason", "")) if needs_clarification else "",
                technical_complexity=int(result.get("technical_complexity", 50)),
                user_emotion=str(result.get("user_emotion", "neutral")),
                escalate_to_human=bool(result.get("escalate_to_human", False)),
                raw_response={"content": content, "response": raw or {}},
                latency_ms=latency_ms,
                engine="jev",
            )
        except (ValueError, TypeError, KeyError) as exc:
            return self._fallback_result(f"Jev输出解析失败: {exc}", latency_ms)

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
