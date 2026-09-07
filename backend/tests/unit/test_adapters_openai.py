"""UT-06-1..13：OpenAI 兼容适配器（经真实 HTTP 访问挂载真实快照库的 SRS）。"""

from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.adapters.base import AdapterError, LlmRequest, NormalizedMessage, StreamEvent
from app.adapters.openai_compat import OpenAICompatAdapter
from tests.helpers import snapshot_content, snapshot_stream_text, snapshot_usage
from tests.snapshot_server.fixtures import _run_srs
from tests.snapshot_server.store import Snapshot, SnapshotStore

QUANTUM = "用一句话解释量子纠缠"
POEM = "写一首关于秋天的四行短诗，每行不超过10个字"


def make_adapter(srs_base_url: str, **kwargs: object) -> OpenAICompatAdapter:
    return OpenAICompatAdapter(
        base_url=f"{srs_base_url}/v1", api_key="sk-srs-test", retry_base_delay=0, **kwargs
    )


async def _inject(srs_base_url: str, config: dict) -> None:
    resp = await httpx.AsyncClient().post(f"{srs_base_url}/_test/config", json=config)
    assert resp.status_code == 200


async def test_basic_call(srs_live_seeded: str) -> None:
    """UT-06-1 基本调用：返回快照的 content 与 usage，耗时字段>=0。"""
    adapter = make_adapter(srs_live_seeded)
    result = await adapter.complete(
        LlmRequest(
            model="deepseek-v4-flash",
            messages=[NormalizedMessage(role="user", content=QUANTUM)],
            temperature=0.7,
        )
    )
    snap_content = snapshot_content("passthrough_basic")
    snap_usage = snapshot_usage("passthrough_basic")
    assert result.content == snap_content
    assert result.usage.prompt_tokens == snap_usage["prompt_tokens"]
    assert result.usage.completion_tokens == snap_usage["completion_tokens"]
    assert result.duration_ms >= 0


async def test_retry_then_success(srs_live_seeded: str) -> None:
    """UT-06-2 重试成功：上游失败1次后成功（重试默认开启）→ 结果成功。"""
    await _inject(srs_live_seeded, {"fail_times": {"deepseek-v4-flash": 1}})
    adapter = make_adapter(srs_live_seeded, max_retries=1)
    result = await adapter.complete(
        LlmRequest(
            model="deepseek-v4-flash",
            messages=[NormalizedMessage(role="user", content=QUANTUM)],
            temperature=0.7,
        )
    )
    snap_content = snapshot_content("passthrough_basic")
    assert result.content == snap_content


async def test_retry_exhausted(srs_live_seeded: str) -> None:
    """UT-06-3 重试耗尽：连续失败 → 抛含上游错误摘要的 AdapterError，不裸抛 SDK 异常。"""
    await _inject(srs_live_seeded, {"fail_times": {"deepseek-v4-flash": 5}})
    adapter = make_adapter(srs_live_seeded, max_retries=1)
    with pytest.raises(AdapterError) as excinfo:
        await adapter.complete(
            LlmRequest(
                model="deepseek-v4-flash",
                messages=[NormalizedMessage(role="user", content=QUANTUM)],
                temperature=0.7,
            )
        )
    err = excinfo.value
    assert err.kind == "upstream_error"
    assert "500" in str(err) or "deepseek-v4-flash" in str(err)  # 上游错误摘要
    assert err.detail


async def test_timeout(srs_live_seeded: str) -> None:
    """UT-06-4 超时：timeout=0.2s、上游延迟1s → 抛超时错误。"""
    await _inject(srs_live_seeded, {"timeout_ms": {"deepseek-v4-flash": 1000}})
    adapter = make_adapter(srs_live_seeded, timeout_seconds=0.2, max_retries=0)
    with pytest.raises(AdapterError) as excinfo:
        await adapter.complete(
            LlmRequest(
                model="deepseek-v4-flash",
                messages=[NormalizedMessage(role="user", content=QUANTUM)],
                temperature=0.7,
            )
        )
    assert excinfo.value.kind == "timeout"


