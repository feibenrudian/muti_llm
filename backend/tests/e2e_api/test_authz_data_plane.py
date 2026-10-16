"""AE-49-1..7：授权读写与数据面权限执行（列表过滤 / 403 / 即时生效 / 动态模型 / 级联）。"""

import httpx
from sqlalchemy import select

from app.orm import UserGrant
from tests.conftest import auth_headers
from tests.e2e_api.test_council import COUNCIL_BODY, seed_council
from tests.helpers import create_user, issue_key

QUANTUM = "用一句话解释量子纠缠"
SNAPSHOT_BODY = {
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "连通性测试：请只回复 pong"}],
    "temperature": 0.0,
}


async def _seed_two_providers(asgi_client: httpx.AsyncClient, srs_base: str) -> dict[str, int]:
    """P1（council 双模型）+ P2（无本地模型行，供动态透传/供应商可见性用例）。"""
    seeded = await seed_council(asgi_client, srs_base)
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": "srs-provider-two",
            "protocol": "openai_compatible",
            "base_url": f"{srs_base}/v1",
            "api_key": "sk-srs-test",
        },
    )
    p2 = resp.json()
    return {"p1": seeded["provider_id"], "p2": p2["id"], "m1": seeded["m1"], "m2": seeded["m2"]}


async def _alice_with_key(asgi_client: httpx.AsyncClient) -> tuple[dict, dict]:
    alice = await create_user(asgi_client, username="alice")
    key = await issue_key(asgi_client, alice["id"])
    return alice, key


