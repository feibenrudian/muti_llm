"""AE-29-1：SPA 静态托管（依赖 frontend/dist，由 make e2e 前置 build 保证）。"""

from pathlib import Path

import httpx
import pytest

DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"


async def test_spa_served(asgi_client: httpx.AsyncClient) -> None:
    """AE-29-1 GET / → 200 text/html（frontend/dist 存在时）。"""
    if not (DIST / "index.html").exists():
        pytest.skip("frontend not built; run `cd frontend && npm run build` first")
    resp = await asgi_client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert b'<div id="root">' in resp.content

    # 非 SPA 保留路径不受 catch-all 影响
    assert (await asgi_client.get("/health")).json() == {"status": "ok"}
    assert (await asgi_client.get("/api/admin/nothing")).status_code in (404, 405)