async def test_stream_iteration(srs_live_seeded: str) -> None:
    """UT-06-5 流式迭代：多个增量片段，拼接等于完整答案。"""
    adapter = make_adapter(srs_live_seeded)
    deltas = [
        delta
        async for delta in adapter.stream(
            LlmRequest(
                model="deepseek-v4-flash",
                messages=[NormalizedMessage(role="user", content=POEM)],
                temperature=0.7,
                max_tokens=2000,
            )
        )
    ]
    assert len(deltas) >= 2
    assert "".join(deltas) == snapshot_stream_text("passthrough_stream")


async def test_probe(srs_live_seeded: str) -> None:
    """UT-06-6 连通性探测：probe 返回快照库中的模型 ID；上游 401 → 归一化认证错误。"""
    adapter = make_adapter(srs_live_seeded)
    model_ids = await adapter.probe()
    assert "deepseek-v4-flash" in model_ids

    await _inject(srs_live_seeded, {"models_auth_fail": True})
    with pytest.raises(AdapterError) as excinfo:
        await adapter.probe()
    assert excinfo.value.kind == "auth"


# ---- 思考型上游（reasoning_content）：合成快照回放，正文与思考分离 ----
# 请求体须与适配器实际发送的逐字段一致，否则 request_hash 失配（SRS 回放纪律）
THINKING_REQUEST: dict[str, Any] = {
    "model": "mock-thinking",
    "messages": [{"role": "user", "content": "想一想再回答"}],
    "stream": True,
    "stream_options": {"include_usage": True},
}


def _thinking_chunk(delta: dict[str, Any], *, delay_ms: int) -> dict[str, Any]:
    return {
        "chunk": {
            "id": "chatcmpl-thinking",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "mock-thinking",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        },
        "delay_ms": delay_ms,
    }


# 思考增量每 100ms 一个、共 600ms（超过用例注入的 0.3s 超时），随后才出正文
THINKING_CHUNKS: list[dict[str, Any]] = [
    _thinking_chunk({"reasoning_content": f"思{i}"}, delay_ms=100) for i in range(6)
] + [
    _thinking_chunk({"content": "答案A"}, delay_ms=100),
    _thinking_chunk({"content": "完毕B"}, delay_ms=0),
    {
        "chunk": {
            "id": "chatcmpl-thinking",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "mock-thinking",
            "choices": [],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        },
        "delay_ms": 0,
    },
]


@pytest.fixture
def srs_thinking(tmp_path: Path) -> Generator[str, None, None]:
    """线程起挂载"思考流"合成快照的 SRS（写入 tmp 目录，不动真实录制库）。"""
    root = tmp_path / "snapshots"
    snap = Snapshot(
        scenario="thinking_stream", request=THINKING_REQUEST, stream_chunks=THINKING_CHUNKS
    )
    SnapshotStore(root).save(snap)
    yield from _run_srs(root)


async def test_reasoning_phase_no_false_timeout(srs_thinking: str) -> None:
    """UT-06-12 思考阶段不误杀：reasoning 增量持续 0.6s（> timeout 0.3s）→ 不超时、正文纯净。"""
    adapter = make_adapter(srs_thinking, timeout_seconds=0.3)
    result = await adapter.complete(
        LlmRequest(
            model="mock-thinking",
            messages=[NormalizedMessage(role="user", content="想一想再回答")],
        )
    )
    assert result.content == "答案A完毕B"  # 思考内容不进正文
    assert result.usage.prompt_tokens == 3
    assert result.usage.completion_tokens == 5


async def test_reasoning_events_surface(srs_thinking: str) -> None:
    """UT-06-13 思考增量暴露为 reasoning 事件：text 为空，reasoning 拼接完整、只含 content 正文。"""
    adapter = make_adapter(srs_thinking)
    events: list[StreamEvent] = [
        event
        async for event in adapter.stream_events_timed(
            LlmRequest(
                model="mock-thinking",
                messages=[NormalizedMessage(role="user", content="想一想再回答")],
            )
        )
    ]
    assert "".join(e.reasoning for e in events if e.reasoning) == "思0思1思2思3思4思5"
    assert "".join(e.text for e in events) == "答案A完毕B"