async def test_grants_read_write(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-49-1 授权读写：PUT→GET 回读一致；重复 PUT 幂等；空数组清空；target 不存在 422。"""
    seeded = await _seed_two_providers(asgi_client, srs_live_seeded)
    alice, _ = await _alice_with_key(asgi_client)
    url = f"/api/admin/users/{alice['id']}/grants"

    pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    pipe_id = pipelines[0]["id"]

    resp = await asgi_client.put(
        url,
        json={"pipelines": [pipe_id], "models": [seeded["m1"]], "providers": [seeded["p2"]]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "pipelines": [pipe_id],
        "models": [seeded["m1"]],
        "providers": [seeded["p2"]],
    }

    # 重复 PUT 幂等；GET 一致
    resp = await asgi_client.put(
        url,
        json={"pipelines": [pipe_id], "models": [seeded["m1"]], "providers": [seeded["p2"]]},
    )
    assert resp.json() == body
    assert (await asgi_client.get(url)).json() == body

    # 空数组清空
    resp = await asgi_client.put(url, json={"pipelines": [], "models": [], "providers": []})
    assert resp.json() == {"pipelines": [], "models": [], "providers": []}

    # target 不存在 → 422
    resp = await asgi_client.put(url, json={"pipelines": [99999], "models": [], "providers": []})
    assert resp.status_code == 422
    resp = await asgi_client.put(url, json={"pipelines": [], "models": [99999], "providers": []})
    assert resp.status_code == 422


async def test_list_filtering(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-49-2 列表过滤：/v1/models、pipeline/models、route、route/{id}/models 收窄。"""
    seeded = await _seed_two_providers(asgi_client, srs_live_seeded)
    pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    pipe_id = pipelines[0]["id"]
    providers = (await asgi_client.get("/api/admin/providers")).json()
    p2_slug = next(p["slug"] for p in providers if p["id"] == seeded["p2"])
    p1_slug = next(p["slug"] for p in providers if p["id"] == seeded["p1"])

    alice, key = await _alice_with_key(asgi_client)
    # 零授权：列表为空
    models = (await asgi_client.get("/v1/models", headers=auth_headers(key["api_key"]))).json()[
        "data"
    ]
    assert models == []

    # 授权 pipeline + provider P1
    await asgi_client.put(
        f"/api/admin/users/{alice['id']}/grants",
        json={"pipelines": [pipe_id], "models": [], "providers": [seeded["p1"]]},
    )

    models = (await asgi_client.get("/v1/models", headers=auth_headers(key["api_key"]))).json()[
        "data"
    ]
    ids = {m["id"] for m in models}
    assert ids == {"council-v1", "deepseek-v4-flash"}  # P1 两模型行同 upstream id，去重后一条

    pipeline_models = (
        await asgi_client.get("/v1/pipeline/models", headers=auth_headers(key["api_key"]))
    ).json()["data"]
    assert [m["id"] for m in pipeline_models] == ["council-v1"]

    routes = (await asgi_client.get("/v1/route", headers=auth_headers(key["api_key"]))).json()[
        "routes"
    ]
    assert [r["slug"] for r in routes] == [p1_slug]

    p1_models = (
        await asgi_client.get(f"/v1/route/{p1_slug}/models", headers=auth_headers(key["api_key"]))
    ).json()["data"]
    assert [m["id"] for m in p1_models] == ["deepseek-v4-flash"]

    # 未授权供应商 → 命名空间级 404（与不存在/停用同形）
    resp = await asgi_client.get(
        f"/v1/route/{p2_slug}/models", headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


async def test_call_reject_and_allow(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-49-3 调用拒绝与放行：未授权 Pipeline/模型 → 403；已授权 → 200（快照回放）。"""
    await seed_council(asgi_client, srs_live_seeded)
    pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    pipe_id = pipelines[0]["id"]
    models = (await asgi_client.get("/api/admin/models")).json()
    m1 = models[0]["id"]

    alice, key = await _alice_with_key(asgi_client)
    await asgi_client.put(
        f"/api/admin/users/{alice['id']}/grants",
        json={"pipelines": [pipe_id], "models": [m1], "providers": []},
    )

    # 已授权 Pipeline → 200
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 200, resp.text

    # 已授权模型透传 → 200
    resp = await asgi_client.post(
        "/v1/chat/completions", json=SNAPSHOT_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 200

    # 撤销 pipeline 授权 → 403（message 含模型名）
    await asgi_client.put(
        f"/api/admin/users/{alice['id']}/grants",
        json={"pipelines": [], "models": [m1], "providers": []},
    )
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 403
    error = resp.json()["error"]
    assert error["code"] == "model_not_authorized"
    assert "council-v1" in error["message"]

    # 未授权模型 → 403（模型存在但未授权，区别于不存在的 404）
    await asgi_client.put(
        f"/api/admin/users/{alice['id']}/grants",
        json={"pipelines": [pipe_id], "models": [], "providers": []},
    )
    resp = await asgi_client.post(
        "/v1/chat/completions", json=SNAPSHOT_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "model_not_authorized"

    # 不存在仍是 404
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={"model": "no-such", "messages": [{"role": "user", "content": "x"}]},
        headers=auth_headers(key["api_key"]),
    )
    assert resp.status_code == 404


async def test_grants_take_effect_immediately(
    asgi_client: httpx.AsyncClient, srs_live_seeded: str
) -> None:
    """AE-49-4 即时生效：加授权立即可调用；撤授权立即 403（同进程、无重启）。"""
    await seed_council(asgi_client, srs_live_seeded)
    pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    pipe_id = pipelines[0]["id"]
    alice, key = await _alice_with_key(asgi_client)
    headers = auth_headers(key["api_key"])
    grants_url = f"/api/admin/users/{alice['id']}/grants"

    async def denied() -> bool:
        resp = await asgi_client.post("/v1/chat/completions", json=COUNCIL_BODY, headers=headers)
        return resp.status_code == 403

    assert await denied()
    await asgi_client.put(grants_url, json={"pipelines": [pipe_id], "models": [], "providers": []})
    resp = await asgi_client.post("/v1/chat/completions", json=COUNCIL_BODY, headers=headers)
    assert resp.status_code == 200
    await asgi_client.put(grants_url, json={"pipelines": [], "models": [], "providers": []})
    assert await denied()


async def test_admin_unrestricted(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-49-5 管理员不受限：存在受限用户时，兼容 Key 仍见全集、全部可调用。"""
    await seed_council(asgi_client, srs_live_seeded)
    pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    pipe_id = pipelines[0]["id"]
    alice, _ = await _alice_with_key(asgi_client)
    await asgi_client.put(
        f"/api/admin/users/{alice['id']}/grants",
        json={"pipelines": [], "models": [], "providers": []},
    )

    # 兼容 Key（admin）：列表含 council-v1 与透传模型，调用成功
    models = (await asgi_client.get("/v1/models", headers=auth_headers(service_key))).json()
    assert {m["id"] for m in models["data"]} >= {"council-v1", "deepseek-v4-flash"}
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200
    assert pipe_id > 0


async def test_dynamic_model_grant(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-49-6 动态模型：库内无行 → 仅供应商级授权覆盖；仅模型级授权 → 403；授供应商后 → 200。"""
    seeded = await _seed_two_providers(asgi_client, srs_live_seeded)
    models = (await asgi_client.get("/api/admin/models")).json()
    m1 = models[0]["id"]
    alice, key = await _alice_with_key(asgi_client)
    grants_url = f"/api/admin/users/{alice['id']}/grants"
    call_url = f"/v1/route/{seeded['p2']}/chat/completions"

    # 仅模型级授权（在 P1 上）→ P2 整体不可见 → 404（命名空间级按可见性，§3.5 契约表）
    await asgi_client.put(grants_url, json={"pipelines": [], "models": [m1], "providers": []})
    resp = await asgi_client.post(
        call_url, json=SNAPSHOT_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"

    # 授予 P2 供应商级 → 动态透传 200（快照回放）
    await asgi_client.put(
        grants_url, json={"pipelines": [], "models": [m1], "providers": [seeded["p2"]]}
    )
    resp = await asgi_client.post(
        call_url, json=SNAPSHOT_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "pong"


async def test_grants_cascade_cleanup(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-49-7 授权级联清理：删除被授权模型/供应商 → 对应授权行删除。"""
    from app.main import app

    seeded = await _seed_two_providers(asgi_client, srs_live_seeded)
    pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    pipe_id = pipelines[0]["id"]
    alice, _ = await _alice_with_key(asgi_client)
    await asgi_client.put(
        f"/api/admin/users/{alice['id']}/grants",
        json={
            "pipelines": [pipe_id],
            "models": [seeded["m1"], seeded["m2"]],
            "providers": [seeded["p1"], seeded["p2"]],
        },
    )

    # 删除 P2 → provider 授权清理（P2 无模型行，不影响其他授权）
    assert (await asgi_client.delete(f"/api/admin/providers/{seeded['p2']}")).status_code == 204

    async with app.state.session_factory() as session:
        rows = (
            (await session.execute(select(UserGrant).where(UserGrant.user_id == alice["id"])))
            .scalars()
            .all()
        )
        remaining = {(r.kind, r.target_id) for r in rows}
    assert ("provider", seeded["p2"]) not in remaining
    assert ("provider", seeded["p1"]) in remaining
    assert ("pipeline", pipe_id) in remaining
    assert ("model", seeded["m1"]) in remaining

    # 删除 Pipeline → pipeline 授权清理
    assert (await asgi_client.delete(f"/api/admin/pipelines/{pipe_id}")).status_code == 204

    # 删除 P1 → 其模型授权清理（council pipeline 因裁判/成员失效被级联删除，其授权行同删）
    assert (await asgi_client.delete(f"/api/admin/providers/{seeded['p1']}")).status_code == 204
    async with app.state.session_factory() as session:
        rows = (
            (await session.execute(select(UserGrant).where(UserGrant.user_id == alice["id"])))
            .scalars()
            .all()
        )
        assert rows == []
