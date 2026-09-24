"""首次启动初始化：建表、生成服务 API Key 与 Fernet 密钥。

服务 Key 认证仍走哈希比对（NFR-3 不变）；同时用 Fernet 存一份可解密副本，
供设置页"掩码展示 + 一键复制"使用（AGENTS.md 的"只回掩码"约束针对上游供应商 Key）。
幂等：已有值不覆盖（UT-05-2）。遗留库（只存过哈希）无副本，重置一次后即有。
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import create_session_factory, init_db
from app.repos import get_setting, set_setting
from app.security import (
    encrypt_secret,
    generate_fernet_key,
    generate_service_key,
    hash_service_key,
)

KEY_SERVICE_HASH = "service_api_key_hash"
KEY_SERVICE_KEY_ENCRYPTED = "service_api_key_encrypted"
KEY_FERNET = "fernet_key"
KEY_LOG_RETENTION_DAYS = "log_retention_days"
# 两类模型的对外接入开关（"0"=关，缺省/"1"=开）：虚拟模型=Pipeline 名寻址，路由模型=真实模型名透传
KEY_EXPOSE_VIRTUAL_MODELS = "expose_virtual_models"
KEY_EXPOSE_ROUTED_MODELS = "expose_routed_models"


@dataclass
class BootstrapResult:
    service_api_key: str | None  # 首次生成时返回明文（仅此一次）；重复启动为 None
    service_api_key_hash: str
    fernet_key: bytes


async def bootstrap(engine: AsyncEngine) -> BootstrapResult:
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        fernet_str = await get_setting(session, KEY_FERNET)
        if not fernet_str:
            fernet_str = generate_fernet_key().decode()
            await set_setting(session, KEY_FERNET, fernet_str)

        new_key: str | None = None
        stored_hash = await get_setting(session, KEY_SERVICE_HASH)
        if not stored_hash:
            new_key = generate_service_key()
            stored_hash = hash_service_key(new_key)
            await set_setting(session, KEY_SERVICE_HASH, stored_hash)
            await set_setting(
                session, KEY_SERVICE_KEY_ENCRYPTED, encrypt_secret(new_key, fernet_str.encode())
            )

        await session.commit()

    return BootstrapResult(
        service_api_key=new_key,
        service_api_key_hash=stored_hash,
        fernet_key=fernet_str.encode(),
    )
