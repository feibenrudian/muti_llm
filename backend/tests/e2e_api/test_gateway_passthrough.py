"""AE-11-1..4：透传调用（非流式）+ 调用日志。"""

import httpx

from app.orm import ModelCallLog, RequestLog
from app.repos import Repository
from tests.conftest import auth_headers
from tests.helpers import seed_provider_and_model, snapshot_content, snapshot_usage

QUANTUM = "用一句话解释量子纠缠"


async def _latest_trace() -> tuple[RequestLog, list[ModelCallLog]]:
    from app.main import app

    async with app.state.session_factory() as session:
        requests = await Repository(session, RequestLog).list()
        trace = requests[-1]
        from app.repos import list_model_calls

        calls = await list_model_calls(session, trace.id)
        return trace, calls


async def test_passthrough_end_to_end(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-11-1 端到端：响应 content/usage 与快照一致，结构兼容 OpenAI。"""
    await seed_provider_and_model(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": QUANTUM}],
            "temperature": 0.7,
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["object"] == "chat.completion"
    assert data["model"] == "deepseek-v4-flash"
    choice = data["choices"][0]
    assert choice["index"] == 0
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == snapshot_content("passthrough_basic")
    assert choice["finish_reason"] == "stop"
    usage = snapshot_usage("passthrough_basic")
    assert data["usage"]["prompt_tokens"] == usage["prompt_tokens"]
    assert data["usage"]["completion_tokens"] == usage["completion_tokens"]
    assert data["usage"]["total_tokens"] == usage["total_tokens"]


async def test_param_merge_priority(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-11-2 参数合并优先级：默认 0.1、请求 0.9 → SRS 录像中 temperature=0.9。"""
    await seed_provider_and_model(asgi_client, srs_live_seeded, default_params={"temperature": 0.1})
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": QUANTUM}],
            "temperature": 0.9,
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200  # 命中 param_merge_09 快照（0.9 请求形态）

    recordings = (
        await httpx.AsyncClient().get(
            f"{srs_live_seeded}/_test/requests", params={"model": "deepseek-v4-flash"}
        )
    ).json()["requests"]
    assert recordings[-1]["body"]["temperature"] == 0.9


async def test_logging_persists(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-11-3 日志落库：1 条 request_logs(success) + 1 条 model_call_logs(含全文)。"""
    await seed_provider_and_model(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": QUANTUM}],
            "temperature": 0.7,
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200

    trace, calls = await _latest_trace()
    assert trace.status == "success"
    assert trace.pipeline_name == ""
    assert trace.client_model_field == "deepseek-v4-flash"
    assert trace.request_messages == [{"role": "user", "content": QUANTUM}]
    assert trace.response_content == resp.json()["choices"][0]["message"]["content"]
    assert trace.total_duration_ms > 0
    assert trace.total_prompt_tokens > 0
    assert trace.total_completion_tokens > 0

    assert len(calls) == 1
    call = calls[0]
    assert call.role == "passthrough"
    assert call.provider_name == "srs-provider"
    assert call.status == "success"
    assert call.request_payload["model"] == "deepseek-v4-flash"
    assert call.request_payload["temperature"] == 0.7
    assert call.request_payload["messages"] == [{"role": "user", "content": QUANTUM}]
    assert call.response_content == resp.json()["choices"][0]["message"]["content"]


async def test_upstream_failure(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-11-4 上游失败：502 + OpenAI error；日志 failed 且 error_message 非空。"""
    await seed_provider_and_model(asgi_client, srs_live_seeded)
    injected = await httpx.AsyncClient().post(
        f"{srs_live_seeded}/_test/config", json={"fail_times": {"deepseek-v4-flash": 3}}
    )
    assert injected.status_code == 200

    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": QUANTUM}],
            "temperature": 0.7,
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_error"

    trace, calls = await _latest_trace()
    assert trace.status == "failed"
    assert calls[0].status == "failed"
    assert calls[0].error_message
