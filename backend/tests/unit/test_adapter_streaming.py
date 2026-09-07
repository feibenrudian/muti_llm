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
    script: list[tuple[str, float] | tuple[str, float, str]],
    *,
    usage: LlmUsage | None = None,
    max_retries: int = 0,
) -> BaseAdapter:
    """按脚本逐事件产出；条目为 (text, delay) 或 (text, delay, kind)，kind=text|reasoning。

    usage 事件（若有）附在流末尾。
    """

    class Scripted(BaseAdapter):
        def __init__(self) -> None:
            self.attempts = 0

        def stream_events(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
            self.attempts += 1
            return self._gen()

        async def probe(self) -> list[str]:  # pragma: no cover - 本用例不探测
            raise NotImplementedError

        async def _gen(self) -> AsyncIterator[StreamEvent]:
            for entry in script:
                text, delay = entry[0], entry[1]
                kind = entry[2] if len(entry) > 2 else "text"
                if delay:
                    await asyncio.sleep(delay)
                if kind == "reasoning":
                    yield StreamEvent(reasoning=text)
                else:
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


async def test_reasoning_phase_keeps_stream_alive() -> None:
    """UT-06-10 思考阶段不算超时：reasoning 事件持续到达（总时长 > timeout）→ 成功且不进正文。"""
    adapter = scripted_adapter(
        [(f"思{i}", 0.1, "reasoning") for i in range(6)] + [("答案", 0.1)],
        usage=LlmUsage(prompt_tokens=1, completion_tokens=2),
    )
    adapter.timeout_seconds = 0.3
    result = await adapter.complete(REQ)
    assert result.content == "答案"  # reasoning 绝不混入正文
    assert result.usage == LlmUsage(prompt_tokens=1, completion_tokens=2)


async def test_reasoning_stream_yields_no_text() -> None:
    """UT-06-11 纯思考流：stream() 对 reasoning 事件不产出任何文本片段。"""
    adapter = scripted_adapter([("思考中", 0.0, "reasoning"), ("正文", 0.0)])
    deltas = [d async for d in adapter.stream(REQ)]
    assert deltas == ["正文"]
