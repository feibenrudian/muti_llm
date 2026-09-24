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
    """设置 API：信息展示（含接入开关默认开）、保留天数/开关更新、重置 Key 后旧 401 / 新 200。"""
    resp = await asgi_client.get("/api/admin/settings")
    assert resp.status_code == 200
    info = resp.json()
    assert info["version"] == "0.1.0"
    assert info["log_retention_days"] == 30
    assert info["expose_virtual_models"] is True
    assert info["expose_routed_models"] is True
    assert "started_at" in info

    resp = await asgi_client.patch("/api/admin/settings", json={"log_retention_days": 7})
    assert resp.status_code == 200
    assert (await asgi_client.get("/api/admin/settings")).json()["log_retention_days"] == 7

    # 两类接入开关分别可配、可回读
    resp = await asgi_client.patch(
        "/api/admin/settings", json={"expose_virtual_models": False, "expose_routed_models": False}
    )
    assert resp.status_code == 200
    info = (await asgi_client.get("/api/admin/settings")).json()
    assert info["expose_virtual_models"] is False
    assert info["expose_routed_models"] is False
    resp = await asgi_client.patch("/api/admin/settings", json={"expose_routed_models": True})
    info = (await asgi_client.get("/api/admin/settings")).json()
    assert info["expose_virtual_models"] is False  # 未触及的开关保持原值
    assert info["expose_routed_models"] is True

    resp = await asgi_client.post("/api/admin/settings/service-key/reset")
    assert resp.status_code == 200
    new_key = resp.json()["service_api_key"]
    assert new_key.startswith("sk-local-")

    # 旧 Key 失效、新 Key 可用
    resp = await asgi_client.get("/v1/models", headers=auth_headers(service_key))
    assert resp.status_code == 401
    resp = await asgi_client.get("/v1/models", headers=auth_headers(new_key))
    assert resp.status_code == 200


async def test_service_key_reveal(asgi_client: httpx.AsyncClient, service_key: str) -> None:
    """AE-28-3 Key 展示：首启即可取（掩码格式+明文可认证）；重置后同步；遗留库 available=false。"""
    resp = await asgi_client.get("/api/admin/settings/service-key")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["service_api_key"] == service_key  # 与首启生成的明文一致
    assert data["masked"].startswith("sk-local-")
    assert data["masked"].endswith(f"***{service_key[-4:]}")
    assert "***" in data["masked"]
    assert service_key not in data["masked"]  # 掩码不含完整明文

    # 副本明文可用于 /v1 认证
    resp = await asgi_client.get("/v1/models", headers=auth_headers(data["service_api_key"]))
    assert resp.status_code == 200

    # 重置后副本同步更新
    new_key = (await asgi_client.post("/api/admin/settings/service-key/reset")).json()[
        "service_api_key"
    ]
    data = (await asgi_client.get("/api/admin/settings/service-key")).json()
    assert data["available"] is True
    assert data["service_api_key"] == new_key

    # 遗留库：只存过哈希、无加密副本 → available=false
    from app.bootstrap import KEY_SERVICE_KEY_ENCRYPTED
    from app.main import app
    from app.orm import AppSetting

    async with app.state.session_factory() as session:
        row = await session.get(AppSetting, KEY_SERVICE_KEY_ENCRYPTED)
        assert row is not None
        await session.delete(row)
        await session.commit()
    data = (await asgi_client.get("/api/admin/settings/service-key")).json()
    assert data["available"] is False
    assert data["service_api_key"] is None
