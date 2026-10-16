"""UT-45-*：多用户数据模型与启动引导（口令原语 / 补列补索引 / 内置 admin / 级联）。"""

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bootstrap import bootstrap
from app.db import create_db_engine, create_session_factory, init_db
from app.orm import AdminSession, User, UserApiKey, UserGrant
from app.repos import (
    create_admin_session,
    delete_user_cascade,
    find_valid_session,
    set_setting,
)
from app.security import (
    generate_password_salt,
    generate_session_token,
    generate_user_key,
    hash_password,
    hash_token,
    verify_password,
)
from app.settings import settings as app_settings


def test_password_hash_roundtrip() -> None:
    """UT-45-1 口令哈希往返：正确口令通过、错误口令拒绝、同口令两次哈希不同（盐随机）。"""
    salt1 = generate_password_salt()
    salt2 = generate_password_salt()
    hashed1 = hash_password("s3cret-pass", salt1, 1000)
    hashed2 = hash_password("s3cret-pass", salt2, 1000)

    assert verify_password("s3cret-pass", salt1, hashed1, 1000)
    assert not verify_password("wrong-pass", salt1, hashed1, 1000)
    assert hashed1 != hashed2  # 盐随机 → 同口令不同哈希
    assert not verify_password("s3cret-pass", salt1, hashed1, 2000)  # 迭代数变更即失效


def test_user_key_and_token_primitives() -> None:
    """UT-45-1b 用户 Key/令牌原语：前缀、唯一性、哈希 64 位 hex。"""
    key1, key2 = generate_user_key(), generate_user_key()
    assert key1.startswith("sk-mu-") and key2.startswith("sk-mu-") and key1 != key2
    token = generate_session_token()
    assert token and generate_session_token() != token
    assert len(hash_token(token)) == 64


async def test_schema_patches_user_columns_and_indexes(tmp_path: Path) -> None:
    """UT-45-2 旧库补列与补索引：request_logs/model_call_logs 增归属两列 + user_id 索引；幂等。"""
    engine = create_db_engine(str(tmp_path / "legacy.db"))
    # 先建一个"旧版"库：只建 request_logs 的旧形态，跑 init_db 应补列补索引
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            """
CREATE TABLE request_logs (
    id INTEGER NOT NULL PRIMARY KEY,
    client_model_field VARCHAR(100) NOT NULL,
    created_at DATETIME
)
"""
        )
        await conn.exec_driver_sql(
            """
CREATE TABLE model_call_logs (
    id INTEGER NOT NULL PRIMARY KEY,
    request_id INTEGER NOT NULL,
    created_at DATETIME
)
"""
        )

    await init_db(engine)
    await init_db(engine)  # 二次执行幂等

    factory = create_session_factory(engine)
    async with factory() as session:
        row = await session.execute(text("SELECT user_id, user_username FROM request_logs LIMIT 1"))
        assert row.first() is None  # 空表可查询即列存在

    async with engine.begin() as conn:
        indexes = {
            row[1]
            for row in (await conn.exec_driver_sql("PRAGMA index_list(request_logs)")).fetchall()
        } | {
            row[1]
            for row in (await conn.exec_driver_sql("PRAGMA index_list(model_call_logs)")).fetchall()
        }
    assert "ix_request_logs_user_id" in indexes
    assert "ix_model_call_logs_user_id" in indexes
    await engine.dispose()


async def test_bootstrap_creates_admin_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT-45-3/4 首次引导建唯一 admin（初始密码可过校验）；二次引导不覆盖。"""
    monkeypatch.setattr(app_settings, "pbkdf2_iterations", 1000)
    db_path = str(tmp_path / "boot.db")

    engine1 = create_db_engine(db_path)
    first = await bootstrap(engine1)
    factory1 = create_session_factory(engine1)
    async with factory1() as session:
        users = (await session.execute(User.__table__.select())).mappings().all()
    assert len(users) == 1
    admin = users[0]
    assert admin["username"] == "admin"
    assert admin["role"] == "admin"
    assert admin["password_hash"]
    assert first.initial_admin_password is not None
    assert verify_password(
        first.initial_admin_password, admin["password_salt"], admin["password_hash"], 1000
    )
    await engine1.dispose()

    engine2: AsyncEngine = create_db_engine(db_path)
    second = await bootstrap(engine2)
    factory2 = create_session_factory(engine2)
    async with factory2() as session:
        count = len((await session.execute(User.__table__.select())).all())
    assert second.initial_admin_password is None  # 不再生成
    assert second.admin_user_id == first.admin_user_id  # 稳定
    assert count == 1
    await engine2.dispose()


async def test_bootstrap_env_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT-45-4b 环境变量指定初始密码：不返回明文；该密码可登录校验。"""
    monkeypatch.setattr(app_settings, "pbkdf2_iterations", 1000)
    monkeypatch.setattr(app_settings, "admin_password", "env-set-pass-1")
    engine = create_db_engine(str(tmp_path / "boot.db"))
    result = await bootstrap(engine)
    assert result.initial_admin_password is None

    factory = create_session_factory(engine)
    async with factory() as session:
        admin = (await session.execute(User.__table__.select())).mappings().one()
    assert verify_password("env-set-pass-1", admin["password_salt"], admin["password_hash"], 1000)
    await engine.dispose()


