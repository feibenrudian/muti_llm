"""AE-01-1 冒烟：/health 可用。"""

import httpx


async def test_health_returns_ok(asgi_client: httpx.AsyncClient) -> None:
    """AE-01-1 GET /health → 200 {"status":"ok"}。"""
    resp = await asgi_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
