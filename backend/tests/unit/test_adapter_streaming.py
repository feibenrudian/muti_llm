"""UT-06-7..9：流式调用的 TTFT 超时语义（决策 D8，打桩事件流，不起网络）。

超时只约束"等待首个事件"与"事件间空闲"，不约束总生成时长。
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.adapters.base import (
    AdapterError,
    BaseAdapter,
    LlmRequest,
    LlmUsage,
    NormalizedMessage,
    StreamEvent,
)

REQ = LlmRequest(model="m", messages=[NormalizedMessage(role="user", content="hi")])


def scripted_adapter(
    script: list[tuple[str, float]],
    *,
    usage: LlmUsage | None = None,
    max_retries: int = 0,
) -> BaseAdapter:
    """按脚本 (text, delay) 逐事件产出；usage 事件（若有）附在流末尾。"""

    class Scripted(BaseAdapter):
        def __init__(self) -> None:
            self.attempts = 0

        def stream_events(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
            self.attempts += 1
            return self._gen()

        async def probe(self) -> list[str]:  # pragma: no cover - 本用例不探测
            raise NotImplementedError

        async def _gen(self) -> AsyncIterator[StreamEvent]:
            for text, delay in script:
                if delay:
                    await asyncio.sleep(delay)
                yield StreamEvent(text=text)
            if usage is not None:
                yield StreamEvent(usage=usage)

    adapter = Scripted()
    adapter.max_retries = max_retries
    adapter.retry_base_delay = 0
    return adapter


async def test_ttft_timeout_is_retryable() -> None:
    """UT-06-7 TTFT 超时：首事件超预算 → kind=timeout，且首事件前可整体重试。"""
    adapter = scripted_adapter([("a", 1.0)])
    adapter.timeout_seconds = 0.2
    with pytest.raises(AdapterError) as excinfo:
        await adapter.complete(REQ)
    assert excinfo.value.kind == "timeout"
    assert excinfo.value.retryable is True
    assert adapter.attempts == 1  # max_retries=0


async def test_long_generation_not_limited_by_total_time() -> None:
    """UT-06-8 长生成不受总时长限制：总时长约 0.6s > timeout 0.2s，但事件间隔均在预算内 → 成功。"""
    adapter = scripted_adapter(
        [(chr(97 + i), 0.15) for i in range(5)],
        usage=LlmUsage(prompt_tokens=1, completion_tokens=2),
    )
    adapter.timeout_seconds = 0.2
    result = await adapter.complete(REQ)
    assert result.content == "abcde"
    assert result.usage == LlmUsage(prompt_tokens=1, completion_tokens=2)


async def test_idle_timeout_mid_stream_no_retry() -> None:
    """UT-06-9 中途空闲超时：已产出内容后等不到后续数据 → timeout 且不可重试（避免重复生成）。"""
    adapter = scripted_adapter([("a", 0.0), ("b", 1.0)], max_retries=3)
    adapter.timeout_seconds = 0.2
    with pytest.raises(AdapterError) as excinfo:
        await adapter.complete(REQ)
    assert excinfo.value.kind == "timeout"
    assert excinfo.value.retryable is False
    assert adapter.attempts == 1
