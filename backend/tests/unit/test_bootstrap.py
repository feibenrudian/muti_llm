"""UT-05-1..2：首次启动初始化（bootstrap）。"""

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from app.bootstrap import KEY_FERNET, KEY_SERVICE_HASH, bootstrap
from app.db import create_db_engine, create_session_factory
from app.repos import get_setting
from app.security import decrypt_secret, encrypt_secret, verify_service_key


async def test_first_boot_generates_service_key(tmp_path: Path) -> None:
    """UT-05-1 首次启动生成：app_settings 存在 key_hash 且通过 T04 校验；fernet_key 可加解密。"""
    engine = create_db_engine(str(tmp_path / "boot.db"))
    result = await bootstrap(engine)

    assert result.service_api_key is not None
    assert result.service_api_key.startswith("sk-local-")

    factory = create_session_factory(engine)
    async with factory() as session:
        stored_hash = await get_setting(session, KEY_SERVICE_HASH)
        fernet_str = await get_setting(session, KEY_FERNET)

    assert stored_hash is not None
    assert verify_service_key(result.service_api_key, stored_hash)
    assert fernet_str is not None
    assert fernet_str.encode() == result.fernet_key

    # fernet_key 与 T04 加解密往返兼容
    ciphertext = encrypt_secret("sk-upstream-xyz", result.fernet_key)
    assert decrypt_secret(ciphertext, result.fernet_key) == "sk-upstream-xyz"

    await engine.dispose()


async def test_reboot_keeps_existing_key(tmp_path: Path) -> None:
    """UT-05-2 重复启动不覆盖：二次启动哈希不变，且不返回新明文。"""
    db_path = str(tmp_path / "boot.db")

    engine1 = create_db_engine(db_path)
    first = await bootstrap(engine1)
    await engine1.dispose()

    engine2: AsyncEngine = create_db_engine(db_path)
    second = await bootstrap(engine2)
    factory = create_session_factory(engine2)
    async with factory() as session:
        hash_after = await get_setting(session, KEY_SERVICE_HASH)

    assert second.service_api_key is None  # 不再生成
    assert verify_service_key(first.service_api_key, hash_after)  # 哈希未变
    await engine2.dispose()
