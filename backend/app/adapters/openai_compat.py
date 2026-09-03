"""OpenAI 兼容适配器：AsyncOpenAI 指向任意 base_url（OpenAI/DeepSeek/智谱/Ollama，决策 D1）。

SDK 自带重试关闭（max_retries=0），重试统一由基座实现。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx
import openai

from app.adapters.base import (
    AdapterError,
    BaseAdapter,
    LlmRequest,
    LlmResult,
    LlmUsage,
    NormalizedMessage,
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
        timeout_seconds: float = 60.0,
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

    async def complete(self, request: LlmRequest) -> LlmResult:
        start = time.perf_counter()

        async def _call() -> Any:
            try:
                return await self._client.chat.completions.create(
                    model=request.model,
                    messages=_to_openai_messages(request.messages),
                    stream=False,
                    **_optional_params(request),
                )
            except openai.APIError as exc:
                # 在基座重试之前归一化：基座只识别 AdapterError
                raise map_openai_error(exc) from None

        resp = await self._with_retry(_call)

        choice = resp.choices[0]
        usage = resp.usage
        return LlmResult(
            content=choice.message.content or "",
            usage=LlmUsage(
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
            ),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

    def stream(self, request: LlmRequest) -> AsyncIterator[str]:
        return self._stream(request)

    async def _stream(self, request: LlmRequest) -> AsyncIterator[str]:
        try:
            response = await self._client.chat.completions.create(
                model=request.model,
                messages=_to_openai_messages(request.messages),
                stream=True,
                **_optional_params(request),
            )
            async for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        except openai.APIError as exc:
            raise map_openai_error(exc) from None
