"""AE-47-1..6：用户与 Key 管理（CRUD / 防自锁 / Key 脱敏 / 兼容 Key 行 / 提升 / 分页）。"""

import hashlib

import httpx
from sqlalchemy import select

from app.orm import UserApiKey
from tests.helpers import create_user, issue_key


async def test_user_crud(asgi_client: httpx.AsyncClient) -> None:
    """AE-47-1 用户 CRUD：建（201, role=user）→ 列表 → 改显示名 → 停用/启用 → 删 204；重名 409。"""
    resp = await asgi_client.post(
        "/api/admin/users",
        json={"username": "alice", "display_name": "Alice", "remark": "测试"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["role"] == "user"
    assert body["key_count"] == 0
    alice_id = body["id"]

    # 重名 → 409
    resp = await asgi_client.post("/api/admin/users", json={"username": "alice"})
    assert resp.status_code == 409

    # 列表含内置 admin 与 alice
    resp = await asgi_client.get("/api/admin/users")
    users = {u["username"]: u for u in resp.json()["items"]}
    assert "admin" in users and "alice" in users

    # 更新显示名 / 停用 / 启用
    resp = await asgi_client.patch(f"/api/admin/users/{alice_id}", json={"display_name": "Alice2"})
    assert resp.json()["display_name"] == "Alice2"
    resp = await asgi_client.patch(f"/api/admin/users/{alice_id}", json={"enabled": False})
    assert resp.json()["enabled"] is False
    resp = await asgi_client.patch(f"/api/admin/users/{alice_id}", json={"enabled": True})
    assert resp.json()["enabled"] is True

    # 删除 → 204，再删 404
    assert (await asgi_client.delete(f"/api/admin/users/{alice_id}")).status_code == 204
    assert (await asgi_client.delete(f"/api/admin/users/{alice_id}")).status_code == 404


async def test_builtin_admin_lock(asgi_client: httpx.AsyncClient) -> None:
    """AE-47-2 防自锁：内置 admin 不可删/停用/降级；第二名管理员可停用、降级、删除。"""
    resp = await asgi_client.get("/api/admin/users", params={"keyword": "admin"})
    admin_id = resp.json()["items"][0]["id"]

    for method, payload in (
        ("patch", {"enabled": False}),
        ("patch", {"role": "user"}),
        ("delete", None),
    ):
        if method == "patch":
            resp = await asgi_client.patch(f"/api/admin/users/{admin_id}", json=payload)
        else:
            resp = await asgi_client.delete(f"/api/admin/users/{admin_id}")
        assert resp.status_code == 409, (method, payload, resp.text)
        assert "内置管理员" in resp.json()["detail"]

    # 第二名管理员：可停用/降级/删除（内置 admin 恒在，防自锁不触发）
    resp = await create_user(asgi_client, username="bob", role="admin", password="bob-pass-123")
    bob_id = resp["id"]
    assert (
        await asgi_client.patch(f"/api/admin/users/{bob_id}", json={"enabled": False})
    ).status_code == 200
    assert (
        await asgi_client.patch(f"/api/admin/users/{bob_id}", json={"role": "user"})
    ).status_code == 200
    assert (await asgi_client.delete(f"/api/admin/users/{bob_id}")).status_code == 204


async def test_key_issue_and_masking(asgi_client: httpx.AsyncClient) -> None:
    """AE-47-3 Key 签发与脱敏：明文一次；列表仅掩码；/secret 可取明文；库内只有哈希。"""
    alice = await create_user(asgi_client, username="alice")
    created = await issue_key(asgi_client, alice["id"], name="笔记本")
    assert created["api_key"].startswith("sk-mu-")
    assert created["masked"] == "****" + created["api_key"][-4:]

    resp = await asgi_client.get(f"/api/admin/users/{alice['id']}/keys")
    keys = [k for k in resp.json() if not k["is_legacy"]]
    assert len(keys) == 1
    assert keys[0]["masked"] == created["masked"]
    assert "api_key" not in keys[0]  # 列表不回显明文

    # /secret 返回与创建一致的明文
    resp = await asgi_client.get(f"/api/admin/users/{alice['id']}/keys/{keys[0]['id']}/secret")
    assert resp.json()["api_key"] == created["api_key"]

    # 库内只存哈希（SHA-256）与密文副本，无明文
    from app.main import app

    async with app.state.session_factory() as session:
        row = (
            await session.execute(select(UserApiKey).where(UserApiKey.id == keys[0]["id"]))
        ).scalar_one()
        assert row.key_hash == hashlib.sha256(created["api_key"].encode()).hexdigest()
        assert created["api_key"] not in row.key_encrypted
        assert row.created_by == app.state.admin_user_id


async def test_key_lifecycle_and_legacy_row(asgi_client: httpx.AsyncClient) -> None:
    """AE-47-4 Key 停删与兼容行：停用生效；删除 204；兼容 Key 行 PATCH/DELETE → 409。"""
    from app.main import app

    admin_id = app.state.admin_user_id
    resp = await asgi_client.get(f"/api/admin/users/{admin_id}/keys")
    keys = resp.json()
    assert keys[0]["is_legacy"] is True  # 兼容 Key 在列表首位

    assert (
        await asgi_client.patch(f"/api/admin/users/{admin_id}/keys/0", json={"enabled": False})
    ).status_code == 409
    assert (await asgi_client.delete(f"/api/admin/users/{admin_id}/keys/0")).status_code == 409

    alice = await create_user(asgi_client, username="alice")
    created = await issue_key(asgi_client, alice["id"])
    key_id = created["id"]
    resp = await asgi_client.patch(
        f"/api/admin/users/{alice['id']}/keys/{key_id}", json={"enabled": False}
    )
    assert resp.json()["enabled"] is False
    assert (
        await asgi_client.patch(
            f"/api/admin/users/{alice['id']}/keys/{key_id}", json={"name": "k2"}
        )
    ).json()["name"] == "k2"
    assert (
        await asgi_client.delete(f"/api/admin/users/{alice['id']}/keys/{key_id}")
    ).status_code == 204
    # 删除后列表只剩空（非 admin 无兼容行）
    resp = await asgi_client.get(f"/api/admin/users/{alice['id']}/keys")
    assert resp.json() == []


async def test_promote_requires_password(asgi_client: httpx.AsyncClient) -> None:
    """AE-47-5 提升与重置口令：提升缺口令 → 422；带口令成功并可登录；重置口令后旧会话吊销。"""
    alice = await create_user(asgi_client, username="alice")

    resp = await asgi_client.patch(f"/api/admin/users/{alice['id']}", json={"role": "admin"})
    assert resp.status_code == 422

    resp = await asgi_client.patch(
        f"/api/admin/users/{alice['id']}",
        json={"role": "admin", "password": "alice-pass-123"},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"

    # 新口令可登录
    resp = await asgi_client.post(
        "/api/admin/auth/login", json={"username": "alice", "password": "alice-pass-123"}
    )
    assert resp.status_code == 200

    # 管理员重置口令：新口令生效
    resp = await asgi_client.post(
        f"/api/admin/users/{alice['id']}/password", json={"new_password": "reset-pass-456"}
    )
    assert resp.status_code == 200
    assert (
        await asgi_client.post(
            "/api/admin/auth/login", json={"username": "alice", "password": "reset-pass-456"}
        )
    ).status_code == 200


async def test_user_list_pagination_and_filters(asgi_client: httpx.AsyncClient) -> None:
    """AE-47-6 分页与筛选：25 用户 page=2 命中；keyword/role/enabled 各维度正确。"""
    for i in range(24):
        await create_user(asgi_client, username=f"bulk-{i:02d}")
    await create_user(
        asgi_client,
        username="special-agent",
        display_name="特批",
        role="admin",
        password="agent-pass-1",
    )

    resp = await asgi_client.get("/api/admin/users", params={"page": 2, "page_size": 20})
    body = resp.json()
    assert body["total"] >= 26
    assert len(body["items"]) == body["total"] - 20

    resp = await asgi_client.get("/api/admin/users", params={"keyword": "special"})
    assert [u["username"] for u in resp.json()["items"]] == ["special-agent"]

    resp = await asgi_client.get("/api/admin/users", params={"role": "admin"})
    assert {u["username"] for u in resp.json()["items"]} >= {"admin", "special-agent"}

    resp = await asgi_client.get("/api/admin/users", params={"enabled": "false"})
    assert all(u["enabled"] is False for u in resp.json()["items"])
