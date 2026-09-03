"""AE-19-1..4：Pipeline 管理 API + 校验。"""

import httpx

from tests.conftest import auth_headers
from tests.helpers import seed_provider_and_model


async def _seed_two_models(asgi_client: httpx.AsyncClient, srs_base: str) -> tuple[int, int]:
    """两个指向 SRS 的模型（同一 upstream，不同 display 名）。"""
    _, m1 = await seed_provider_and_model(asgi_client, srs_base)
    resp = await asgi_client.post(
        "/api/admin/models",
        json={
            "provider_id": 1,
            "display_name": "flash-09",
            "upstream_model_id": "deepseek-v4-flash",
            "default_params": {},
        },
    )
    assert resp.status_code == 201, resp.text
    return m1, resp.json()["id"]


def pipeline_payload(m1: int, m2: int, **overrides) -> dict:
    payload = {
        "name": "council-v1",
        "strategy": "council",
        "judge_model_id": m1,
        "members": [
            {"model_id": m1, "param_overrides": {"temperature": 0.7}},
            {"model_id": m2, "param_overrides": {"temperature": 0.9}},
        ],
    }
    payload.update(overrides)
    return payload


async def test_crud_and_member_order(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-19-1 CRUD+成员排序：创建带成员的 pipeline，返回顺序与覆盖参数一致；更新排序生效。"""
    m1, m2 = await _seed_two_models(asgi_client, srs_live_seeded)

    resp = await asgi_client.post("/api/admin/pipelines", json=pipeline_payload(m1, m2))
    assert resp.status_code == 201, resp.text
    data = resp.json()
    pid = data["id"]
    assert data["name"] == "council-v1"
    assert [m["model_id"] for m in data["members"]] == [m1, m2]
    assert data["members"][0]["param_overrides"] == {"temperature": 0.7}
    assert data["members"][0]["sort_order"] == 0

    # 更新成员顺序（倒排）生效
    resp = await asgi_client.patch(
        f"/api/admin/pipelines/{pid}",
        json={"members": [{"model_id": m2}, {"model_id": m1}]},
    )
    assert resp.status_code == 200
    assert [m["model_id"] for m in resp.json()["members"]] == [m2, m1]

    # 列表与详情
    assert len((await asgi_client.get("/api/admin/pipelines")).json()) == 1
    assert (await asgi_client.get(f"/api/admin/pipelines/{pid}")).json()["id"] == pid
    assert (await asgi_client.delete(f"/api/admin/pipelines/{pid}")).status_code == 204
    assert (await asgi_client.get("/api/admin/pipelines")).json() == []


async def test_validation_members_and_judge(
    asgi_client: httpx.AsyncClient, srs_live_seeded: str
) -> None:
    """AE-19-2 校验：0 成员 → 422；judge 不存在 → 422。"""
    m1, _ = await _seed_two_models(asgi_client, srs_live_seeded)

    resp = await asgi_client.post(
        "/api/admin/pipelines",
        json={"name": "p-no-members", "judge_model_id": m1, "members": []},
    )
    assert resp.status_code == 422

    resp = await asgi_client.post(
        "/api/admin/pipelines",
        json={"name": "p-no-judge", "judge_model_id": 9999, "members": [{"model_id": m1}]},
    )
    assert resp.status_code == 422
    assert "judge_model_id" in resp.json()["detail"]

    # 未注册策略
    resp = await asgi_client.post(
        "/api/admin/pipelines",
        json={
            "name": "p-bad-strategy",
            "strategy": "voting",
            "judge_model_id": m1,
            "members": [{"model_id": m1}],
        },
    )
    assert resp.status_code == 422


async def test_validation_name(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-19-3 校验：重名 → 409；非法字符 → 422。"""
    m1, _ = await _seed_two_models(asgi_client, srs_live_seeded)
    resp = await asgi_client.post("/api/admin/pipelines", json=pipeline_payload(m1, m1))
    assert resp.status_code == 201

    # 重名 → 409
    resp = await asgi_client.post(
        "/api/admin/pipelines", json=pipeline_payload(m1, m1, name="council-v1")
    )
    assert resp.status_code == 409

    # 非法名（大写/中文/空格）→ 422
    for bad in ["Council-V1", "中文管道", "has space"]:
        resp = await asgi_client.post(
            "/api/admin/pipelines", json=pipeline_payload(m1, m1, name=bad)
        )
        assert resp.status_code == 422, bad


async def test_enable_disable_linkage(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-19-4 启停联动：停用后 /v1/models 不列出，调用该名 → 404。"""
    m1, m2 = await _seed_two_models(asgi_client, srs_live_seeded)
    resp = await asgi_client.post("/api/admin/pipelines", json=pipeline_payload(m1, m2))
    pid = resp.json()["id"]

    models_resp = await asgi_client.get("/v1/models", headers=auth_headers(service_key))
    ids = [m["id"] for m in models_resp.json()["data"]]
    assert "council-v1" in ids

    assert (
        await asgi_client.patch(f"/api/admin/pipelines/{pid}", json={"enabled": False})
    ).status_code == 200
    ids = [
        m["id"]
        for m in (await asgi_client.get("/v1/models", headers=auth_headers(service_key))).json()[
            "data"
        ]
    ]
    assert "council-v1" not in ids

    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={"model": "council-v1", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 404  # 不再命中 Pipeline，走解析兜底
