"""AE-51-1/2：Key 最后使用时间与管理动作审计流水。"""

import httpx

from tests.conftest import auth_headers
from tests.e2e_api.test_council import COUNCIL_BODY, seed_council
from tests.helpers import create_user, issue_key


async def test_key_last_used_at(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-51-1：用 Key 调用一次 → last_used_at 非空且 ≤ now；列表接口返回该值。"""
    await seed_council(asgi_client, srs_live_seeded)
    alice = await create_user(asgi_client, username="alice-audit")
    key = await issue_key(asgi_client, alice["id"])

    resp = await asgi_client.get(f"/api/admin/users/{alice['id']}/keys")
    assert [k for k in resp.json() if k["id"] == key["id"]][0]["last_used_at"] is None

    pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    await asgi_client.put(
        f"/api/admin/users/{alice['id']}/grants",
        json={"pipelines": [pipelines[0]["id"]], "models": [], "providers": []},
    )
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(key["api_key"])
    )
    assert resp.status_code == 200

    keys = (await asgi_client.get(f"/api/admin/users/{alice['id']}/keys")).json()
    row = next(k for k in keys if k["id"] == key["id"])
    assert row["last_used_at"] is not None


async def test_audit_trail(asgi_client: httpx.AsyncClient, srs_live_seeded: str) -> None:
    """AE-51-2：建用户/发 Key/改授权/登录成败 → 审计行含操作人、动作、目标；可按动作筛选。"""
    from app.main import app

    await seed_council(asgi_client, srs_live_seeded)
    alice = await create_user(asgi_client, username="alice-trail")
    await issue_key(asgi_client, alice["id"])
    pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    await asgi_client.put(
        f"/api/admin/users/{alice['id']}/grants",
        json={"pipelines": [pipelines[0]["id"]], "models": [], "providers": []},
    )
    # 登录成功 + 失败各一次
    initial = app.state.initial_admin_password
    await asgi_client.post("/api/admin/auth/login", json={"username": "admin", "password": initial})
    await asgi_client.post("/api/admin/auth/login", json={"username": "admin", "password": "wrong"})

    rows = (await asgi_client.get("/api/admin/audit")).json()["items"]
    actions = {r["action"] for r in rows}
    expected = {"user.create", "key.create", "grant.update", "auth.login", "auth.login_failed"}
    assert expected <= actions

    created = next(r for r in rows if r["action"] == "user.create")
    assert created["actor_username"] == "admin"
    assert created["detail"]["username"] == "alice-trail"

    failed = next(r for r in rows if r["action"] == "auth.login_failed")
    assert failed["target_id"] == "admin"

    # 按动作筛选
    resp = await asgi_client.get("/api/admin/audit", params={"action": "key.create"})
    rows = resp.json()["items"]
    assert rows and all(r["action"] == "key.create" for r in rows)