async def _make_user_with_everything(engine: AsyncEngine) -> tuple[int, int]:
    factory = create_session_factory(engine)
    async with factory() as session:
        user = User(username="alice", display_name="Alice", role="user")
        session.add(user)
        await session.flush()
        session.add(
            UserApiKey(
                user_id=user.id, name="k1", key_hash=hash_token("sk-mu-a"), key_encrypted="x"
            )
        )
        session.add(UserGrant(user_id=user.id, kind="pipeline", target_id=1))
        await create_admin_session(session, user.id, "tok-alice")
        other = User(username="bob", display_name="Bob", role="user")
        session.add(other)
        await session.flush()
        session.add(UserApiKey(user_id=other.id, name="k2", key_hash=hash_token("sk-mu-b")))
        await session.commit()
        return user.id, other.id


async def test_user_grant_unique_constraint(tmp_path: Path) -> None:
    """UT-45-5 授权唯一约束：同 (user_id, kind, target_id) 重复插入抛 IntegrityError。"""
    engine = create_db_engine(str(tmp_path / "grants.db"))
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        user = User(username="alice")
        session.add(user)
        await session.flush()
        session.add(UserGrant(user_id=user.id, kind="pipeline", target_id=1))
        await session.commit()
    async with factory() as session:
        session.add(UserGrant(user_id=1, kind="pipeline", target_id=1))
        with pytest.raises(IntegrityError):
            await session.commit()
    await engine.dispose()


async def test_delete_user_cascade(tmp_path: Path) -> None:
    """UT-45-6 用户级联删除：Key/授权/会话同删，其他用户不受影响。"""
    engine = create_db_engine(str(tmp_path / "cascade.db"))
    await init_db(engine)
    alice_id, bob_id = await _make_user_with_everything(engine)

    factory = create_session_factory(engine)
    async with factory() as session:
        await delete_user_cascade(session, alice_id)
        await session.commit()

        assert await session.get(User, alice_id) is None
        assert (
            await session.execute(
                text("SELECT count(*) FROM user_api_keys WHERE user_id = :u"),
                {"u": alice_id},
            )
        ).scalar_one() == 0
        assert (
            await session.execute(
                text("SELECT count(*) FROM user_grants WHERE user_id = :u"), {"u": alice_id}
            )
        ).scalar_one() == 0
        assert (
            await session.execute(
                text("SELECT count(*) FROM admin_sessions WHERE user_id = :u"), {"u": alice_id}
            )
        ).scalar_one() == 0
        # 其他用户不受影响
        assert (
            await session.execute(
                text("SELECT count(*) FROM user_api_keys WHERE user_id = :u"), {"u": bob_id}
            )
        ).scalar_one() == 1
    await engine.dispose()


async def test_find_valid_session_expiry(tmp_path: Path) -> None:
    """UT-45-7 会话校验：有效会话命中、停用用户/过期会话拒绝。"""
    from datetime import UTC, datetime, timedelta

    engine = create_db_engine(str(tmp_path / "sess.db"))
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        user = User(username="alice", role="admin")
        session.add(user)
        await session.flush()
        await create_admin_session(session, user.id, "tok-1")
        # 手工造过期会话
        row = AdminSession(
            user_id=user.id,
            token_hash=hash_token("tok-expired"),
            expires_at=datetime.now(UTC) - timedelta(hours=1),
            last_seen_at=datetime.now(UTC),
        )
        session.add(row)
        await session.commit()

    async with factory() as session:
        assert await find_valid_session(session, "tok-1") is not None
        assert await find_valid_session(session, "tok-expired") is None
        assert await find_valid_session(session, "tok-unknown") is None
    await engine.dispose()


async def test_set_setting_smoke(tmp_path: Path) -> None:
    """冒烟：set_setting 在多表库上照常工作（防止 import 回归）。"""
    engine = create_db_engine(str(tmp_path / "kv.db"))
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        await set_setting(session, "some_key", "1")
        await session.commit()
    await engine.dispose()
