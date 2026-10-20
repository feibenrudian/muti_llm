"""AE-54-1..6：会话 Cookie 持久登录（T54）。

登录成功下发 HttpOnly 会话 Cookie；无 Bearer 头时中间件回退 Cookie 认证；
已认证响应滑动续期 Cookie（同值重置 max_age）；登出吊销会话并下发清除指令、
改密从 Cookie 解析 keep_token；管理/门户 Cookie 按名字与路径互相隔离。
"""

import httpx

from app.auth_svc import (
    ADMIN_SESSION_COOKIE,
    PORTAL_SESSION_COOKIE,
    SESSION_COOKIE_MAX_AGE,
)
from app.main import app
from tests.conftest import auth_headers
from tests.helpers import create_user

NO_BEARER = {"Authorization": ""}  # 显式清掉 asgi_client 默认管理会话头，只留 Cookie 生效


def _set_cookie_header(resp: httpx.Response, name: str) -> str:
    for header in resp.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            return header
    return ""


async def _admin_login(asgi_client: httpx.AsyncClient) -> str:
    resp = await asgi_client.post(
        "/api/admin/auth/login",
        json={"username": "admin", "password": app.state.initial_admin_password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


async def test_login_sets_cookie(asgi_client: httpx.AsyncClient) -> None:
    """AE-54-1 登录下发会话 Cookie：HttpOnly、SameSite=Lax、路径限定、Max-Age 与 TTL 一致。"""
    await create_user(asgi_client, username="cookie-alice", password="alice-pass-123")

    resp = await asgi_client.post(
        "/api/admin/auth/login",
        json={"username": "admin", "password": app.state.initial_admin_password},
    )
    header = _set_cookie_header(resp, ADMIN_SESSION_COOKIE)
    assert header, "管理面登录应下发会话 Cookie"
    assert "HttpOnly" in header and "SameSite=lax" in header
    assert "Path=/api/admin" in header
    assert f"Max-Age={SESSION_COOKIE_MAX_AGE}" in header

    resp = await asgi_client.post(
        "/api/portal/auth/login", json={"username": "cookie-alice", "password": "alice-pass-123"}
    )
    header = _set_cookie_header(resp, PORTAL_SESSION_COOKIE)
    assert header, "门户登录应下发会话 Cookie"
    assert "Path=/api/portal" in header and "HttpOnly" in header and "SameSite=lax" in header


async def test_cookie_authenticates_without_bearer(asgi_client: httpx.AsyncClient) -> None:
    """AE-54-2 Cookie 免 Bearer 认证：仅持 Cookie（清 Authorization）→ /me 200；坏 Cookie → 401。"""
    await _admin_login(asgi_client)

    # httpx 客户端自动持有登录 Set-Cookie，等价于浏览器重启后 sessionStorage 为空、Cookie 尚在
    resp = await asgi_client.get("/api/admin/auth/me", headers=NO_BEARER)
    assert resp.status_code == 200
    assert resp.json()["username"] == "admin"

    resp = await asgi_client.get(
        "/api/admin/auth/me",
        headers=NO_BEARER,
        cookies={ADMIN_SESSION_COOKIE: "bogus-session-token"},
    )
    assert resp.status_code == 401


async def test_cookie_change_password_keeps_current_session(asgi_client: httpx.AsyncClient) -> None:
    """AE-54-3 Cookie 改密：keep_token 从 Cookie 解析——当前会话保留、直插库铸的其他会话吊销。"""
    from app.repos import create_admin_session

    created = await create_user(asgi_client, username="pw-alice", password="pw-pass-12345")
    old_password = "pw-pass-12345"
    resp = await asgi_client.post(
        "/api/portal/auth/login", json={"username": "pw-alice", "password": old_password}
    )
    assert resp.status_code == 200

    # 另铸一个"其他会话"（直插库），改密后应被吊销
    other_token = "other-portal-session"
    async with app.state.session_factory() as session:
        await create_admin_session(session, created["id"], other_token)
        await session.commit()

    resp = await asgi_client.post(
        "/api/portal/auth/password",
        json={"old_password": old_password, "new_password": "pw-new-pass-678"},
        headers=NO_BEARER,  # 仅 Cookie：keep_token 必须从 Cookie 解析
    )
    assert resp.status_code == 200

    resp = await asgi_client.get("/api/portal/auth/me", headers=NO_BEARER)
    assert resp.status_code == 200, "当前（Cookie）会话应保留"
    resp = await asgi_client.get("/api/portal/auth/me", headers=auth_headers(other_token))
    assert resp.status_code == 401, "其他会话应被吊销"


async def test_logout_clears_cookie(asgi_client: httpx.AsyncClient) -> None:
    """AE-54-4 Cookie 登出：吊销 Cookie 对应会话并下发清除指令；旧 Cookie 随即 401。"""
    await create_user(asgi_client, username="out-alice", password="out-pass-12345")
    resp = await asgi_client.post(
        "/api/portal/auth/login", json={"username": "out-alice", "password": "out-pass-12345"}
    )
    token = resp.json()["token"]

    resp = await asgi_client.post("/api/portal/auth/logout", headers=NO_BEARER)
    assert resp.status_code == 200
    header = _set_cookie_header(resp, PORTAL_SESSION_COOKIE)
    assert "Max-Age=0" in header, "登出响应应下发 Cookie 清除指令"

    resp = await asgi_client.get(
        "/api/portal/auth/me", headers=NO_BEARER, cookies={PORTAL_SESSION_COOKIE: token}
    )
    assert resp.status_code == 401, "登出后旧 Cookie 对应会话已吊销"


async def test_cookie_plane_isolation(asgi_client: httpx.AsyncClient) -> None:
    """AE-54-5 平面隔离：门户 Cookie 打管理面 → 401；管理 Cookie 打门户面 → 401（D26 同源）。"""
    await create_user(asgi_client, username="iso2-alice", password="iso-pass-12345")
    admin_token = await _admin_login(asgi_client)
    resp = await asgi_client.post(
        "/api/portal/auth/login", json={"username": "iso2-alice", "password": "iso-pass-12345"}
    )
    portal_token = resp.json()["token"]

    resp = await asgi_client.get(
        "/api/admin/auth/me",
        headers=NO_BEARER,
        cookies={ADMIN_SESSION_COOKIE: "", PORTAL_SESSION_COOKIE: portal_token},
    )
    assert resp.status_code == 401
    resp = await asgi_client.get(
        "/api/portal/auth/me",
        headers=NO_BEARER,
        cookies={PORTAL_SESSION_COOKIE: "", ADMIN_SESSION_COOKIE: admin_token},
    )
    assert resp.status_code == 401


async def test_authenticated_response_refreshes_cookie(asgi_client: httpx.AsyncClient) -> None:
    """AE-54-6 滑动续期：已认证响应重发同值 Cookie（max_age 重置），Bearer 请求同样续期。"""
    token = await _admin_login(asgi_client)

    resp = await asgi_client.get("/api/admin/auth/me", headers=auth_headers(token))
    assert resp.status_code == 200
    header = _set_cookie_header(resp, ADMIN_SESSION_COOKIE)
    assert header.split(";")[0] == f"{ADMIN_SESSION_COOKIE}={token}", "应同值续期 Cookie"
    assert f"Max-Age={SESSION_COOKIE_MAX_AGE}" in header
