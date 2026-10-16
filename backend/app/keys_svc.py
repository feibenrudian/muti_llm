"""用户 API Key 视图服务：掩码序列化、兼容 Key 特例行（admin/users.py 与 portal/keys.py 共用）。

1.2 未规划该文件位置，属 Key 管理域（T52 自门户抽取，避免两处复制同一序列化逻辑）。
明文只在创建响应出现一次；列表仅掩码；/secret 经 Fernet 副本解密供"复制 Key"。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.orm import UserApiKey, utcnow
from app.schemas import KeyOut
from app.security import decrypt_secret, mask_key


def as_utc_datetime(value: Any) -> datetime | None:
    """SQLite 读回的 naive 时间补 UTC（与 orm.UTCDatetime 同口径；聚合函数绕过 TypeDecorator）。"""
    if value is None:
        return None
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def fernet() -> bytes:
    from app.main import app

    return app.state.fernet_key


def key_out(row: UserApiKey, encrypted: str) -> KeyOut:
    masked = "****"
    if encrypted:
        try:
            masked = mask_key(decrypt_secret(encrypted, fernet()))
        except Exception:  # Fernet 密钥轮换后解不开 → 只显示掩码占位
            pass
    return KeyOut(
        id=row.id,
        name=row.name,
        masked=masked,
        enabled=row.enabled,
        created_by=row.created_by,
        last_used_at=as_utc_datetime(row.last_used_at),
        created_at=row.created_at,
    )


async def legacy_key_row(session: AsyncSession) -> KeyOut:
    """兼容 Key（旧服务 Key）只读特例行：归属内置 admin，重置仍走设置页（需求 D21）。"""
    from app.bootstrap import KEY_SERVICE_KEY_ENCRYPTED
    from app.repos import get_setting

    encrypted = await get_setting(session, KEY_SERVICE_KEY_ENCRYPTED)
    masked = "****"
    if encrypted:
        try:
            masked = mask_key(decrypt_secret(encrypted, fernet()))
        except Exception:  # Fernet 密钥轮换后解不开 → 只显示掩码占位
            masked = "****"
    return KeyOut(
        id=0,
        name="兼容 Key（旧服务 Key）",
        masked=masked,
        enabled=True,
        is_legacy=True,
        created_by=0,
        last_used_at=None,
        created_at=utcnow(),
    )
