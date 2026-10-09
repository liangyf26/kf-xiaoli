"""Qwen LLM客户端：封装OpenAI兼容API的对话生成调用。

- 支持流式（generate_stream）与非流式（generate）
- 配置从settings读取（MODEL_API_BASE/MODEL_NAME/TEMPERATURE/MAX_TOKENS/MODEL_API_KEY）
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

    def __init__(self, base_url: str | None = None, model: str | None = None):
        """初始化客户端，配置默认来自settings，可显式覆盖。"""
        self.base_url = base_url or settings.MODEL_API_BASE
        self.model = model or settings.MODEL_NAME
        self.api_key = settings.MODEL_API_KEY
        self.temperature = settings.TEMPERATURE
        self.max_tokens = settings.MAX_TOKENS
        self.timeout = 60.0  # 任务书决策5：平衡体验与复杂问题处理
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

    def _payload(self, messages: List[Dict[str, str]], stream: bool) -> Dict[str, Any]:
        """构造chat/completions请求体。"""
        return {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }

    async def generate(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        """非流式生成。返回 {response, usage, latency_ms}；失败重试1次后仍失败则抛出异常。"""
        start = time.monotonic()
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                logger.debug("LLM调用开始: model=%s attempt=%d", self.model, attempt + 1)
                client = self._get_client()
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=self._payload(messages, stream=False),
                    headers=self._headers(),
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
                data = response.json()

                latency_ms = int((time.monotonic() - start) * 1000)
                logger.info(
                    "LLM调用结束: %dms completion_tokens=%s",
                    latency_ms, data.get("usage", {}).get("completion_tokens", "?"),
                )
                return {
                    "response": data["choices"][0]["message"]["content"],
                    "usage": data.get("usage", {}),
                    "latency_ms": latency_ms,
                }
            except Exception as exc:  # noqa: BLE001 重试一次
                last_error = exc
                logger.warning("LLM调用失败(第%d次): %s", attempt + 1, str(exc)[:120])

        if isinstance(last_error, httpx.TimeoutException):
            # httpx超时异常str()为空，转为含明确信息的异常（任务书验收要求消息可读）
            raise RuntimeError(f"LLM请求超时（timed out after {self.timeout}s）") from last_error
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
