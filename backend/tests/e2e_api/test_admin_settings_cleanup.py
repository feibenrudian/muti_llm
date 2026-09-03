"""UT-26-1 / AE-26-2：日志清理（保留期 + 手动清空）。"""

import httpx

from app.logging_svc import cleanup_old_logs
from app.orm import ModelCallLog, RequestLog, utcnow
from app.repos import Repository
from tests.conftest import auth_headers
from tests.e2e_api.test_council import seed_council

QUANTUM = "用一句话解释量子纠缠"


async def test_retention_cleanup() -> None:
    """UT-26-1 保留期：31 天前/29 天前两条 → 清理后仅剩 29 天一条，子记录同删。"""
    from datetime import timedelta

    from app.db import create_db_engine, create_session_factory, init_db

    engine = create_db_engine(":memory:")
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        now = utcnow()
        old = await Repository(session, RequestLog).create(
            client_model_field="m", created_at=now - timedelta(days=31)
        )
        recent = await Repository(session, RequestLog).create(
            client_model_field="m", created_at=now - timedelta(days=29)
        )
        for rid in (old.id, recent.id):
            await Repository(session, ModelCallLog).create(
                request_id=rid, role="member", created_at=now
            )
        await session.commit()

        deleted = await cleanup_old_logs(session, 30)
        await session.commit()
        assert deleted == 1

        remaining = await Repository(session, RequestLog).list()
        assert [r.id for r in remaining] == [recent.id]
        calls = await Repository(session, ModelCallLog).list()
        assert [c.request_id for c in calls] == [recent.id]  # 只删了旧请求的子记录
    await engine.dispose()


async def test_manual_clear_and_write_again(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-26-2 手动清空：清空后列表为空，且再写入新 trace 正常。"""
    await seed_council(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/api/admin/playground/run", json={"pipeline_name": "council-v1", "message": QUANTUM}
    )
    assert resp.json()["status_code"] == 200
    assert (await asgi_client.get("/api/admin/traces")).json()["total"] == 1

    resp = await asgi_client.post("/api/admin/traces/clear")
    assert resp.status_code == 200 and resp.json()["cleared"] is True
    assert (await asgi_client.get("/api/admin/traces")).json()["total"] == 0

    # 清空后可正常写入新 trace
    resp = await asgi_client.post(
        "/api/admin/playground/run", json={"pipeline_name": "council-v1", "message": QUANTUM}
    )
    assert resp.json()["status_code"] == 200
    payload = (await asgi_client.get("/api/admin/traces")).json()
    assert payload["total"] == 1 and payload["items"][0]["id"] > 0


async def test_settings_and_key_reset(asgi_client: httpx.AsyncClient, service_key: str) -> None:
    """设置 API：信息展示、保留天数更新、重置 Key 后旧 Key 401 / 新 Key 200。"""
    resp = await asgi_client.get("/api/admin/settings")
    assert resp.status_code == 200
    info = resp.json()
    assert info["version"] == "0.1.0"
    assert info["log_retention_days"] == 30
    assert "started_at" in info

    resp = await asgi_client.patch("/api/admin/settings", json={"log_retention_days": 7})
    assert resp.status_code == 200
    assert (await asgi_client.get("/api/admin/settings")).json()["log_retention_days"] == 7

    resp = await asgi_client.post("/api/admin/settings/service-key/reset")
    assert resp.status_code == 200
    new_key = resp.json()["service_api_key"]
    assert new_key.startswith("sk-local-")

    # 旧 Key 失效、新 Key 可用
    resp = await asgi_client.get("/v1/models", headers=auth_headers(service_key))
    assert resp.status_code == 401
    resp = await asgi_client.get("/v1/models", headers=auth_headers(new_key))
    assert resp.status_code == 200
