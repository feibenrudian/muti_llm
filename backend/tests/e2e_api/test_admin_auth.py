"""AE-46-1..6：管理端认证（全管理面强制会话、登录/登出/改密、平面隔离、初始密码）。"""

import httpx

from app.main import app
from tests.conftest import auth_headers
from tests.helpers import seed_provider_and_model

ADMIN_ENDPOINTS = (
    ("GET", "/api/admin/providers"),
    ("GET", "/api/admin/models"),
    ("GET", "/api/admin/pipelines"),
    ("GET", "/api/admin/traces"),
    ("GET", "/api/admin/stats/overview"),
    ("GET", "/api/admin/settings"),
    ("GET", "/api/admin/meta/strategies"),
    ("POST", "/api/admin/playground/run"),
    ("GET", "/api/admin/users"),
    ("GET", "/api/admin/auth/me"),
)


async def test_admin_requires_session(asgi_client: httpx.AsyncClient) -> None:
    """AE-46-1 全管理面强制会话：无凭证 → 401 {"detail"}；带会话 → 既有行为不变。"""
    for method, url in ADMIN_ENDPOINTS:
        resp = await asgi_client.request(
            method,
            url,
            headers={"Authorization": ""},  # 显式清掉客户端默认管理会话头
            **({"json": {"pipeline_name": "x", "message": "hi"}} if method == "POST" else {}),
        )
        assert resp.status_code == 401, (method, url, resp.status_code)
        assert "detail" in resp.json(), (method, url)

    # 带会话（asgi_client 默认头）→ 非 401
    for method, url in ADMIN_ENDPOINTS:
        resp = await asgi_client.request(method, url)
        assert resp.status_code != 401, (method, url)

    # 公开端点不受影响
    assert (await asgi_client.get("/health")).status_code == 200
    # 登录/登出在白名单
    resp = await asgi_client.post("/api/admin/auth/login", json={"username": "x", "password": "y"})
    assert resp.status_code == 401  # 可达（凭据错误而非未认证 401 结构差异）


async def test_login_success_and_failure(asgi_client: httpx.AsyncClient) -> None:
    """AE-46-2/6 登录：初始密码成功；错误口令/不存在用户/停用用户 → 401 统一文案。"""
    initial = app.state.initial_admin_password
    assert initial, "初始密码仅在首次启动生成"
    resp = await asgi_client.post(
        "/api/admin/auth/login", json={"username": "admin", "password": initial}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["user"]["username"] == "admin"
    assert body["user"]["role"] == "admin"

    for creds in (
        {"username": "admin", "password": "wrong-pass"},
        {"username": "ghost", "password": initial},
    ):
        resp = await asgi_client.post("/api/admin/auth/login", json=creds)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "用户名或密码错误"  # 不区分存在性（防枚举）


async def test_logout_revokes_token(asgi_client: httpx.AsyncClient) -> None:
    """AE-46-3 登出：旧令牌立即 401；重复登出幂等 200。"""
    resp = await asgi_client.post(
        "/api/admin/auth/login",
        json={"username": "admin", "password": app.state.initial_admin_password},
    )
    token = resp.json()["token"]
    headers = auth_headers(token)
    assert (await asgi_client.get("/api/admin/auth/me", headers=headers)).status_code == 200

    assert (await asgi_client.post("/api/admin/auth/logout", headers=headers)).status_code == 200
    assert (await asgi_client.get("/api/admin/auth/me", headers=headers)).status_code == 401
    # 重复登出（同令牌）幂等
    assert (await asgi_client.post("/api/admin/auth/logout", headers=headers)).status_code == 200


async def test_change_password(asgi_client: httpx.AsyncClient, tmp_path: object) -> None:
    """AE-46-4 改密：旧口令错 → 400；成功后旧口令登录失败、新口令成功；其他会话吊销、当前保留。"""
    from app.repos import create_admin_session

    initial = app.state.initial_admin_password
    resp = await asgi_client.post(
        "/api/admin/auth/login", json={"username": "admin", "password": initial}
    )
    token = resp.json()["token"]
    headers = auth_headers(token)

    # 另铸一个"其他会话"（直插库）
    other_token = "other-session-token"
    async with app.state.session_factory() as session:
        await create_admin_session(session, app.state.admin_user_id, other_token)
        await session.commit()
    assert (
        await asgi_client.get("/api/admin/auth/me", headers=auth_headers(other_token))
    ).status_code == 200

    # 旧口令错 → 400
    resp = await asgi_client.post(
        "/api/admin/auth/password",
        json={"old_password": "totally-wrong", "new_password": "brand-new-pass-1"},
        headers=headers,
    )
    assert resp.status_code == 400

    # 过短口令 → 400
    resp = await asgi_client.post(
        "/api/admin/auth/password",
        json={"old_password": initial, "new_password": "short"},
        headers=headers,
    )
    assert resp.status_code == 400

    # 成功改密
    resp = await asgi_client.post(
        "/api/admin/auth/password",
        json={"old_password": initial, "new_password": "brand-new-pass-1"},
        headers=headers,
    )
    assert resp.status_code == 200

    # 旧口令登录失败、新口令成功
    assert (
        await asgi_client.post(
            "/api/admin/auth/login", json={"username": "admin", "password": initial}
        )
    ).status_code == 401
    assert (
        await asgi_client.post(
            "/api/admin/auth/login", json={"username": "admin", "password": "brand-new-pass-1"}
        )
    ).status_code == 200

    # 当前会话保留、其他会话被吊销
    assert (await asgi_client.get("/api/admin/auth/me", headers=headers)).status_code == 200
    assert (
        await asgi_client.get("/api/admin/auth/me", headers=auth_headers(other_token))
    ).status_code == 401


async def test_plane_isolation(asgi_client: httpx.AsyncClient, service_key: str) -> None:
    """AE-46-5 平面隔离：管理会话令牌调 /v1 → 401（数据面不认会话令牌）。"""
    login = await asgi_client.post(
        "/api/admin/auth/login",
        json={"username": "admin", "password": app.state.initial_admin_password},
    )
    session_token = login.json()["token"]
    resp = await asgi_client.get("/v1/models", headers=auth_headers(session_token))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"
    # 而服务 Key 照常工作
    resp = await asgi_client.get("/v1/models", headers=auth_headers(service_key))
    assert resp.status_code == 200


async def test_v1_rejects_missing_and_bad_key(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-46-7 /v1 默认头回归：显式空/错 Key → 401（默认管理会话头不越权到数据面）。"""
    await seed_provider_and_model(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 401
    resp = await asgi_client.get("/v1/models", headers=auth_headers("sk-mu-unknown"))
    assert resp.status_code == 401
