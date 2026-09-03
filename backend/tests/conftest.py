"""共享 fixtures：ASGI 客户端（跑真实 lifespan、临时库）+ SRS fixtures 汇出。"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

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


@pytest.fixture
async def asgi_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """通过 ASGI 直连后端：真实执行 lifespan（建表/初始化），数据库指向临时文件。"""
    from app import settings as settings_module
    from app.main import app

    monkeypatch.setattr(settings_module.settings, "database_path", str(tmp_path / "asgi_test.db"))
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://backend.test") as client:
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
def backend_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    """线程起真实 TCP 的后端（uvicorn 自跑 lifespan，独立临时库）。

    返回 (base_url, service_key)。ASGITransport 不传播客户端断开，
    取消/断开类场景（AE-12-3）必须走真实网络栈。
    """
    import threading
    import time as time_mod

    import uvicorn

    from app import settings as settings_module
    from app.main import app

    monkeypatch.setattr(settings_module.settings, "database_path", str(tmp_path / "live.db"))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time_mod.monotonic() + 10
    while not server.started and time_mod.monotonic() < deadline:
        time_mod.sleep(0.01)
    assert server.started, "backend live server failed to start"
    port = server.servers[0].sockets[0].getsockname()[1]
    key = app.state.service_api_key
    assert key, "service key not generated"
    yield f"http://127.0.0.1:{port}", key
    server.should_exit = True
    thread.join(timeout=5)
