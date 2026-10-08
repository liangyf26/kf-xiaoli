"""Kev本地模型决策引擎。

两种运行模式（按配置自动选择）：
1. HTTP模式（推荐）：KEV_SERVE_URL指向本地kev.serve服务（官方kev包的
   `python -m kev.serve --run runs/kev`），POST /v1/systemone，TypeSafe System One契约。
   jaredpalmer/kev-0.5b是LoRA+指针头自定义架构，官方服务路径即kev包，不直接用transformers加载。
2. transformers模式：KEV_SERVE_URL为空时按KEV_MODEL_PATH懒加载（保留，供自定义完整模型）。

任一模式失败（服务不可达/解析失败/依赖缺失）都返回低置信度回退结果，保证有输出。
"""
import asyncio
import json
import logging
import time
from typing import Any, Dict

import httpx

from backend.config import settings
from backend.decision_layer.base import DecisionEngine, DecisionResult

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
EMOTION_OPTIONS = ("neutral", "positive", "negative", "urgent")

# score分档数：decisions契约限制最多10档；档索引∈[0, N-1]，归一化=score/(N-1)
SCORE_BANDS = 10

# 本地serve使用的模型名（kev.serve同时接受kev-latest/jev-latest）
SERVE_MODEL_NAME = "kev-latest"

# 问题定义（System One契约：type判别键，choice/score需criteria，score最多10档）
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
        "criteria": [
            {"description": "0-10：完全无法判断", "max": 10},
            {"description": "10-20：几乎没有把握", "min": 10, "max": 20},
            {"description": "20-30：略有线索", "min": 20, "max": 30},
            {"description": "30-40：能猜测方向", "min": 30, "max": 40},
            {"description": "40-50：有一定把握", "min": 40, "max": 50},
            {"description": "50-60：把握过半", "min": 50, "max": 60},
            {"description": "60-70：比较有把握", "min": 60, "max": 70},
            {"description": "70-80：把握较大", "min": 70, "max": 80},
            {"description": "80-90：很确定", "min": 80, "max": 90},
            {"description": "90-100：非常确定", "min": 90},
        ],
    },
    "user_emotion": {
        "type": "choice",
        "options": list(EMOTION_OPTIONS),
        "instructions": "判断用户的情绪状态",
        "criteria": {
            "neutral": "情绪平静，正常咨询",
            "positive": "满意、感谢等积极情绪",
            "negative": "不满、抱怨、投诉倾向",
            "urgent": "着急、催促、强调紧急",
        },
    },
    "technical_complexity": {
        "type": "score",
        "instructions": "问题涉及的技术复杂程度（0=简单寒暄，100=复杂技术问题）",
        "criteria": [
            {"description": "0-10：寒暄或无技术内容", "max": 10},
            {"description": "10-20：简单产品咨询", "min": 10, "max": 20},
            {"description": "20-30：一般操作咨询", "min": 20, "max": 30},
            {"description": "30-40：使用步骤指导", "min": 30, "max": 40},
            {"description": "40-50：配置类问题", "min": 40, "max": 50},
            {"description": "50-60：功能异常排查", "min": 50, "max": 60},
            {"description": "60-70：网络质量问题", "min": 60, "max": 70},
            {"description": "70-80：多设备组网问题", "min": 70, "max": 80},
            {"description": "80-90：复杂网络技术问题", "min": 80, "max": 90},
            {"description": "90-100：高度复杂疑难问题", "min": 90},
        ],
    },
    "escalate_to_human": {
        "type": "noul",
        "instructions": "结合情绪与问题复杂度，判断是否应该转接人工客服",
    },
}

# HTTP调用超时（秒）
KEV_TIMEOUT_SECONDS = 5.0


class KevEngine(DecisionEngine):
    """Kev本地模型决策引擎（kev.serve HTTP模式 / transformers懒加载模式）。"""

    def __init__(self, model_path: str | None = None, device: str | None = None):
        self.model_path = model_path or settings.KEV_MODEL_PATH
        self.device = device or settings.KEV_DEVICE
        self.serve_url = (settings.KEV_SERVE_URL or "").rstrip("/")
        self.questions = QUESTIONS
        self.model = None
        self.tokenizer = None
        self._load_failed_reason = ""  # transformers模式：加载失败后短路

    async def decide(self, message: str, context: Dict[str, Any]) -> DecisionResult:
        start = time.monotonic()
        if self.serve_url:
            return await self._decide_http(message, context, start)
        return await self._decide_transformers(message, context, start)

    # ---------- HTTP模式（kev.serve） ----------

    async def _decide_http(self, message: str, context: Dict[str, Any], start: float) -> DecisionResult:
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
        headers = {"Content-Type": "application/json"}
        if settings.KEV_API_KEY:
            headers["Authorization"] = f"Bearer {settings.KEV_API_KEY}"

        try:
            async with httpx.AsyncClient(timeout=KEV_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{self.serve_url}/v1/systemone",
                    json={"model": SERVE_MODEL_NAME, "state": state, "questions": self.questions},
                    headers=headers,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:400]}")
                data = response.json()

            return self._result_from_answers(data.get("answers", {}), int((time.monotonic() - start) * 1000), raw=data)
        except Exception as exc:  # noqa: BLE001 服务不可达/超时/解析失败统一回退
            logger.warning("Kev(serve)决策回退: %s", exc)
            return self._fallback_result(f"Kev服务不可用: {exc}", int((time.monotonic() - start) * 1000))

    # ---------- transformers模式（懒加载） ----------

    async def _decide_transformers(self, message: str, context: Dict[str, Any], start: float) -> DecisionResult:
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
                "请安装requirements.txt中的transformers、torch后重试"
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
        """构建决策prompt（TDD 2.3.4，供transformers模式使用）。"""
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

    # ---------- 公共解析与回退 ----------

    def _result_from_answers(self, answers: Dict[str, Any], latency_ms: int, raw: Dict[str, Any] | None = None) -> DecisionResult:
        """解析System One answers结构为DecisionResult（与OpenRouter decisions同构）。

        - choice: answers[x].choice；noul: answers[x].noul>=0.5为真
        - score: answers[x].score为分档索引期望值，归一化到0-1（复杂度再映射回0-100）
        """
        try:
            intent = answers["intent"]["choice"]
            if intent not in INTENT_OPTIONS:
                intent = "unclear"

            intent_confidence_raw = float(answers["intent_confidence"]["score"])
            intent_confidence = min(1.0, max(0.0, intent_confidence_raw / (SCORE_BANDS - 1)))

            needs_clarification = float(answers["needs_clarification"]["noul"]) >= 0.5

            complexity_raw = float(answers["technical_complexity"]["score"])
            complexity = int(min(100, max(0, round(complexity_raw / (SCORE_BANDS - 1) * 100))))

            escalate = float(answers["escalate_to_human"]["noul"]) >= 0.5
            emotion = str(answers["user_emotion"]["choice"])
        except (KeyError, ValueError, TypeError) as exc:
            return self._fallback_result(f"Kev answers解析失败: {exc}", latency_ms)

        return DecisionResult(
            intent=intent,
            intent_confidence=intent_confidence,
            needs_clarification=needs_clarification,
            clarification_reason="Kev判断需要澄清" if needs_clarification else "",
            technical_complexity=complexity,
            user_emotion=emotion,
            escalate_to_human=escalate,
            raw_response=raw or {},
            latency_ms=latency_ms,
            engine="kev",
        )

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
