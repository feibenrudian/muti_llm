"""UT-06-1..5：OpenAI 兼容适配器（经真实 HTTP 访问挂载真实快照库的 SRS）。"""

import httpx
import pytest

from app.adapters.base import AdapterError, LlmRequest, NormalizedMessage
from app.adapters.openai_compat import OpenAICompatAdapter
from tests.helpers import snapshot_content, snapshot_stream_text, snapshot_usage

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
