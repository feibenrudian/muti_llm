"""UT-14-1..8：成员并发执行器（打桩适配器，不起网络）。"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.adapters.base import (
    AdapterError,
    BaseAdapter,
    LlmRequest,
    LlmUsage,
    StreamEvent,
)
from app.orm import LlmModel, Pipeline, PipelineMember, Provider
from app.strategies.base import AllMembersFailed, MemberSpec, StrategyContext
from app.strategies.council import run_members

MESSAGES = [{"role": "user", "content": "hi"}]


class StubAdapter(BaseAdapter):
    """behavior 是 async generator：产出 StreamEvent（超时/重试由基座统一实现）。"""

    def __init__(self, behavior, *, timeout_seconds: float = 60.0) -> None:
        self.behavior = behavior
        self.requests: list[LlmRequest] = []
        self.timeout_seconds = timeout_seconds

    def stream_events(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        return self.behavior(request)

    async def probe(self) -> list[str]:  # pragma: no cover - 执行器不使用
        raise NotImplementedError


def make_provider(pid: int, name: str) -> Provider:
    return Provider(id=pid, name=name, protocol="openai_compatible", base_url="http://x")


def make_model(mid: int, defaults: dict | None = None) -> LlmModel:
    return LlmModel(
        id=mid,
        provider_id=1,
        display_name=f"m{mid}",
        upstream_model_id=f"model-{mid}",
        default_params=defaults or {},
    )


def make_ctx(
    specs: list[tuple[dict | None, dict | None]],
    *,
    user_params: dict | None = None,
    fault_tolerance: dict | None = None,
    member_timeout: int = 60,
    max_concurrency: int = 10,
) -> StrategyContext:
    pipeline = Pipeline(
        id=1,
        name="council-v1",
        strategy="council",
        judge_model_id=1,
        member_timeout_seconds=member_timeout,
        fault_tolerance=fault_tolerance or {},
        max_concurrency=max_concurrency,
    )
    members = []
    for i, (defaults, overrides) in enumerate(specs):
        model = make_model(i + 1, defaults)
        members.append(
            MemberSpec(
                member=PipelineMember(
                    id=i + 1,
                    pipeline_id=1,
                    model_id=model.id,
                    sort_order=i,
                    param_overrides=overrides or {},
                ),
                model=model,
                provider=make_provider(i + 1, f"P{i + 1}"),
            )
        )
    judge_model = make_model(99)
    return StrategyContext(
        body={"model": "council-v1", "messages": MESSAGES},
        user_params=user_params or {},
        pipeline=pipeline,
        members=members,
        judge_model=judge_model,
        judge_provider=make_provider(99, "judge-p"),
        fernet_key=b"k" * 44,
    )


def ok_result(content: str = "ans", delay: float = 0.0) -> Any:
    async def behavior(request: LlmRequest) -> AsyncIterator[StreamEvent]:
        if delay:
            await asyncio.sleep(delay)
        yield StreamEvent(text=content)
        yield StreamEvent(usage=LlmUsage(prompt_tokens=1, completion_tokens=1))

    return behavior


def fail_result(kind: str = "upstream_error", delay: float = 0.0) -> Any:
    async def behavior(request: LlmRequest) -> AsyncIterator[StreamEvent]:
        if delay:
            await asyncio.sleep(delay)
        raise AdapterError(f"stub failure ({kind})", kind=kind, retryable=False)
        yield StreamEvent(text="")  # pragma: no cover - 使其成为 async generator

    return behavior


async def test_concurrent_execution() -> None:
    """UT-14-1 并发生效：3 个各延迟 200ms 的成员总耗时 < 500ms（串行必 > 600ms）。"""
    ctx = make_ctx([(None, None)] * 3)
    adapters: list[StubAdapter] = []

    def factory(provider, merged):
        adapter = StubAdapter(ok_result(delay=0.2))
        adapters.append(adapter)
        return adapter

    start = time.perf_counter()
    outcomes = await run_members(ctx, adapter_factory=factory)
    elapsed = time.perf_counter() - start

    assert len(outcomes) == 3
    assert all(o.status == "success" for o in outcomes)
    assert elapsed < 0.5, f"members did not run concurrently: {elapsed:.2f}s"


async def test_partial_failure_tolerated() -> None:
    """UT-14-2 部分失败容错：3 成员 1 失败 → 返回 2 个成功结果，无异常。"""
    ctx = make_ctx([(None, None)] * 3)
    behaviors = [ok_result("a"), fail_result(), ok_result("b")]

    def factory(provider, merged):
        return StubAdapter(behaviors[int(provider.name[1:]) - 1])

    outcomes = await run_members(ctx, adapter_factory=factory)
    assert [o.status for o in outcomes] == ["success", "failed", "success"]
    assert outcomes[1].error_message
    assert outcomes[0].response_content == "a"


async def test_all_members_failed() -> None:
    """UT-14-3 全部失败：抛 AllMembersFailed，含各成员错误摘要。"""
    ctx = make_ctx([(None, None)] * 2)

    def factory(provider, merged):
        return StubAdapter(fail_result())

    with pytest.raises(AllMembersFailed) as excinfo:
        await run_members(ctx, adapter_factory=factory)
    message = str(excinfo.value)
    assert "model-1" in message and "model-2" in message
    assert "stub failure" in message


async def test_strict_mode_fails_on_any_member() -> None:
    """UT-14-4 严格模式：容错=strict 时任一失败即整体失败。"""
    ctx = make_ctx([(None, None), (None, None)], fault_tolerance={"member_failure": "strict"})
    behaviors = [ok_result(), fail_result()]

    def factory(provider, merged):
        return StubAdapter(behaviors[int(provider.name[1:]) - 1])

    with pytest.raises(AllMembersFailed):
        await run_members(ctx, adapter_factory=factory)


async def test_member_timeout_marks_timeout() -> None:
    """UT-14-5 单成员超时（TTFT 语义，决策 D8）：首响应超预算 → 标记 timeout，其余正常。"""
    ctx = make_ctx([(None, None), (None, None)], member_timeout=1)

    def factory(provider, merged):
        pid = int(provider.name[1:])
        if pid == 1:
            return StubAdapter(ok_result(delay=5.0), timeout_seconds=1.0)
        return StubAdapter(ok_result("fast"))

    outcomes = await run_members(ctx, adapter_factory=factory)
    assert outcomes[0].status == "timeout"
    assert "超时" in outcomes[0].error_message
    assert outcomes[1].status == "success"


async def test_concurrency_cap_enforced() -> None:
    """UT-14-6 并发上限：max_concurrency=2、4 成员各延迟 100ms → 在飞数 ≤ 2。"""
    ctx = make_ctx([(None, None)] * 4, max_concurrency=2)
    inflight = {"now": 0, "max": 0}

    async def behavior(request: LlmRequest) -> AsyncIterator[StreamEvent]:
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0.1)
        inflight["now"] -= 1
        yield StreamEvent(text="x")

    def factory(provider, merged):
        return StubAdapter(behavior)

    start = time.perf_counter()
    outcomes = await run_members(ctx, adapter_factory=factory)
    elapsed = time.perf_counter() - start

    assert len(outcomes) == 4 and all(o.status == "success" for o in outcomes)
    assert inflight["max"] <= 2, f"concurrency cap violated: {inflight['max']}"
    assert elapsed >= 0.2, "with cap=2 four 100ms tasks need at least two batches"


async def test_param_merge_priority() -> None:
    """UT-14-7 参数合并：成员=模型默认 < 成员覆盖（请求参数不透传给成员，只作用于裁判）。"""
    ctx = make_ctx(
        [
            ({"temperature": 0.1, "max_tokens": 100}, {"temperature": 0.5}),
            ({"temperature": 0.2}, None),
        ],
        user_params={"temperature": 0.9},
    )
    captured: dict[str, LlmRequest] = {}

    class Capturing(StubAdapter):
        def stream_events(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
            captured[self._owner] = request  # type: ignore[attr-defined]
            return super().stream_events(request)

    def factory(provider, merged):
        adapter = Capturing(ok_result())
        adapter._owner = provider.name  # type: ignore[attr-defined]
        return adapter

    await run_members(ctx, adapter_factory=factory)

    # 成员1：默认0.1 被覆盖0.5 → 0.5；请求 0.9 不作用于成员；max_tokens 保留默认 100
    assert captured["P1"].temperature == 0.5
    assert captured["P1"].max_tokens == 100
    # 成员2：无覆盖 → 默认0.2，请求 0.9 同样不影响
    assert captured["P2"].temperature == 0.2
    # payload 同样反映合并结果
    outcomes = await run_members(ctx, adapter_factory=factory)
    assert outcomes[0].request_payload["temperature"] == 0.5
    assert outcomes[0].request_payload["max_tokens"] == 100


async def test_member_payload_is_streaming() -> None:
    """UT-14-8 上游流式调用（决策 D8）：成员 payload stream=true 且带 usage 采集参数。"""
    ctx = make_ctx([(None, None)])

    def factory(provider, merged):
        return StubAdapter(ok_result("x"))

    outcomes = await run_members(ctx, adapter_factory=factory)
    payload = outcomes[0].request_payload
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
