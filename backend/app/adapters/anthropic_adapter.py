"""Anthropic 协议适配器：messages API ↔ 归一化契约转换。

差异处理：system 消息提升为顶层 system 参数；max_tokens 必填（默认 4096）；
usage 的 input/output_tokens 映射为 prompt/completion，cache_read/cache_creation_input_tokens
拆分为命中/写入（prompt = input + 读 + 写，归一化语义见 base.LlmUsage）。
注意：Anthropic Messages API（SDK 1.x）已移除 temperature/top_p 采样参数，本适配器静默忽略。
一律流式调用（决策 D8，超时=TTFT/块间空闲，由基座统一实现）；SDK 自带重试关闭，重试统一由基座实现。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx2 as httpx

from app.adapters.base import (
    AdapterError,
    BaseAdapter,
    LlmRequest,
    LlmUsage,
    NormalizedMessage,
    StreamEvent,
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
        timeout_seconds: float = 120.0,
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

    def stream_events(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
        return self._stream_events(request)

    async def _stream_events(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
        if request.tools:
            raise AdapterError(
                "anthropic 协议暂不支持工具透传（tools）",
                kind="bad_request",
                retryable=False,
            )
        kwargs = _build_kwargs(request)
        try:
            # 迭代原始事件而非 text_stream：thinking_delta 也要产出事件（见 base.StreamEvent），
            # 否则扩展思考阶段会被基座误判为 TTFT 超时
            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type != "content_block_delta":
                        continue
                    delta = event.delta
                    if delta.type == "text_delta":
                        if delta.text:
                            yield StreamEvent(text=delta.text)
                    elif delta.type == "thinking_delta":
                        if delta.thinking:
                            yield StreamEvent(reasoning=delta.thinking)
                final = await stream.get_final_message()
        except anthropic.APIError as exc:
            raise map_anthropic_error(exc) from None
        usage = final.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        # input_tokens 不含缓存部分，归一化为计费输入总量（T41 对账语义，见 base.LlmUsage）
        yield StreamEvent(
            usage=LlmUsage(
                prompt_tokens=usage.input_tokens + cache_read + cache_write,
                completion_tokens=usage.output_tokens,
                cached_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
        )

    async def probe(self) -> list[str]:
        try:
            page = await self._with_retry(self._client.models.list)
        except anthropic.APIError as exc:
            raise map_anthropic_error(exc) from None
        return [m.id for m in page.data]
