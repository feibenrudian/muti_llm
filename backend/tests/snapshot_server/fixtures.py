"""SRS pytest fixtures：ASGI 直连（UT 用）与随机端口线程（AE/UT 集成用）。"""

import threading
import time
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import httpx
import pytest
import uvicorn

from tests.snapshot_server.app import create_app

REPO_SNAPSHOTS_DIR = Path(__file__).resolve().parents[1] / "snapshots"


@pytest.fixture
def snapshots_dir(tmp_path: Path) -> Path:
    return tmp_path / "snapshots"


@pytest.fixture
def srs_app(snapshots_dir: Path):
    """replay 模式的 SRS 应用（ASGI 直连，不起端口）；测试自行向 store 放快照。"""
    return create_app(snapshots_dir, mode="replay")


@pytest.fixture
async def srs_client(srs_app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=srs_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://srs.test") as client:
        yield client


def _run_srs(snapshots_dir: Path, *, delay_scale: float = 1.0) -> Generator[str, None, None]:
    """线程起真实端口的 replay 模式 SRS，返回 base_url（后端/适配器经真实 HTTP 访问）。"""
    app = create_app(snapshots_dir, mode="replay", delay_scale=delay_scale)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "SRS failed to start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def srs_live(snapshots_dir: Path) -> Generator[str, None, None]:
    """空快照库的 SRS（用于失配/注入类场景）。"""
    yield from _run_srs(snapshots_dir)


@pytest.fixture
def srs_live_seeded() -> Generator[str, None, None]:
    """挂载 tests/snapshots 真实录制库的 SRS（回放即时化提速；时序类用例自行注入 delay_scale）。"""
    yield from _run_srs(REPO_SNAPSHOTS_DIR, delay_scale=0.0)
