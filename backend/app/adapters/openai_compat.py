"""OpenAI 兼容适配器：AsyncOpenAI 指向任意 base_url（OpenAI/DeepSeek/智谱/Ollama，决策 D1）。

一律流式调用（决策 D8，超时=TTFT/块间空闲，由基座统一实现）；
stream_options.include_usage 采集 token 用量，个别兼容服务不支持时降级省略。
SDK 自带重试关闭（max_retries=0），重试统一由基座实现。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx
import openai

from app.adapters.base import (
    AdapterError,
    BaseAdapter,
    LlmRequest,
    LlmUsage,
    NormalizedMessage,
    StreamEvent,
)


def _to_openai_messages(messages: list[NormalizedMessage]) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in messages]


def _optional_params(request: LlmRequest) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if request.temperature is not None:
        params["temperature"] = request.temperature
    if request.max_tokens is not None:
        params["max_tokens"] = request.max_tokens
    if request.top_p is not None:
        params["top_p"] = request.top_p
    return params


def map_openai_error(exc: Exception) -> AdapterError:
    if isinstance(exc, openai.APITimeoutError):
        return AdapterError(f"上游超时: {exc}", kind="timeout", retryable=True, detail=str(exc))
    if isinstance(exc, openai.APIConnectionError):
        return AdapterError(
            f"上游连接失败: {exc}", kind="connection", retryable=True, detail=str(exc)
        )
    if isinstance(exc, openai.RateLimitError):
        return AdapterError(f"上游限流: {exc}", kind="rate_limit", retryable=True, detail=str(exc))
    if isinstance(exc, openai.AuthenticationError):
        return AdapterError(f"上游认证失败: {exc}", kind="auth", retryable=False, detail=str(exc))
    if isinstance(exc, openai.BadRequestError):
        return AdapterError(
            f"上游拒绝请求(400): {exc}", kind="bad_request", retryable=False, detail=str(exc)
        )
    if isinstance(exc, openai.APIStatusError):
        return AdapterError(
            f"上游错误(HTTP {exc.status_code}): {exc}",
            kind="upstream_error",
            retryable=exc.status_code >= 500,
            detail=str(exc),
        )
    return AdapterError(
        f"未知上游错误: {exc}", kind="upstream_error", retryable=True, detail=str(exc)
    )


class OpenAICompatAdapter(BaseAdapter):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        timeout_seconds: float = 120.0,
        max_retries: int = 1,
        retry_base_delay: float = 0.5,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._client = openai.AsyncOpenAI(
            base_url=base_url,
            api_key=api_key or "EMPTY",
            timeout=timeout_seconds,
            max_retries=0,
            http_client=http_client,
        )

    def stream_events(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
        return self._stream_events(request)

    async def _create_stream(self, kwargs: dict[str, Any]) -> Any:
        """优先带 include_usage 采集 usage；个别兼容服务不认该参数时降级重试一次。"""
        try:
            return await self._client.chat.completions.create(
                stream_options={"include_usage": True}, **kwargs
            )
        except openai.BadRequestError:
            return await self._client.chat.completions.create(**kwargs)

    async def _stream_events(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
        response = None
        try:
            response = await self._create_stream(
                {
                    "model": request.model,
                    "messages": _to_openai_messages(request.messages),
                    "stream": True,
                    **_optional_params(request),
                }
            )
            async for chunk in response:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    yield StreamEvent(
                        usage=LlmUsage(
                            prompt_tokens=usage.prompt_tokens or 0,
                            completion_tokens=usage.completion_tokens or 0,
                        )
                    )
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                # 思考模型增量在 reasoning_content（SDK 未声明该字段，getattr 兜底）
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield StreamEvent(reasoning=reasoning)
                if delta.content:
                    yield StreamEvent(text=delta.content)
        except openai.APIError as exc:
            raise map_openai_error(exc) from None
        finally:
            if response is not None:
                await response.close()

    async def probe(self) -> list[str]:
        try:
            page = await self._with_retry(self._client.models.list)
        except openai.APIError as exc:
            raise map_openai_error(exc) from None
        return [m.id for m in page.data]
