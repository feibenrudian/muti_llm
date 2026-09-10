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
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "sk-plain-979fbfa0",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["api_key_masked"] == "****bfa0"  # 只回显尾 4 位，非明文
    assert "sk-plain" not in resp.text

    assert data["created_at"].endswith(("+00:00", "Z"))  # 带时区序列化（时区显示正确的前提）

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
        json={"name": "P1", "protocol": "anthropic", "base_url": "http://127.0.0.1:9/old"},
    )
    pid = resp.json()["id"]
    resp = await asgi_client.patch(
        f"/api/admin/providers/{pid}", json={"base_url": "http://127.0.0.1:9/new"}
    )
    assert resp.status_code == 200
    assert resp.json()["base_url"] == "http://127.0.0.1:9/new"
    got = await asgi_client.get(f"/api/admin/providers/{pid}")
    assert got.json()["base_url"] == "http://127.0.0.1:9/new"


async def test_delete_cascades(asgi_client: httpx.AsyncClient) -> None:
    """AE-08-3 级联删除：Provider 连同其模型一并删除；裁判失效/成员被清空的 Pipeline 一并删除，
    仍有其他成员与有效裁判的 Pipeline 保留，无关供应商不受影响。"""
    # 供应商 A：2 个模型；pipe-a 成员/裁判全来自 A；pipe-mix 成员横跨 A、B 且裁判来自 B
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": "P-del",
            "protocol": "openai_compatible",
            "base_url": "http://127.0.0.1:9/v1",
        },
    )
    pid_a = resp.json()["id"]
    mids_a = []
    for name in ("m-a1", "m-a2"):
        resp = await asgi_client.post(
            "/api/admin/models",
            json={"provider_id": pid_a, "display_name": name, "upstream_model_id": f"{name}-up"},
        )
        assert resp.status_code == 201, resp.text
        mids_a.append(resp.json()["id"])

    # 供应商 B：pipe-b 全部来自 B；pipe-mix 的另一成员与裁判也来自 B
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": "P-keep",
            "protocol": "openai_compatible",
            "base_url": "http://127.0.0.1:9/v1",
        },
    )
    pid_b = resp.json()["id"]
    resp = await asgi_client.post(
        "/api/admin/models",
        json={"provider_id": pid_b, "display_name": "m-b", "upstream_model_id": "m-b-up"},
    )
    assert resp.status_code == 201, resp.text
    mid_b = resp.json()["id"]

    for payload in (
        {"name": "pipe-a", "judge_model_id": mids_a[0], "members": [{"model_id": mids_a[1]}]},
        {
            "name": "pipe-mix",
            "judge_model_id": mid_b,
            "members": [{"model_id": mids_a[0]}, {"model_id": mid_b}],
        },
        {"name": "pipe-b", "judge_model_id": mid_b, "members": [{"model_id": mid_b}]},
    ):
        resp = await asgi_client.post(
            "/api/admin/pipelines", json={"strategy": "council", **payload}
        )
        assert resp.status_code == 201, resp.text

    assert (await asgi_client.delete(f"/api/admin/providers/{pid_a}")).status_code == 204

    models_left = (await asgi_client.get("/api/admin/models")).json()
    assert [m["display_name"] for m in models_left] == ["m-b"]
    pipelines_left = (await asgi_client.get("/api/admin/pipelines")).json()
    assert sorted(p["name"] for p in pipelines_left) == ["pipe-b", "pipe-mix"]  # pipe-mix 仍可运行
    assert (await asgi_client.get(f"/api/admin/providers/{pid_a}")).status_code == 404


async def test_protocol_validation(asgi_client: httpx.AsyncClient) -> None:
    """AE-08-4 校验：非法 protocol 枚举 → 422。"""
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={"name": "Bad", "protocol": "grpc", "base_url": "http://127.0.0.1:9/v1"},
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


