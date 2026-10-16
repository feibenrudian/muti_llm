"""共享 fixtures：ASGI 客户端（跑真实 lifespan、临时库）+ SRS fixtures 汇出。

多用户测试基建（T46/D29）：asgi_client 默认携带"直插库铸造"的管理员会话头，
既有 /api/admin 调用点零改动；/v1 用例按 auth_headers(service_key) 逐请求覆盖
（两平面令牌不通用）。断言"未认证"的用例必须显式传 headers={"Authorization": ""}。
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from tests.mock_anthropic import anthropic_mock  # noqa: F401

# 让 snapshot_server/fixtures.py 中的 fixtures 对全部用例可见
from tests.snapshot_server.fixtures import (  # noqa: F401
    snapshots_dir,
    srs_app,
    srs_client,
    srs_live,
    srs_live_seeded,
)


async def mint_admin_session(app: Any) -> str:
    """直插库铸造管理会话（不跑 PBKDF2、不走登录端点）；返回明文令牌。"""
    from app.repos import create_admin_session

    token = f"test-session-{uuid.uuid4().hex}"
    async with app.state.session_factory() as session:
        await create_admin_session(session, app.state.admin_user_id, token)
        await session.commit()
    return token


@pytest.fixture
async def asgi_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """通过 ASGI 直连后端：真实执行 lifespan（建表/初始化），数据库指向临时文件。

    默认头为管理员会话（见模块 docstring）；pbkdf2_iterations 调低（D30，加速 bootstrap）。
    """
    from app import settings as settings_module
    from app.main import app

    monkeypatch.setattr(settings_module.settings, "database_path", str(tmp_path / "asgi_test.db"))
    monkeypatch.setattr(settings_module.settings, "pbkdf2_iterations", 1000)
    # 路由视图的上游列表缓存是进程级的，跨用例（临时库 id 重叠）会串味 → 每用例清空
    from app.gateways.namespaces import clear_route_list_cache

    clear_route_list_cache()
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://backend.test") as client:
            client.headers["Authorization"] = f"Bearer {await mint_admin_session(app)}"
            yield client


@pytest.fixture
def service_key(asgi_client: httpx.AsyncClient) -> str:
    """本次测试会话的服务 API Key（asgi_client 的 lifespan 首次生成，存于 app.state）。"""
    from app.main import app

    key = getattr(app.state, "service_api_key", None)
    assert key, "service key not generated"
    return key


def auth_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def backend_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str, str]]:
    """线程起真实 TCP 的后端（uvicorn 自跑 lifespan，独立临时库）。

    返回 (base_url, service_key, admin_token)。ASGITransport 不传播客户端断开，
    取消/断开类场景（AE-12-3）必须走真实网络栈。admin_token 为真实登录端点换取的
    管理会话（密码经 settings 注入），供经管理 API 造数的用例携带。
    """
    import threading
    import time as time_mod

    import uvicorn

    from app import settings as settings_module
    from app.main import app

    monkeypatch.setattr(settings_module.settings, "database_path", str(tmp_path / "live.db"))
    monkeypatch.setattr(settings_module.settings, "pbkdf2_iterations", 1000)
    monkeypatch.setattr(settings_module.settings, "admin_password", "live-admin-pass")
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time_mod.monotonic() + 10
    while not server.started and time_mod.monotonic() < deadline:
        time_mod.sleep(0.01)
    assert server.started, "backend live server failed to start"
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    key = app.state.service_api_key
    assert key, "service key not generated"
    login = httpx.post(
        f"{base}/api/admin/auth/login",
        json={"username": "admin", "password": "live-admin-pass"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    yield base, key, token
    server.should_exit = True
    thread.join(timeout=5)
