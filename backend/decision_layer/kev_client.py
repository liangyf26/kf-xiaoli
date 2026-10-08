"""Kev-0.5B本地模型决策引擎：transformers加载，构建决策prompt，解析JSON输出。

模型懒加载：构造时不加载（工厂切换/结构测试无需torch），首次decide时加载。
模型不可用（未安装transformers、下载失败、推理异常）时返回低置信度回退结果，
保证任何输入都有DecisionResult输出（与Jev回退策略一致）。
"""
import asyncio
import json
import logging
import time
from typing import Any, Dict

from backend.config import settings
from backend.decision_layer.base import DecisionEngine, DecisionResult

logger = logging.getLogger(__name__)

# 决策prompt中使用的意图选项（与Jev问题定义一致）
INTENT_OPTIONS = (
    "price_inquiry",
    "product_comparison",
    "technical_support",
    "usage_guide",
    "troubleshooting",
    "purchase_process",
    "unclear",
)


class KevEngine(DecisionEngine):
    """Kev本地模型决策引擎。"""

    def __init__(self, model_path: str | None = None, device: str | None = None):
        self.model_path = model_path or settings.KEV_MODEL_PATH
        self.device = device or settings.KEV_DEVICE
        self.model = None
        self.tokenizer = None
        self._load_failed_reason = ""  # 加载失败后短路，避免每条消息重复尝试

    async def decide(self, message: str, context: Dict[str, Any]) -> DecisionResult:
        start = time.monotonic()
        try:
            self._ensure_loaded()
            prompt = self._build_kev_prompt(message, context)
            # 推理是同步阻塞调用，放入线程池避免阻塞事件循环
            loop = asyncio.get_running_loop()
            response_text = await loop.run_in_executor(None, self._generate_sync, prompt)
            return self._parse_output(response_text, int((time.monotonic() - start) * 1000))
        except Exception as exc:  # noqa: BLE001 模型缺失/加载失败/推理异常统一回退
            logger.warning("Kev决策回退: %s", exc)
            return self._fallback_result(f"Kev模型不可用: {exc}", int((time.monotonic() - start) * 1000))

    def _ensure_loaded(self) -> None:
        """首次调用时加载模型与tokenizer；此前失败过则直接抛错短路。"""
        if self.model is not None:
            return
        if self._load_failed_reason:
            raise RuntimeError(self._load_failed_reason)

        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            self._load_failed_reason = (
                f"缺少依赖(transformers/torch): {exc}。"
                "请安装requirements.txt中的transformers==4.37.2、torch==2.2.0后重试"
            )
            raise RuntimeError(self._load_failed_reason) from exc

        try:
            dtype = torch.float16 if self.device != "cpu" else torch.float32
            self.model = AutoModelForCausalLM.from_pretrained(
                self.model_path,
                torch_dtype=dtype,
                device_map="auto" if self.device != "cpu" else None,
            )
            if self.device == "cpu":
                self.model = self.model.to("cpu")
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_path)
            self.model.eval()
            logger.info("Kev模型加载完成: %s (%s)", self.model_path, self.device)
        except Exception as exc:  # noqa: BLE001 下载失败/模型不存在等
            self._load_failed_reason = f"模型加载失败({self.model_path}): {exc}"
            raise RuntimeError(self._load_failed_reason) from exc

    def _generate_sync(self, prompt: str) -> str:
        """同步推理一次（由decide放入线程池调用）。"""
        import torch

        with torch.no_grad():
            inputs = self.tokenizer(prompt, return_tensors="pt")
            if self.device != "cpu":
                inputs = {k: v.to(self.device) for k, v in inputs.items()}
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=256,
                temperature=0.3,
                do_sample=False,
            )
            return self.tokenizer.decode(outputs[0], skip_special_tokens=True)

    def _build_kev_prompt(self, message: str, context: Dict[str, Any]) -> str:
        """构建决策prompt（TDD 2.3.4）。"""
        history = context.get("history", [])[-5:]
        history_text = "\n".join(f"{m['role']}: {m['content']}" for m in history)

        return f"""分析以下客服对话，返回JSON格式的决策结果。

对话历史:
{history_text}

用户最新消息: {message}

请返回（intent必须是以下选项之一: {", ".join(INTENT_OPTIONS)}）:
{{
  "intent": "意图",
  "intent_confidence": 0.0到1.0,
  "needs_clarification": true或false,
  "clarification_reason": "原因",
  "technical_complexity": 0到100,
  "user_emotion": "neutral/positive/negative/urgent之一",
  "escalate_to_human": true或false
}}

JSON:"""

    def _parse_output(self, response_text: str, latency_ms: int) -> DecisionResult:
        """解析模型输出的JSON（容忍```json代码块包裹），失败返回低置信度结果。"""
        try:
            payload = response_text.split("```json")[-1].split("```")[0]
            result = json.loads(payload)
            intent = result.get("intent", "unclear")
            if intent not in INTENT_OPTIONS:
                intent = "unclear"
            confidence = float(result.get("intent_confidence", 0.5))
            confidence = min(1.0, max(0.0, confidence))
            return DecisionResult(
                intent=intent,
                intent_confidence=confidence,
                needs_clarification=bool(result.get("needs_clarification", False)),
                clarification_reason=str(result.get("clarification_reason", "")),
                technical_complexity=int(result.get("technical_complexity", 50)),
                user_emotion=str(result.get("user_emotion", "neutral")),
                escalate_to_human=bool(result.get("escalate_to_human", False)),
                raw_response=result,
                latency_ms=latency_ms,
                engine="kev",
            )
        except (ValueError, TypeError) as exc:
            logger.warning("Kev输出解析失败: %s", exc)
            return self._fallback_result("Kev输出解析失败", latency_ms)

    def _fallback_result(self, reason: str, latency_ms: int) -> DecisionResult:
        """模型不可用/解析失败时返回低置信度结果。"""
        return DecisionResult(
            intent="unclear",
            intent_confidence=0.0,
            needs_clarification=True,
            clarification_reason=reason,
            technical_complexity=50,
            user_emotion="neutral",
            escalate_to_human=False,
            latency_ms=latency_ms,
            engine="kev_failed",
        )