async def test_auto_sync_models(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-08-8 自动同步：创建指向 SRS 的 Provider → 自动建出上游模型（SRS /v1/models 固化列表）；
    重复"测试"不产生重复行；上游不可达时创建不阻塞（模型留空）。"""
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": "auto-sync",
            "protocol": "openai_compatible",
            "base_url": f"{srs_live_seeded}/v1",
            "api_key": "sk-srs-test",
        },
    )
    assert resp.status_code == 201
    pid = resp.json()["id"]

    models = (await asgi_client.get("/api/admin/models")).json()
    mine = [m for m in models if m["provider_id"] == pid]
    assert [m["upstream_model_id"] for m in mine] == ["deepseek-v4-flash"]
    assert mine[0]["display_name"] == "deepseek-v4-flash"
    assert mine[0]["enabled"] is True

    resp = await asgi_client.post(f"/api/admin/providers/{pid}/test")
    data = resp.json()
    assert data["ok"] is True
    assert data["synced"] == []  # 已存在，不重复建
    assert len((await asgi_client.get("/api/admin/models")).json()) == 1

    # 死地址：创建成功但模型为空（best-effort）
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": "auto-sync-dead",
            "protocol": "openai_compatible",
            "base_url": "http://127.0.0.1:9/v1",
        },
    )
    assert resp.status_code == 201
    assert (await asgi_client.get("/api/admin/models")).json().__len__() == 1


async def test_sync_marks_removed_models_offline(asgi_client: httpx.AsyncClient, srs_live: str) -> None:
    """AE-40-1 下线同步：上游列表缩小时消失的模型被标记 upstream_missing 并联动停用
    （重复测试不重复上报）；用户手动停用不被同步覆盖；上游重新上架自动恢复。"""
    http = httpx.AsyncClient()
    await http.post(f"{srs_live}/_test/config", json={"models_override": ["m-stay", "m-gone"]})
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": "sync-offline",
            "protocol": "openai_compatible",
            "base_url": f"{srs_live}/v1",
            "api_key": "sk-srs-test",
        },
    )
    pid = resp.json()["id"]

    async def models_of_provider() -> dict[str, dict]:
        rows = (await asgi_client.get("/api/admin/models")).json()
        return {m["upstream_model_id"]: m for m in rows if m["provider_id"] == pid}

    assert set(await models_of_provider()) == {"m-stay", "m-gone"}

    # 上游下线 m-gone → 测试触发同步：标记下线 + 联动停用，不物理删除
    await http.post(f"{srs_live}/_test/config", json={"models_override": ["m-stay"]})
    data = (await asgi_client.post(f"/api/admin/providers/{pid}/test")).json()
    assert data["ok"] is True
    assert data["removed"] == ["m-gone"]
    assert data["synced"] == []
    rows = await models_of_provider()
    assert rows["m-gone"]["upstream_missing"] is True
    assert rows["m-gone"]["enabled"] is False
    assert rows["m-stay"]["upstream_missing"] is False
    assert rows["m-stay"]["enabled"] is True

    # 重复测试：已标记的不重复上报
    data = (await asgi_client.post(f"/api/admin/providers/{pid}/test")).json()
    assert data["removed"] == []

    # 用户手动停用仍在上游的模型：同步不覆盖其 enabled，也不打上游下线标
    stay_id = rows["m-stay"]["id"]
    assert (await asgi_client.patch(f"/api/admin/models/{stay_id}", json={"enabled": False})).status_code == 200
    data = (await asgi_client.post(f"/api/admin/providers/{pid}/test")).json()
    assert data["removed"] == []
    rows = await models_of_provider()
    assert rows["m-stay"]["enabled"] is False
    assert rows["m-stay"]["upstream_missing"] is False

    # 上游重新上架 m-gone：自动恢复启用；手动停用的 m-stay 保持停用
    await http.post(f"{srs_live}/_test/config", json={"models_override": ["m-stay", "m-gone"]})
    data = (await asgi_client.post(f"/api/admin/providers/{pid}/test")).json()
    assert data["synced"] == []
    assert data["removed"] == []
    rows = await models_of_provider()
    assert rows["m-gone"]["upstream_missing"] is False
    assert rows["m-gone"]["enabled"] is True
    assert rows["m-stay"]["enabled"] is False
    assert rows["m-stay"]["upstream_missing"] is False
