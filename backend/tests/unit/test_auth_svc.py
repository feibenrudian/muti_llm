"""UT-46-1..3：管理会话签发/校验/续期/脱敏（auth_svc 领域服务，打桩库）。"""

from pathlib import Path

from sqlalchemy import select

from app.auth_svc import verify_session
from app.db import create_db_engine, create_session_factory, init_db
from app.orm import AdminSession
from app.repos import create_admin_session


async def test_session_issue_and_verify(tmp_path: Path) -> None:
    """UT-46-1 会话签发与校验：明文令牌可校验、库内只存哈希；未知名/错令牌拒绝。"""
    engine = create_db_engine(str(tmp_path / "s.db"))
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        from app.orm import User

        user = User(username="admin", role="admin")
        session.add(user)
        await session.flush()
        token = "plain-token-abc"
        row = await create_admin_session(session, user.id, token)
        await session.commit()
        stored = (
            await session.execute(select(AdminSession).where(AdminSession.id == row.id))
        ).scalar_one()
        assert stored.token_hash != token  # 明文不落库
        assert len(stored.token_hash) == 64

    async with factory() as session:
        ok = await verify_session(session, token)
        assert not isinstance(ok, Exception)
        assert ok.username == "admin"
        bad = await verify_session(session, "wrong-token")
        assert bad.status == 401 and bad.code == "unauthorized"
    await engine.dispose()


async def test_session_sliding_renewal(tmp_path: Path) -> None:
    """UT-46-2 滑动续期：校验后 expires_at/last_seen_at 前移。"""
    engine = create_db_engine(str(tmp_path / "s.db"))
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        from app.orm import User

        user = User(username="admin", role="admin")
        session.add(user)
        await session.flush()
        await create_admin_session(session, user.id, "tok")
        await session.commit()

    async with factory() as session:
        first = (await session.execute(select(AdminSession))).scalar_one()
        expires_before = first.expires_at
        await verify_session(session, "tok")
        await session.commit()  # 续期落库

    async with factory() as session:
        after = (await session.execute(select(AdminSession))).scalar_one()
        assert after.expires_at > expires_before
    await engine.dispose()


async def test_session_verify_masks_token(tmp_path: Path) -> None:
    """UT-46-3 令牌脱敏：verify_session 返回的 Caller 不含明文令牌，库内无明文。"""
    engine = create_db_engine(str(tmp_path / "s.db"))
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        from app.orm import User

        user = User(username="admin", role="admin")
        session.add(user)
        await session.flush()
        await create_admin_session(session, user.id, "secret-tok")
        await session.commit()

    async with factory() as session:
        caller = await verify_session(session, "secret-tok")
        assert "secret-tok" not in repr(caller)
        rows = (await session.execute(select(AdminSession))).scalars().all()
        assert all("secret-tok" not in r.token_hash for r in rows)
    await engine.dispose()


async def test_verify_session_role_gate(tmp_path: Path) -> None:
    """UT-52-1 角色闸门参数化：user 会话过管理面（默认）403，过门户面（require_role=None）放行。"""
    engine = create_db_engine(str(tmp_path / "s.db"))
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        from app.orm import User

        user = User(username="alice", role="user")
        session.add(user)
        await session.flush()
        await create_admin_session(session, user.id, "alice-tok")
        await session.commit()

    async with factory() as session:
        rejected = await verify_session(session, "alice-tok")  # 默认 require_role="admin"
        assert rejected.status == 403 and rejected.code == "forbidden"
        ok = await verify_session(session, "alice-tok", require_role=None)
        assert ok.role == "user" and ok.via == "session" and ok.key_id is None
    await engine.dispose()
