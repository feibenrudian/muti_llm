"""AE-10-1..3：网关骨架（认证 / model 解析 / /v1/models）。"""

import httpx

from app.orm import Pipeline
from app.repos import Repository
from tests.conftest import auth_headers
from tests.helpers import seed_provider_and_model


async def _seed_pipeline(
    asgi_client: httpx.AsyncClient, judge_model_id: int, name: str = "council-v1"
) -> None:
    from app.main import app

    async with app.state.session_factory() as session:
        await Repository(session, Pipeline).create(
            name=name, strategy="council", judge_model_id=judge_model_id
        )
        await session.commit()


async def test_auth(asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str) -> None:
    """AE-10-1 认证：无/错 Key → 401 OpenAI error 结构；正确 Key → 非 401。"""
    body = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "连通性测试：请只回复 pong"}],
        "temperature": 0.0,
    }
    url = "/v1/chat/completions"

    resp = await asgi_client.post(url, json=body)  # 无 Key
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "invalid_request_error"

    resp = await asgi_client.post(url, json=body, headers=auth_headers("sk-local-wrong"))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"

    # 正确 Key：透传命中（需先建 model 指向 SRS）
    await seed_provider_and_model(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(url, json=body, headers=auth_headers(service_key))
    assert resp.status_code != 401
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "pong"


async def test_model_resolution(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-10-2 model 解析：不存在 → 404 model_not_found；已配置真实模型名 → 透传分支 200。"""
    await seed_provider_and_model(asgi_client, srs_live_seeded)

    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "model_not_found"
    assert "no-such-model" in error["message"]

    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "连通性测试：请只回复 pong"}],
            "temperature": 0.0,
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200  # 命中透传分支

    # 参数校验：messages 缺失 → 400
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={"model": "deepseek-v4-flash"},
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 400

    # 不支持的能力 → 400
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function"}],
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 400
    assert "不支持的能力" in resp.json()["error"]["message"]


async def test_models_list(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-10-3 models 列表：种子 1 个 Pipeline 后列表含该虚拟模型与透传模型。"""
    _, model_id = await seed_provider_and_model(asgi_client, srs_live_seeded)
    await _seed_pipeline(asgi_client, judge_model_id=model_id)

    resp = await asgi_client.get("/v1/models", headers=auth_headers(service_key))
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["object"] == "list"
    ids = [item["id"] for item in payload["data"]]
    assert "council-v1" in ids  # Pipeline 虚拟模型
    assert "deepseek-v4-flash" in ids  # 透传真实模型
    assert all(item["object"] == "model" for item in payload["data"])

    # Pipeline 命中策略分支（该 pipeline 未配成员 → 策略执行失败 502，而非 404 透传兜底）
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={"model": "council-v1", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 502
    assert "策略执行失败" in resp.json()["error"]["message"]
