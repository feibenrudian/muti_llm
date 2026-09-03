"""Anthropic 协议适配器：messages API ↔ 归一化契约转换。

差异处理：system 消息提升为顶层 system 参数；max_tokens 必填（默认 4096）；
usage 的 input/output_tokens 映射为 prompt/completion；流式 content_block_delta 事件转增量文本。
注意：Anthropic Messages API（SDK 1.x）已移除 temperature/top_p 采样参数，本适配器静默忽略。
SDK 自带重试关闭，重试统一由基座实现。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx2 as httpx

from app.adapters.base import (
    AdapterError,
    BaseAdapter,
    LlmRequest,
    LlmResult,
    LlmUsage,
    NormalizedMessage,
)

DEFAULT_MAX_TOKENS = 4096


def _split_messages(
    messages: list[NormalizedMessage],
) -> tuple[str, list[dict[str, str]]]:
    system_parts = [m.content for m in messages if m.role == "system"]
    rest = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    return "\n\n".join(system_parts), rest


def _build_kwargs(request: LlmRequest) -> dict[str, Any]:
    system, messages = _split_messages(request.messages)
    # 新版 Messages API 无 temperature/top_p（见模块 docstring），仅透传结构与 max_tokens
    kwargs: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "max_tokens": request.max_tokens or DEFAULT_MAX_TOKENS,
    }
    if system:
        kwargs["system"] = system
    return kwargs


def map_anthropic_error(exc: Exception) -> AdapterError:
    if isinstance(exc, anthropic.APITimeoutError):
        return AdapterError(f"上游超时: {exc}", kind="timeout", retryable=True, detail=str(exc))
    if isinstance(exc, anthropic.APIConnectionError):
        return AdapterError(
            f"上游连接失败: {exc}", kind="connection", retryable=True, detail=str(exc)
        )
    if isinstance(exc, anthropic.RateLimitError):
        return AdapterError(f"上游限流: {exc}", kind="rate_limit", retryable=True, detail=str(exc))
    if isinstance(exc, anthropic.AuthenticationError):
        return AdapterError(f"上游认证失败: {exc}", kind="auth", retryable=False, detail=str(exc))
    if isinstance(exc, anthropic.BadRequestError):
        return AdapterError(
            f"上游拒绝请求(400): {exc}", kind="bad_request", retryable=False, detail=str(exc)
        )
    if isinstance(exc, anthropic.APIStatusError):
        return AdapterError(
            f"上游错误(HTTP {exc.status_code}): {exc}",
            kind="upstream_error",
            retryable=exc.status_code >= 500,
            detail=str(exc),
        )
    return AdapterError(
        f"未知上游错误: {exc}", kind="upstream_error", retryable=True, detail=str(exc)
    )


class AnthropicAdapter(BaseAdapter):
    def __init__(
        self,
        *,
        base_url: str = "https://api.anthropic.com",
        api_key: str = "",
        timeout_seconds: float = 60.0,
        max_retries: int = 1,
        retry_base_delay: float = 0.5,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or "EMPTY",
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
            http_client=http_client,
        )

    async def complete(self, request: LlmRequest) -> LlmResult:
        start = time.perf_counter()
        kwargs = _build_kwargs(request)

        async def _call() -> Any:
            try:
                return await self._client.messages.create(**kwargs)
            except anthropic.APIError as exc:
                raise map_anthropic_error(exc) from None

        resp = await self._with_retry(_call)
        content = "".join(block.text for block in resp.content if block.type == "text")
        return LlmResult(
            content=content,
            usage=LlmUsage(
                prompt_tokens=resp.usage.input_tokens,
                completion_tokens=resp.usage.output_tokens,
            ),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

    async def probe(self) -> list[str]:
        try:
            page = await self._client.models.list()
        except anthropic.APIError as exc:
            raise map_anthropic_error(exc) from None
        return [m.id for m in page.data]

    def stream(self, request: LlmRequest) -> AsyncIterator[str]:
        return self._stream(request)

    async def _stream(self, request: LlmRequest) -> AsyncIterator[str]:
        kwargs = _build_kwargs(request)
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
        except anthropic.APIError as exc:
            raise map_anthropic_error(exc) from None
