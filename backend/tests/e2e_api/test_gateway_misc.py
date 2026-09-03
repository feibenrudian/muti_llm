"""网关补充用例：参数校验分支、provider 停用、流式上游失败、anthropic 协议透传。"""

import asyncio
import json

import httpx

from app.orm import RequestLog
from app.repos import Repository
from tests.conftest import auth_headers
from tests.helpers import seed_provider_and_model


async def test_invalid_json_body(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """非法 JSON 请求体 → 400。"""
    await seed_provider_and_model(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions",
        content=b"{not valid json",
        headers={**auth_headers(service_key), "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "JSON" in resp.json()["error"]["message"]


async def test_invalid_message_shape(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """message 结构非法（role 不合法/缺 content）→ 400。"""
    await seed_provider_and_model(asgi_client, srs_live_seeded)
    for bad_messages in (
        [{"role": "wizard", "content": "hi"}],  # 非法 role
        [{"role": "user"}],  # 缺 content
        ["just a string"],  # 非 dict
    ):
        resp = await asgi_client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-v4-flash", "messages": bad_messages},
            headers=auth_headers(service_key),
        )
        assert resp.status_code == 400, bad_messages


async def test_model_field_required(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """model 缺失 → 400。"""
    await seed_provider_and_model(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "model"


async def test_provider_disabled(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """provider 停用 → 502 provider 不可用。"""
    provider_id, _ = await seed_provider_and_model(asgi_client, srs_live_seeded)
    resp = await asgi_client.patch(f"/api/admin/providers/{provider_id}", json={"enabled": False})
    assert resp.status_code == 200

    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 502
    assert "不可用" in resp.json()["error"]["message"]


async def test_stream_upstream_failure(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """流式上游失败：SRS 注入失败 → 流终止，日志 status=failed。"""
    await seed_provider_and_model(asgi_client, srs_live_seeded)
    await httpx.AsyncClient().post(
        f"{srs_live_seeded}/_test/config", json={"fail_times": {"deepseek-v4-flash": 5}}
    )
    body = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    async with asgi_client.stream(
        "POST", "/v1/chat/completions", json=body, headers=auth_headers(service_key)
    ) as resp:
        payloads = []
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                payloads.append(line[6:])

    await asyncio.sleep(0.2)
    from app.main import app

    async with app.state.session_factory() as session:
        requests = await Repository(session, RequestLog).list()
        assert requests[-1].status == "failed"
    assert payloads[0] == json.dumps(
        {
            "id": json.loads(payloads[0])["id"],
            "object": "chat.completion.chunk",
            "created": json.loads(payloads[0])["created"],
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    )  # 只收到了首个 role chunk，无 [DONE]


async def test_anthropic_passthrough(
    asgi_client: httpx.AsyncClient, service_key: str, anthropic_mock: str
) -> None:
    """anthropic 协议全链路透传：OpenAI 请求 → AnthropicAdapter → mock /v1/messages。"""
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": "anthropic-mock",
            "protocol": "anthropic",
            "base_url": anthropic_mock,
            "api_key": "sk-ant-test",
        },
    )
    assert resp.status_code == 201
    provider_id = resp.json()["id"]
    resp = await asgi_client.post(
        "/api/admin/models",
        json={
            "provider_id": provider_id,
            "display_name": "claude",
            "upstream_model_id": "claude-sonnet-4",
        },
    )
    assert resp.status_code == 201

    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "system", "content": "你是严谨的助手"},
                {"role": "user", "content": "你好"},
            ],
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "ANSWER"
    assert data["usage"]["prompt_tokens"] == 7  # anthropic input_tokens 归一化
    assert data["usage"]["completion_tokens"] == 3

    recordings = (await httpx.AsyncClient().get(f"{anthropic_mock}/_test/requests")).json()[
        "requests"
    ]
    assert recordings[-1]["body"]["system"] == "你是严谨的助手"
    assert recordings[-1]["body"]["messages"] == [{"role": "user", "content": "你好"}]
