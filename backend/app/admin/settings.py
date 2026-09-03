"""服务设置 API：运行信息、日志保留天数、服务 API Key 展示/重置（认证比对仍走哈希）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap import KEY_LOG_RETENTION_DAYS, KEY_SERVICE_KEY_ENCRYPTED
from app.deps import get_session
from app.logging_svc import cleanup_old_logs
from app.repos import get_setting, set_setting
from app.security import (
    decrypt_secret,
    encrypt_secret,
    generate_service_key,
    hash_service_key,
    mask_service_key,
)
from app.settings import settings as app_settings

router = APIRouter(prefix="/settings", tags=["admin-settings"])


class SettingsUpdate(BaseModel):
    log_retention_days: int | None = Field(default=None, ge=1, le=3650)


@router.get("")
async def get_settings_info(
    request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    db_path = Path(app_settings.database_path).resolve()
    retention = await get_setting(session, KEY_LOG_RETENTION_DAYS)
    return {
        "version": request.app.version,
        "started_at": getattr(request.app.state, "started_at", datetime.now(UTC)).isoformat(),
        "database_path": str(db_path),
        "database_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "log_retention_days": int(retention) if retention else app_settings.log_retention_days,
    }


@router.patch("")
async def update_settings(
    body: SettingsUpdate, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    if body.log_retention_days is not None:
        await set_setting(session, KEY_LOG_RETENTION_DAYS, str(body.log_retention_days))
        await session.commit()
    return {"ok": True}


@router.get("/service-key")
async def get_service_key(
    request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """设置页展示：掩码 + 可复制明文（Fernet 加密副本解出）。

    /v1 认证比对仍走哈希不变；遗留库（历史版本只存过哈希）无副本 → available=false，
    重置一次后即可随时复制。
    """
    encrypted = await get_setting(session, KEY_SERVICE_KEY_ENCRYPTED)
    if not encrypted:
        return {"available": False, "masked": "", "service_api_key": None}
    try:
        key = decrypt_secret(encrypted, request.app.state.fernet_key)
    except Exception:  # 密钥轮换后无法解密 → 视同无副本
        return {"available": False, "masked": "", "service_api_key": None}
    return {"available": True, "masked": mask_service_key(key), "service_api_key": key}


@router.post("/service-key/reset")
async def reset_service_key(
    request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, str]:
    """重置服务 API Key：旧 Key 立即失效；新明文仅本次响应返回一次。"""
    from app.bootstrap import KEY_SERVICE_HASH

    new_key = generate_service_key()
    new_hash = hash_service_key(new_key)
    await set_setting(session, KEY_SERVICE_HASH, new_hash)
    await set_setting(
        session, KEY_SERVICE_KEY_ENCRYPTED, encrypt_secret(new_key, request.app.state.fernet_key)
    )
    await session.commit()
    request.app.state.service_api_key = new_key
    request.app.state.service_api_key_hash = new_hash
    return {"service_api_key": new_key}


@router.post("/cleanup")
async def run_cleanup(
    request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, int]:
    """手动触发按保留期清理（lifespan 启动时也会自动执行一次）。"""
    from app.bootstrap import KEY_LOG_RETENTION_DAYS

    retention = await get_setting(session, KEY_LOG_RETENTION_DAYS)
    days = int(retention) if retention else app_settings.log_retention_days
    deleted = await cleanup_old_logs(session, days)
    await session.commit()
    return {"deleted": deleted, "retention_days": days}
