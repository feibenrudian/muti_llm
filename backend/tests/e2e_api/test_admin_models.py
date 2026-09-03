"""AE-09-1..3：Model 管理 API + 连通性测试。"""

import httpx

from tests.helpers import seed_provider_and_model


async def test_model_crud(asgi_client: httpx.AsyncClient) -> None:
    """AE-09-1 CRUD：创建绑定 provider_id 的模型，列表/更新/删除正常。"""
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={"name": "P", "protocol": "openai_compatible", "base_url": "http://127.0.0.1:9/v1"},
    )
    pid = resp.json()["id"]

    resp = await asgi_client.post(
        "/api/admin/models",
        json={
            "provider_id": pid,
            "display_name": "flash",
            "upstream_model_id": "deepseek-v4-flash",
            "default_params": {"temperature": 0.3, "timeout_seconds": 30},
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["provider_id"] == pid
    assert data["default_params"]["temperature"] == 0.3

    assert (await asgi_client.get("/api/admin/models")).json()[0]["display_name"] == "flash"

    resp = await asgi_client.patch(
        f"/api/admin/models/{data['id']}", json={"display_name": "flash2"}
    )
    assert resp.json()["display_name"] == "flash2"

    assert (await asgi_client.delete(f"/api/admin/models/{data['id']}")).status_code == 204
    assert (await asgi_client.get("/api/admin/models")).json() == []

    # 绑定不存在的 provider → 422
    resp = await asgi_client.post(
        "/api/admin/models",
        json={"provider_id": 9999, "display_name": "x", "upstream_model_id": "y"},
    )
    assert resp.status_code == 422


async def test_connectivity_ok(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-09-2 连通性成功：指向快照回放服务器 → ok:true，且 SRS 录像收到该测试请求。"""
    _, model_id = await seed_provider_and_model(asgi_client, srs_live_seeded)

    resp = await asgi_client.post(f"/api/admin/models/{model_id}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["latency_ms"] >= 0
    assert data["content"] == "pong"  # model_test_msg 快照的固化回复

    recordings = (
        await httpx.AsyncClient().get(
            f"{srs_live_seeded}/_test/requests", params={"model": "deepseek-v4-flash"}
        )
    ).json()["requests"]
    assert len(recordings) >= 1
    assert recordings[-1]["body"]["messages"][0]["content"] == "连通性测试：请只回复 pong"


async def test_connectivity_fail(asgi_client: httpx.AsyncClient) -> None:
    """AE-09-3 连通性失败：指向坏地址 → ok:false + error，HTTP 仍为 200。"""
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": "dead",
            "protocol": "openai_compatible",
            # 127.0.0.1:9 保留端口，连接必失败
            "base_url": "http://127.0.0.1:9/v1",
        },
    )
    pid = resp.json()["id"]
    resp = await asgi_client.post(
        "/api/admin/models",
        json={"provider_id": pid, "display_name": "dead", "upstream_model_id": "whatever"},
    )
    mid = resp.json()["id"]

    resp = await asgi_client.post(f"/api/admin/models/{mid}/test")
    assert resp.status_code == 200  # 业务结果而非异常
    data = resp.json()
    assert data["ok"] is False
    assert data["error"]
