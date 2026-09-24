"""AE-42-1..6：命名空间网关（/v1/pipeline 虚拟模型、/v1/route/{ident} 按供应商透传）。"""

import httpx

from tests.conftest import auth_headers
from tests.helpers import seed_provider_and_model

PONG_BODY = {
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "连通性测试：请只回复 pong"}],
    "temperature": 0.0,
}


async def _seed_named_provider(
    asgi_client: httpx.AsyncClient, srs_base_url: str, name: str
) -> dict:
    """建指向 SRS 的 provider，返回响应 JSON（含 slug）。"""
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": name,
            "protocol": "openai_compatible",
            "base_url": f"{srs_base_url}/v1",
            "api_key": "sk-srs-test",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _models_ids(
    asgi_client: httpx.AsyncClient, service_key: str, path: str
) -> list[str]:
    resp = await asgi_client.get(path, headers=auth_headers(service_key))
    assert resp.status_code == 200, resp.text
    return [m["id"] for m in resp.json()["data"]]


async def test_slug_identity(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-42-1 slug 身份：创建自动生成；改名不变；/v1/route 列出；slug 与数字 id 等价路由。"""
    provider = await _seed_named_provider(asgi_client, srs_live_seeded, "srs-provider")
    assert provider["slug"] == "srs-provider"

    resp = await asgi_client.patch(
        f"/api/admin/providers/{provider['id']}", json={"name": "改名后的供应商"}
    )
    assert resp.status_code == 200
    assert resp.json()["slug"] == "srs-provider"  # slug 不随显示名变化

    resp = await asgi_client.get("/v1/route", headers=auth_headers(service_key))
    assert resp.status_code == 200
    routes = resp.json()["routes"]
    assert {"id": provider["id"], "slug": "srs-provider", "name": "改名后的供应商"} in routes

    await seed_provider_and_model(asgi_client, srs_live_seeded)
    by_slug = await _models_ids(
        asgi_client, service_key, "/v1/route/srs-provider/models"
    )
    by_id = await _models_ids(asgi_client, service_key, f"/v1/route/{provider['id']}/models")
    assert by_slug == by_id == ["deepseek-v4-flash"]


async def test_slug_generation_rules(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-42-2 slug 生成：大小写/空格归一；冲突加序号；纯中文名兜底 provider-{id}。"""
    first = await _seed_named_provider(asgi_client, srs_live_seeded, "SRS Provider")
    assert first["slug"] == "srs-provider"
    second = await _seed_named_provider(asgi_client, srs_live_seeded, "srs provider")
    assert second["slug"] == "srs-provider-2"
    chinese = await _seed_named_provider(asgi_client, srs_live_seeded, "智谱")
    assert chinese["slug"] == f"provider-{chinese['id']}"


async def test_pipeline_namespace(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-42-3 pipeline 命名空间：models 只列虚拟模型；chat 只认 Pipeline 名。"""
    _, model_id = await seed_provider_and_model(asgi_client, srs_live_seeded)
    from app.main import app
    from app.orm import Pipeline
    from app.repos import Repository

    async with app.state.session_factory() as session:
        await Repository(session, Pipeline).create(
            name="council-v1", strategy="council", judge_model_id=model_id
        )
        await session.commit()

    resp = await asgi_client.get("/v1/pipeline", headers=auth_headers(service_key))
    assert resp.status_code == 200 and resp.json()["namespace"] == "pipeline"

    ids = await _models_ids(asgi_client, service_key, "/v1/pipeline/models")
    assert ids == ["council-v1"]  # 不含真实模型

    resp = await asgi_client.post(
        "/v1/pipeline/chat/completions",
        json={"model": "council-v1", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 502  # 命中策略分支（无成员 → 策略执行失败），而非透传兜底
    assert "策略执行失败" in resp.json()["error"]["message"]

    resp = await asgi_client.post(
        "/v1/pipeline/chat/completions",
        json=PONG_BODY,  # 真实模型名在 pipeline 命名空间不可见
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


async def test_route_chat_resolution(
    asgi_client: httpx.AsyncClient,
    service_key: str,
    srs_live_seeded: str,
    monkeypatch: object,
) -> None:
    """AE-42-4 route chat：启用模型透传；本地停用拒绝；上游新模型（本地无记录）动态透传。"""
    provider_id, model_id = await seed_provider_and_model(asgi_client, srs_live_seeded)
    provider = (
        await asgi_client.get("/api/admin/providers")
    ).json()[0]

    # 1) 启用模型 → 正常透传（复用 AE-10-1 形态的快照）
    resp = await asgi_client.post(
        f"/v1/route/{provider['slug']}/chat/completions",
        json=PONG_BODY,
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "pong"

    # 2) 本地停用 → 404（明确停用的模型不可调用）
    resp = await asgi_client.patch(
        f"/api/admin/models/{model_id}", json={"enabled": False}
    )
    assert resp.status_code == 200
    resp = await asgi_client.post(
        f"/v1/route/{provider['id']}/chat/completions",
        json=PONG_BODY,
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"

    # 3) 上游新模型本地无记录 → 动态透传默认可工作：
    #    新供应商的自动同步列表（models_override）不含该名 → 本地无任何行 → 动态透传
    import httpx as httpx_mod

    await httpx_mod.AsyncClient().post(
        f"{srs_live_seeded}/_test/config", json={"models_override": ["upstream-only"]}
    )
    fresh = await _seed_named_provider(asgi_client, srs_live_seeded, "fresh-provider")
    resp = await asgi_client.post(
        f"/v1/route/{fresh['slug']}/chat/completions",
        json=PONG_BODY,
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "pong"


async def test_route_models_filtering_and_fallback(
    asgi_client: httpx.AsyncClient,
    service_key: str,
    srs_live_seeded: str,
    monkeypatch: object,
) -> None:
    """AE-42-5 路由列表：过滤本地停用、上游新模型保留（默认可工作）、上游失败回落库内列表。"""
    from app.gateways import namespaces

    monkeypatch.setattr(namespaces, "ROUTE_LIST_TTL_SECONDS", 0.0)  # 每次都探上游
    provider_id, model_id = await seed_provider_and_model(asgi_client, srs_live_seeded)
    slug = (await asgi_client.get("/api/admin/providers")).json()[0]["slug"]

    import httpx as httpx_mod

    await httpx_mod.AsyncClient().post(
        f"{srs_live_seeded}/_test/config",
        json={"models_override": ["deepseek-v4-flash", "brand-new"]},
    )
    ids = await _models_ids(asgi_client, service_key, f"/v1/route/{slug}/models")
    assert sorted(ids) == ["brand-new", "deepseek-v4-flash"]  # 上游新模型默认可工作

    resp = await asgi_client.patch(
        f"/api/admin/models/{model_id}", json={"enabled": False}
    )
    assert resp.status_code == 200
    ids = await _models_ids(asgi_client, service_key, f"/v1/route/{slug}/models")
    assert ids == ["brand-new"]  # 本地明确停用的被过滤

    # 上游认证失败（不可达）→ 回落库内已同步的启用模型
    await httpx_mod.AsyncClient().post(
        f"{srs_live_seeded}/_test/config", json={"models_auth_fail": True}
    )
    ids = await _models_ids(asgi_client, service_key, f"/v1/route/{slug}/models")
    assert ids == []  # deepseek-v4-flash 已停用、brand-new 无本地行

    await httpx_mod.AsyncClient().post(
        f"{srs_live_seeded}/_test/config", json={"models_auth_fail": False}
    )
    _ = provider_id


async def test_namespace_exposure_and_provider_gate(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-42-6 接入开关与供应商闸门：类型开关整体下线命名空间；停用供应商 404。"""
    provider_id, model_id = await seed_provider_and_model(asgi_client, srs_live_seeded)
    from app.main import app
    from app.orm import Pipeline
    from app.repos import Repository

    async with app.state.session_factory() as session:
        await Repository(session, Pipeline).create(
            name="council-v1", strategy="council", judge_model_id=model_id
        )
        await session.commit()

    resp = await asgi_client.patch(
        "/api/admin/settings", json={"expose_virtual_models": False}
    )
    assert resp.status_code == 200
    for path, method in (
        ("/v1/pipeline", "get"),
        ("/v1/pipeline/models", "get"),
        ("/v1/pipeline/chat/completions", "post"),
    ):
        call = asgi_client.post if method == "post" else asgi_client.get
        resp = await call(
            path,
            **(
                {"json": {"model": "council-v1", "messages": [{"role": "user", "content": "hi"}]}}
                if method == "post"
                else {}
            ),
            headers=auth_headers(service_key),
        )
        assert resp.status_code == 404, f"{path} 应整体 404"
    # 路由命名空间不受虚拟开关影响
    assert (
        await asgi_client.get(
            "/v1/route/srs-provider/models", headers=auth_headers(service_key)
        )
    ).status_code == 200

    resp = await asgi_client.patch("/api/admin/settings", json={"expose_routed_models": False})
    assert resp.status_code == 200
    resp = await asgi_client.get(
        "/v1/route/srs-provider/models", headers=auth_headers(service_key)
    )
    assert resp.status_code == 404

    # 恢复开关后，停用供应商同样整体 404
    await asgi_client.patch("/api/admin/settings", json={"expose_routed_models": True})
    await asgi_client.patch(
        f"/api/admin/providers/{provider_id}", json={"enabled": False}
    )
    resp = await asgi_client.get("/v1/route", headers=auth_headers(service_key))
    assert resp.json()["routes"] == []
    resp = await asgi_client.get(
        "/v1/route/srs-provider/models", headers=auth_headers(service_key)
    )
    assert resp.status_code == 404
