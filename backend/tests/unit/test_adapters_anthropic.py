"""UT-07-1..3：Anthropic 适配器（走本地 mock 服务的真实 HTTP，见 tests/mock_anthropic.py）。"""

import httpx
import pytest

from app.adapters.anthropic_adapter import AnthropicAdapter
from app.adapters.base import AdapterError, LlmRequest, NormalizedMessage
from tests.mock_anthropic import build_sse_events


def make_adapter(base_url: str, **kwargs: object) -> AnthropicAdapter:
    return AnthropicAdapter(base_url=base_url, api_key="sk-ant-test", retry_base_delay=0, **kwargs)


def make_request() -> LlmRequest:
    return LlmRequest(
        model="claude-sonnet-4",
        messages=[
            NormalizedMessage(role="system", content="你是严谨的助手"),
            NormalizedMessage(role="user", content="你好"),
        ],
        temperature=0.5,
        max_tokens=128,
    )


async def recorded_requests(base_url: str) -> list[dict]:
    resp = await httpx.AsyncClient().get(f"{base_url}/_test/requests")
    assert resp.status_code == 200
    return resp.json()["requests"]


async def test_request_and_response_mapping(anthropic_mock: str) -> None:
    """UT-07-1 参数/响应映射：system 提升、max_tokens 必填、流式 usage 归一化。"""
    adapter = make_adapter(anthropic_mock)
    result = await adapter.complete(make_request())

    recordings = await recorded_requests(anthropic_mock)
    assert len(recordings) == 1
    sent = recordings[0]["body"]
    headers = recordings[0]["headers"]

    assert sent["model"] == "claude-sonnet-4"
    assert sent["max_tokens"] == 128
    assert sent["system"] == "你是严谨的助手"
    assert sent["messages"] == [{"role": "user", "content": "你好"}]  # system 不在 messages
    assert sent["stream"] is True  # 上游一律流式调用（决策 D8）
    assert headers.get("x-api-key") == "sk-ant-test"
    assert "temperature" not in sent  # 新版 API 已移除采样参数，适配器静默忽略

    # 流式聚合：默认 SSE 事件文本 + message_start/message_delta 的 usage 累加
    assert result.content == "你好，世界"
    assert result.usage.prompt_tokens == 5
    assert result.usage.completion_tokens == 2
    assert result.usage.total_tokens == 7


async def test_stream_event_conversion(anthropic_mock: str) -> None:
    """UT-07-2 流式事件转换：content_block_delta 事件序列 → 增量片段序列。"""
    resp = await httpx.AsyncClient().post(
        f"{anthropic_mock}/_test/config",
        json={"sse_events": build_sse_events(["你好", "，世界"])},
    )
    assert resp.status_code == 200

    adapter = make_adapter(anthropic_mock)
    deltas = [d async for d in adapter.stream(make_request())]
    assert deltas == ["你好", "，世界"]


async def test_usage_cache_fields_normalized(anthropic_mock: str) -> None:
    """UT-41-3 缓存归一化：prompt = input + 缓存读 + 缓存写；无缓存字段时行为不变。"""
    resp = await httpx.AsyncClient().post(
        f"{anthropic_mock}/_test/config",
        json={
            "sse_events": build_sse_events(
                ["你好"], cache_read_input_tokens=200, cache_creation_input_tokens=50
            )
        },
    )
    assert resp.status_code == 200

    adapter = make_adapter(anthropic_mock)
    result = await adapter.complete(make_request())
    assert result.usage.prompt_tokens == 5 + 200 + 50  # input_tokens 不含缓存，归一化补和
    assert result.usage.cached_tokens == 200
    assert result.usage.cache_write_tokens == 50
    assert result.usage.completion_tokens == 2

    # 默认 SSE 无缓存字段：prompt 保持 input 原值（既有语义不回归）
    await httpx.AsyncClient().post(f"{anthropic_mock}/_test/config", json={"reset": True})
    plain = await adapter.complete(make_request())
    assert plain.usage.prompt_tokens == 5
    assert plain.usage.cached_tokens == 0
    assert plain.usage.cache_write_tokens == 0


async def test_reuses_base_retry(anthropic_mock: str) -> None:
    """UT-07-3 复用基座：anthropic 5xx 映射为可重试错误，基座重试后成功；auth 不重试。"""
    # 5xx 一次后成功
    await httpx.AsyncClient().post(f"{anthropic_mock}/_test/config", json={"fail_times": 1})
    adapter = make_adapter(anthropic_mock, max_retries=1)
    result = await adapter.complete(make_request())
    assert result.content == "你好，世界"
    assert len(await recorded_requests(anthropic_mock)) == 2  # 基座确实重试了一次

    # auth 错误不可重试，直接抛出
    await httpx.AsyncClient().post(
        f"{anthropic_mock}/_test/config", json={"fail_times": 3, "fail_status": 401}
    )
    adapter2 = make_adapter(anthropic_mock, max_retries=2)
    with pytest.raises(AdapterError) as excinfo:
        await adapter2.complete(make_request())
    assert excinfo.value.kind == "auth"
    assert (
        len(await recorded_requests(anthropic_mock)) == 3
    )  # 未重试（1 次成功 + 1 次 500 + 1 次 401）


async def test_probe(anthropic_mock: str) -> None:
    """UT-07-4 连通性探测：probe 返回 mock 的模型列表，请求携带 x-api-key。"""
    adapter = make_adapter(anthropic_mock)
    model_ids = await adapter.probe()
    assert model_ids == ["claude-sonnet-4"]

    recordings = await recorded_requests(anthropic_mock)
    probe = [r for r in recordings if r["body"].get("path") == "/v1/models"][-1]
    assert probe["headers"].get("x-api-key") == "sk-ant-test"


async def test_thinking_delta_maps_to_reasoning_event(anthropic_mock: str) -> None:
    """UT-07-5 思考增量映射：thinking_delta → reasoning 事件，不进正文；text 块正常聚合。"""
    resp = await httpx.AsyncClient().post(
        f"{anthropic_mock}/_test/config",
        json={"sse_events": build_sse_events(["你好"], thinking=["先想想", "再想想"])},
    )
    assert resp.status_code == 200

    adapter = make_adapter(anthropic_mock)
    events = [event async for event in adapter.stream_events_timed(make_request())]
    assert "".join(e.reasoning for e in events if e.reasoning) == "先想想再想想"
    assert "".join(e.text for e in events) == "你好"

    result = await adapter.complete(make_request())
    assert result.content == "你好"  # 思考内容绝不混入正文
