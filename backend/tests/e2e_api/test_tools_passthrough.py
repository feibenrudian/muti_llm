"""T43 工具透传：AE-43-1..5（空值容忍 / tool_calls 透传非流式+流式 / 聚合拒绝 / 协议闸门）。"""

import json

import httpx

from tests.conftest import auth_headers
from tests.helpers import seed_provider_and_model
from tests.record_scenarios import TOOL_QUESTION, TOOL_WEATHER  # noqa: I001


async def _seed_srs_provider(asgi_client: httpx.AsyncClient, srs: str) -> None:
    await seed_provider_and_model(asgi_client, srs)


TOOL_BODY = {
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": TOOL_QUESTION}],
    "temperature": 0.0,
    "tools": TOOL_WEATHER,
    "tool_choice": "auto",
}

# 客户端默认携带的"关闭态"字段：忽略后上游载荷与既有 pong 快照一致
OFF_STATE_FIELDS = {
    "tools": [],
    "tool_choice": "auto",
    "functions": [],
    "function_call": "none",
    "logprobs": False,
    "logit_bias": {},
}


async def test_off_state_fields_ignored(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-43-1 空值容忍：tools=[]/tool_choice=auto/logprobs=false 等默认携带 → 照常透传。"""
    await _seed_srs_provider(asgi_client, srs_live_seeded)
    body = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "连通性测试：请只回复 pong"}],
        "temperature": 0.0,
        **OFF_STATE_FIELDS,
    }
    resp = await asgi_client.post(
        "/v1/chat/completions", json=body, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "pong"
    assert resp.json()["choices"][0]["finish_reason"] == "stop"


async def test_passthrough_tool_calls_non_stream(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-43-2 非流式工具透传：message.tool_calls 原样返回、finish_reason=tool_calls、落库。"""
    await _seed_srs_provider(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions", json=TOOL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    choice = payload["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    tool_calls = choice["message"]["tool_calls"]
    assert tool_calls and tool_calls[0]["function"]["name"] == "get_weather"
    assert "广州" in tool_calls[0]["function"]["arguments"]
    assert payload["usage"]["total_tokens"] > 0

    # 调用行落库：response_tool_calls 聚合形态、请求级 finish_reason 透传
    from sqlalchemy import select

    from app.main import app
    from app.orm import ModelCallLog

    async with app.state.session_factory() as session:
        logs = (
            (await session.execute(select(ModelCallLog).where(ModelCallLog.role == "passthrough")))
            .scalars()
            .all()
        )
    assert logs and logs[-1].response_tool_calls is not None
    assert logs[-1].response_tool_calls[0]["function"]["name"] == "get_weather"

    # route 命名空间同路径：同一快照，行为一致
    slug = (await asgi_client.get("/api/admin/providers")).json()[0]["slug"]
    resp = await asgi_client.post(
        f"/v1/route/{slug}/chat/completions", json=TOOL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["finish_reason"] == "tool_calls"


async def test_passthrough_tool_calls_stream(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-43-3 流式工具透传：delta.tool_calls 分片原样下发，终止块 finish_reason 透传。"""
    await _seed_srs_provider(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={**TOOL_BODY, "stream": True},
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    deltas: list[dict] = []
    finish_reason = None
    saw_done = False
    async for line in resp.aiter_lines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            saw_done = saw_done or line == "data: [DONE]"
            continue
        chunk = json.loads(line[len("data: ") :])
        choice = (chunk.get("choices") or [{}])[0]
        if choice.get("delta", {}).get("tool_calls"):
            deltas.extend(choice["delta"]["tool_calls"])
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
    assert saw_done
    assert deltas, "流内应有 tool_calls 分片"
    assert any(f.get("function", {}).get("name") == "get_weather" for f in deltas)
    assert finish_reason == "tool_calls"


async def test_pipeline_rejects_tools(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-43-4 聚合分支：非空 tools → 400 明确报错；空 tools 放行进策略分支。"""
    _, model_id = await seed_provider_and_model(asgi_client, srs_live_seeded)
    from app.main import app
    from app.orm import Pipeline
    from app.repos import Repository

    async with app.state.session_factory() as session:
        await Repository(session, Pipeline).create(
            name="council-v1", strategy="council", judge_model_id=model_id
        )
        await session.commit()

    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": "council-v1",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": TOOL_WEATHER,
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 400
    assert "暂不支持工具调用" in resp.json()["error"]["message"]

    # 空 tools 无语义 → 不拦截，照常命中策略分支（无成员 → 502）
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": "council-v1",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [],
            "tool_choice": "auto",
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 502
    assert "策略执行失败" in resp.json()["error"]["message"]


async def test_anthropic_provider_rejects_tools(
    asgi_client: httpx.AsyncClient, service_key: str, anthropic_mock: str
) -> None:
    """AE-43-5 协议闸门：anthropic 供应商 + 非空 tools → 适配器层明确拒绝，不打上游。"""
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": "anthropic-mock",
            "protocol": "anthropic",
            "base_url": anthropic_mock,
            "api_key": "sk-ant-test",
        },
    )
    assert resp.status_code == 201, resp.text
    provider_id = resp.json()["id"]
    resp = await asgi_client.post(
        "/api/admin/models",
        json={
            "provider_id": provider_id,
            "display_name": "claude-mock",
            "upstream_model_id": "claude-mock",
        },
    )
    assert resp.status_code == 201, resp.text

    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-mock",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": TOOL_WEATHER,
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 502
    assert "anthropic 协议暂不支持工具透传" in resp.json()["error"]["message"]
