"""首次启动初始化：建表、生成服务 API Key 与 Fernet 密钥、内置管理员（多用户 T45）。

服务 Key 认证仍走哈希比对（NFR-3 不变）；同时用 Fernet 存一份可解密副本，
供设置页"掩码展示 + 一键复制"使用（AGENTS.md 的"只回掩码"约束针对上游供应商 Key）。
幂等：已有值不覆盖（UT-05-2）。遗留库（只存过哈希）无副本，重置一次后即有。

内置 admin（FR-17）：无启用管理员时创建（username=admin），随机初始密码仅随
BootstrapResult 返回一次并由启动日志打印；`MUTILLM_ADMIN_PASSWORD` 可显式指定
（e2e/脚本部署用，不打印日志、不返回明文）。
"""

import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db import create_session_factory, init_db
from app.orm import User
from app.repos import get_setting, set_setting
from app.security import (
    encrypt_secret,
    generate_fernet_key,
    generate_password_salt,
    generate_service_key,
    hash_password,
    hash_service_key,
)
from app.settings import settings

logger = logging.getLogger("muti_llm")

KEY_SERVICE_HASH = "service_api_key_hash"
KEY_SERVICE_KEY_ENCRYPTED = "service_api_key_encrypted"
KEY_FERNET = "fernet_key"
KEY_LOG_RETENTION_DAYS = "log_retention_days"
# 两类模型的对外接入开关（"0"=关，缺省/"1"=开）：虚拟模型=Pipeline 名寻址，路由模型=真实模型名透传
KEY_EXPOSE_VIRTUAL_MODELS = "expose_virtual_models"
KEY_EXPOSE_ROUTED_MODELS = "expose_routed_models"

ADMIN_USERNAME = "admin"


@dataclass
class BootstrapResult:
    service_api_key: str | None  # 首次生成时返回明文（仅此一次）；重复启动为 None
    service_api_key_hash: str
    fernet_key: bytes
    admin_user_id: int  # 内置/既有管理员的用户 id（中间件兼容 Key 归属用，D28）
    initial_admin_password: str | None  # 随机生成时返回明文（仅此一次，打启动日志）；否则 None


async def bootstrap(engine: AsyncEngine) -> BootstrapResult:
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        fernet_str = await get_setting(session, KEY_FERNET)
        if not fernet_str:
            fernet_str = generate_fernet_key().decode()
            await set_setting(session, KEY_FERNET, fernet_str)
        fernet_key = fernet_str.encode()

        new_key: str | None = None
        stored_hash = await get_setting(session, KEY_SERVICE_HASH)
        if not stored_hash:
            new_key = generate_service_key()
            stored_hash = hash_service_key(new_key)
            await set_setting(session, KEY_SERVICE_HASH, stored_hash)
            await set_setting(
                session, KEY_SERVICE_KEY_ENCRYPTED, encrypt_secret(new_key, fernet_key)
            )

        admin_user_id, initial_admin_password = await _ensure_admin(session)

        await session.commit()

    return BootstrapResult(
        service_api_key=new_key,
        service_api_key_hash=stored_hash or "",
        fernet_key=fernet_key,
        admin_user_id=admin_user_id,
        initial_admin_password=initial_admin_password,
    )


async def _ensure_admin(session: AsyncSession) -> tuple[int, str | None]:
    """内置管理员引导：无启用管理员时创建 admin；返回 (admin_user_id, 随机初始密码|None)。"""
    existing = (
        await session.execute(
            select(User.id)
            .where(User.role == "admin", User.enabled.is_(True))
            .order_by(User.id)
            .limit(1)
        )
    ).first()
    if existing is not None:
        return int(existing[0]), None

    env_password = settings.admin_password
    password = env_password or secrets.token_urlsafe(12)
    salt = generate_password_salt()
    user = User(
        username=ADMIN_USERNAME,
        display_name="管理员",
        role="admin",
        enabled=True,
        password_hash=hash_password(password, salt, settings.pbkdf2_iterations),
        password_salt=salt,
    )
    session.add(user)
    await session.flush()
    # 显式指定的初始密码不回传（部署方已知）；仅随机生成时由调用方打一次日志
    return user.id, (None if env_password else password)
