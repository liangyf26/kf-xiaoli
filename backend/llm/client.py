"""Qwen LLM客户端：封装OpenAI兼容API的对话生成调用。

- 支持流式（generate_stream）与非流式（generate）
- 配置从settings读取（MODEL_API_BASE/MODEL_NAME/TEMPERATURE/MODEL_API_KEY；
  max_tokens按路径拆分：默认GEN_MAX_TOKENS/GEN_ENABLE_THINKING，意图快判由
  QwenEngine显式传DECISION_MAX_TOKENS/DECISION_ENABLE_THINKING）
- 60秒超时，失败自动重试1次
- generate返回 {response, usage, latency_ms}
- 持久连接复用（Phase 4性能优化）：AsyncClient按事件循环惰性创建并缓存，
  避免每次请求重建TCP连接
"""
import asyncio
import logging
import time
import weakref
from typing import Any, AsyncIterator, Dict, List

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)


class QwenClient:
    """Qwen主LLM客户端（OpenAI兼容chat/completions）。"""

    def __init__(self, base_url: str | None = None, model: str | None = None,
                 enable_thinking: bool | None = None, max_tokens: int | None = None):
        """初始化客户端，配置默认来自settings，可显式覆盖。

        enable_thinking/max_tokens 缺省取生成路径配置（GEN_*）；意图快判路径由
        QwenEngine 显式传 DECISION_*。思考开关经 chat_template_kwargs 传递
        （Qwen3.8-27B 实测：顶层 enable_thinking 无效，chat_template_kwargs 才生效；
        思考开时 max_tokens<512 会出空正文，这里统一抬到 512）。
        """
        self.base_url = base_url or settings.MODEL_API_BASE
        self.model = model or settings.MODEL_NAME
        self.api_key = settings.MODEL_API_KEY
        self.temperature = settings.TEMPERATURE
        self.max_tokens = max_tokens if max_tokens is not None else settings.GEN_MAX_TOKENS
        self.enable_thinking = (settings.GEN_ENABLE_THINKING
                                if enable_thinking is None else enable_thinking)
        if self.enable_thinking and self.max_tokens < 512:
            logger.warning(
                "enable_thinking=True 且 max_tokens=%d < 512（实测思考会吃光 token 出空正文），抬到 512",
                self.max_tokens)
            self.max_tokens = 512
        # #062 第八节③：生成超时 60→30s——#061 实测 85-89s 三题全是「60.3s 客户端
        # 超时+重试」模式；21 字话术不需要 60s，30s 内出不来就走重试/降级
        self.timeout = 30.0
        self.max_retries = 1
        self._clients: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def _get_client(self) -> httpx.AsyncClient:
        """取当前事件循环的持久客户端（首个请求时创建，随循环回收）。"""
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
            self._clients[loop] = client
        return client

    def _headers(self) -> Dict[str, str]:
        """构造请求头（含Bearer认证）。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, messages: List[Dict[str, str]], stream: bool,
                 enable_thinking: bool | None = None) -> Dict[str, Any]:
        """构造chat/completions请求体。

        思考开关走 chat_template_kwargs（Qwen3.8 实测唯一生效写法）；
        enable_thinking 形参可 per-call 覆盖（#062 §10 缺陷重试关思考防 JSON 截断）。
        """
        thinking = self.enable_thinking if enable_thinking is None else enable_thinking
        return {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
            "chat_template_kwargs": {"enable_thinking": thinking},
        }

    async def generate(
        self,
        messages: List[Dict[str, str]],
        timeout_s: float | None = None,
        retries: int | None = None,
        enable_thinking: bool | None = None,
    ) -> Dict[str, Any]:
        """非流式生成。返回 {response, usage, latency_ms}；失败重试1次后仍失败则抛出异常。

        timeout_s/retries/enable_thinking（#062 §9.2/§10 缺陷重试专用）：per-call
        覆盖——重试用 15s 短超时、不内部重试、关思考（seed11 实测 12/68 次调用
        completion_tokens 打满 1024=GEN_MAX_TOKENS，思考吃 token 致 JSON 截断）。
        缺省 None=沿用构造配置，行为不变。
        """
        timeout = self.timeout if timeout_s is None else timeout_s
        n_retries = self.max_retries if retries is None else retries
        start = time.monotonic()
        last_error: Exception | None = None

        for attempt in range(n_retries + 1):
            try:
                logger.debug("LLM调用开始: model=%s attempt=%d", self.model, attempt + 1)
                client = self._get_client()
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=self._payload(messages, stream=False,
                                       enable_thinking=enable_thinking),
                    headers=self._headers(),
                    timeout=timeout,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
                data = response.json()

                latency_ms = int((time.monotonic() - start) * 1000)
                choice = data["choices"][0]
                content = choice["message"]["content"]
                # #062 §10：finish_reason/响应长度进日志——「空回复或JSON残缺」要能
                # 逐题自证是 max_tokens 截断（finish_reason=length）而不是超时
                logger.info(
                    "LLM调用结束: %dms completion_tokens=%s finish_reason=%s resp_chars=%d",
                    latency_ms, data.get("usage", {}).get("completion_tokens", "?"),
                    choice.get("finish_reason"), len(content or ""),
                )
                return {
                    "response": content,
                    "usage": data.get("usage", {}),
                    "latency_ms": latency_ms,
                    "finish_reason": choice.get("finish_reason"),
                }
            except Exception as exc:  # noqa: BLE001 重试一次
                last_error = exc
                logger.warning("LLM调用失败(第%d次): %s", attempt + 1, str(exc)[:120])

        if isinstance(last_error, httpx.TimeoutException):
            # httpx超时异常str()为空，转为含明确信息的异常（任务书验收要求消息可读）
            raise RuntimeError(f"LLM请求超时（timed out after {timeout}s）") from last_error
        raise last_error  # type: ignore[misc]

    async def generate_stream(self, messages: List[Dict[str, str]]) -> AsyncIterator[str]:
        """流式生成：逐段产出增量文本。失败重试1次后仍失败则抛出异常。"""
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                client = self._get_client()
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=self._payload(messages, stream=True),
                    headers=self._headers(),
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        raise RuntimeError(f"HTTP {response.status_code}: {body[:300]}")
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        chunk = line[len("data:"):].strip()
                        if chunk == "[DONE]":
                            return
                        delta = json_loads_safe(chunk)
                        if delta:
                            yield delta
                return
            except Exception as exc:  # noqa: BLE001 重试一次
                last_error = exc

        raise last_error  # type: ignore[misc]


def json_loads_safe(chunk: str) -> str:
    """解析SSE数据块，提取增量content；解析失败返回空串。"""
    try:
        data = __import__("json").loads(chunk)
        delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
        return delta or ""
    except Exception:  # noqa: BLE001 忽略心跳/残缺块
        return ""
