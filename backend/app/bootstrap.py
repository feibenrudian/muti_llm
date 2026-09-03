"""首次启动初始化：建表、生成服务 API Key（仅存哈希）与 Fernet 密钥（上游 Key 加解密用）。

幂等：已有值不覆盖（UT-05-2）。服务 Key 明文只在首次生成时返回一次，不落库。
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import create_session_factory, init_db
from app.repos import get_setting, set_setting
from app.security import generate_fernet_key, generate_service_key, hash_service_key

KEY_SERVICE_HASH = "service_api_key_hash"
KEY_FERNET = "fernet_key"
KEY_LOG_RETENTION_DAYS = "log_retention_days"


@dataclass
class BootstrapResult:
    service_api_key: str | None  # 首次生成时返回明文（仅此一次）；重复启动为 None
    service_api_key_hash: str
    fernet_key: bytes


async def bootstrap(engine: AsyncEngine) -> BootstrapResult:
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        new_key: str | None = None

        stored_hash = await get_setting(session, KEY_SERVICE_HASH)
        if not stored_hash:
            new_key = generate_service_key()
            stored_hash = hash_service_key(new_key)
            await set_setting(session, KEY_SERVICE_HASH, stored_hash)

        fernet_str = await get_setting(session, KEY_FERNET)
        if not fernet_str:
            fernet_str = generate_fernet_key().decode()
            await set_setting(session, KEY_FERNET, fernet_str)

        await session.commit()

    return BootstrapResult(
        service_api_key=new_key,
        service_api_key_hash=stored_hash,
        fernet_key=fernet_str.encode(),
    )
