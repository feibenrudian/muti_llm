"""AE-08-1..4：Provider 管理 API。"""

import httpx

from app.orm import Provider
from app.repos import Repository
from app.security import decrypt_secret


async def _raw_provider(asgi_client: httpx.AsyncClient, provider_id: int) -> Provider:
    """用例内直接查库断言（tech-plan AE-08-1）。"""
    from app.main import app

    factory = app.state.session_factory
    async with factory() as session:
        return await Repository(session, Provider).get(provider_id)


async def test_create_and_get(asgi_client: httpx.AsyncClient) -> None:
    """AE-08-1 创建+查询：201；响应含掩码 Key；DB 中存的是密文。"""
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": "DeepSeek",
            "protocol": "openai_compatible",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-plain-979fbfa0",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["api_key_masked"] == "****bfa0"  # 只回显尾 4 位，非明文
    assert "sk-plain" not in resp.text

    row = await _raw_provider(asgi_client, data["id"])
    assert row is not None
    assert row.api_key_encrypted != "sk-plain-979fbfa0"  # DB 中非明文
    from app.main import app

    assert decrypt_secret(row.api_key_encrypted, app.state.fernet_key) == "sk-plain-979fbfa0"

    got = await asgi_client.get(f"/api/admin/providers/{data['id']}")
    assert got.status_code == 200
    assert got.json()["name"] == "DeepSeek"


async def test_update(asgi_client: httpx.AsyncClient) -> None:
    """AE-08-2 更新：改 base_url 后 GET 返回新值。"""
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={"name": "P1", "protocol": "anthropic", "base_url": "https://old.example.com"},
    )
    pid = resp.json()["id"]
    resp = await asgi_client.patch(
        f"/api/admin/providers/{pid}", json={"base_url": "https://new.example.com"}
    )
    assert resp.status_code == 200
    assert resp.json()["base_url"] == "https://new.example.com"
    got = await asgi_client.get(f"/api/admin/providers/{pid}")
    assert got.json()["base_url"] == "https://new.example.com"


async def test_delete_protected(asgi_client: httpx.AsyncClient) -> None:
    """AE-08-3 删除保护：Provider 下仍有 Model 时 → 409 并提示。"""
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={"name": "P2", "protocol": "openai_compatible", "base_url": "https://x.example.com"},
    )
    pid = resp.json()["id"]
    resp = await asgi_client.post(
        "/api/admin/models",
        json={"provider_id": pid, "display_name": "m", "upstream_model_id": "m-1"},
    )
    assert resp.status_code == 201

    resp = await asgi_client.delete(f"/api/admin/providers/{pid}")
    assert resp.status_code == 409
    assert "模型" in resp.json()["detail"]

    # 删掉模型后可删除
    mid = (await asgi_client.get("/api/admin/models")).json()[0]["id"]
    assert (await asgi_client.delete(f"/api/admin/models/{mid}")).status_code == 204
    assert (await asgi_client.delete(f"/api/admin/providers/{pid}")).status_code == 204


async def test_protocol_validation(asgi_client: httpx.AsyncClient) -> None:
    """AE-08-4 校验：非法 protocol 枚举 → 422。"""
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={"name": "Bad", "protocol": "grpc", "base_url": "https://x.example.com"},
    )
    assert resp.status_code == 422


async def test_connectivity_ok(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-08-5 连通性成功：指向 SRS → ok:true + 模型列表；SRS 收到带认证的探测请求。"""
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": "probe-ok",
            "protocol": "openai_compatible",
            "base_url": f"{srs_live_seeded}/v1",
            "api_key": "sk-srs-test",
        },
    )
    pid = resp.json()["id"]

    resp = await asgi_client.post(f"/api/admin/providers/{pid}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["latency_ms"] >= 0
    assert "deepseek-v4-flash" in data["models"]

    recordings = (await httpx.AsyncClient().get(f"{srs_live_seeded}/_test/requests")).json()[
        "requests"
    ]
    probes = [r for r in recordings if r["model"] == "__models_list__"]
    assert probes and probes[-1]["body"]["has_authorization"] is True  # Key 确实被发送


async def test_connectivity_dead_port(asgi_client: httpx.AsyncClient) -> None:
    """AE-08-6 连通性失败：坏地址 → ok:false + error，HTTP 仍为 200（业务结果而非异常）。"""
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": "probe-dead",
            "protocol": "openai_compatible",
            # 127.0.0.1:9 保留端口，连接必失败
            "base_url": "http://127.0.0.1:9/v1",
        },
    )
    pid = resp.json()["id"]

    resp = await asgi_client.post(f"/api/admin/providers/{pid}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["error"]


async def test_connectivity_bad_key(asgi_client: httpx.AsyncClient, srs_live: str) -> None:
    """AE-08-7 错误 Key：上游 401 → ok:false，error 指向认证失败。"""
    await httpx.AsyncClient().post(f"{srs_live}/_test/config", json={"models_auth_fail": True})
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": "probe-bad-key",
            "protocol": "openai_compatible",
            "base_url": f"{srs_live}/v1",
            "api_key": "sk-wrong-key",
        },
    )
    pid = resp.json()["id"]

    resp = await asgi_client.post(f"/api/admin/providers/{pid}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "认证" in data["error"]
