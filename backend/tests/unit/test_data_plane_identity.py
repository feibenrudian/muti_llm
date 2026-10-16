"""UT-48-1..4：数据面身份解析与归属穿参（auth_svc / logging_svc，打桩 app 与库）。"""

from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.auth_svc import AuthError, Caller, resolve_data_plane_identity
from app.db import create_db_engine, create_session_factory, init_db
from app.logging_svc import record_call, start_request
from app.orm import ModelCallLog, RequestLog, User, UserApiKey
from app.security import generate_service_key, hash_service_key, hash_token


class _StubApp:
    """最小 app 替身：session_factory + 兼容 Key 哈希 + admin id（resolve 的全部依赖）。"""

    def __init__(self, engine: Any, legacy_key: str, admin_user_id: int) -> None:
        self.state = type("State", (), {})()
        self.state.session_factory = create_session_factory(engine)
        self.state.service_api_key_hash = hash_service_key(legacy_key)
        self.state.admin_user_id = admin_user_id


async def _seed(engine: Any, *, user_enabled: bool = True, key_enabled: bool = True) -> int:
    factory = create_session_factory(engine)
    async with factory() as session:
        user = User(username="alice", role="user", enabled=user_enabled)
        session.add(user)
        await session.flush()
        session.add(
            UserApiKey(
                user_id=user.id,
                name="k",
                key_hash=hash_token("sk-mu-test-key"),
                key_encrypted="",
                enabled=key_enabled,
            )
        )
        await session.commit()
        return user.id


async def test_resolve_key_paths(tmp_path: Path) -> None:
    """UT-48-1 Key 解析：有效 → via=key；停用 Key/不存在 → 401；用户停用 → 403。"""
    engine = create_db_engine(str(tmp_path / "a.db"))
    await init_db(engine)
    legacy = generate_service_key()
    user_id = await _seed(engine)
    app = _StubApp(engine, legacy, admin_user_id=1)

    ok = await resolve_data_plane_identity(app, "sk-mu-test-key")
    assert isinstance(ok, Caller) and ok.via == "key" and ok.user_id == user_id

    legacy_caller = await resolve_data_plane_identity(app, legacy)
    assert isinstance(legacy_caller, Caller) and legacy_caller.via == "legacy"

    for token in ("sk-mu-unknown", ""):
        err = await resolve_data_plane_identity(app, token)
        assert isinstance(err, AuthError) and err.status == 401

    # Key 停用 → 401；用户停用 → 403
    factory = create_session_factory(engine)
    async with factory() as session:
        row = (
            await session.execute(select(UserApiKey).where(UserApiKey.user_id == user_id))
        ).scalar_one()
        row.enabled = False
        await session.commit()
    assert (await resolve_data_plane_identity(app, "sk-mu-test-key")).status == 401

    async with factory() as session:
        row = (
            await session.execute(select(UserApiKey).where(UserApiKey.user_id == user_id))
        ).scalar_one()
        row.enabled = True
        user = await session.get(User, user_id)
        user.enabled = False
        await session.commit()
    err = await resolve_data_plane_identity(app, "sk-mu-test-key")
    assert isinstance(err, AuthError) and err.status == 403 and err.code == "user_disabled"
    await engine.dispose()


async def test_legacy_short_circuit_zero_query(tmp_path: Path, monkeypatch: Any) -> None:
    """UT-48-2 兼容 Key 短路：命中旧哈希不查 users/user_api_keys（打桩计数）。"""
    engine = create_db_engine(str(tmp_path / "a.db"))
    await init_db(engine)
    legacy = generate_service_key()
    app = _StubApp(engine, legacy, admin_user_id=7)

    import app.repos as repos

    calls = {"key": 0, "user": 0}
    orig_key = repos.find_user_key_by_hash

    async def spy_key(*args: Any, **kwargs: Any) -> None:
        calls["key"] += 1
        return await orig_key(*args, **kwargs)

    monkeypatch.setattr("app.auth_svc.find_user_key_by_hash", spy_key)
    caller = await resolve_data_plane_identity(app, legacy)
    assert isinstance(caller, Caller)
    assert calls["key"] == 0  # 零查询短路
    await engine.dispose()


async def test_attribution_kwargs_roundtrip(tmp_path: Path) -> None:
    """UT-48-3 归属穿参：start_request/record_call 携带 user_id/user_username 落库；缺省 0/""。"""
    engine = create_db_engine(str(tmp_path / "a.db"))
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as session:
        trace = await start_request(
            session,
            client_model_field="council-v1",
            pipeline_name="council-v1",
            messages=[],
            params={},
            user_id=42,
            user_username="alice",
        )
        await record_call(
            session,
            request_id=trace.id,
            role="member",
            model_id=1,
            upstream_model_id="m",
            provider_name="p",
            request_payload={},
            user_id=42,
            user_username="alice",
        )
        # 缺省 → 0/""（历史语义）
        trace2 = await start_request(
            session,
            client_model_field="m",
            pipeline_name="",
            messages=[],
            params={},
        )
        await session.commit()

    async with factory() as session:
        row = await session.get(RequestLog, trace.id)
        assert row.user_id == 42 and row.user_username == "alice"
        call = (
            await session.execute(select(ModelCallLog).where(ModelCallLog.request_id == trace.id))
        ).scalar_one()
        assert call.user_id == 42 and call.user_username == "alice"
        history = await session.get(RequestLog, trace2.id)
        assert history.user_id == 0 and history.user_username == ""
    await engine.dispose()


async def test_key_usage_throttled(tmp_path: Path, monkeypatch: Any) -> None:
    """UT-48-4 节流写：60s 窗口内第二次认证不再写 last_used_at。"""
    import app.auth_svc as svc

    engine = create_db_engine(str(tmp_path / "a.db"))
    await init_db(engine)
    app = _StubApp(engine, generate_service_key(), admin_user_id=1)
    await _seed(engine)
    monkeypatch.setattr(svc, "_last_usage_write", {})  # 清进程级记账

    first = await resolve_data_plane_identity(app, "sk-mu-test-key")
    assert isinstance(first, Caller)
    factory = create_session_factory(engine)
    async with factory() as session:
        row = (await session.execute(select(UserApiKey))).scalars().one()
        assert row.last_used_at is not None
        first_ts = row.last_used_at

    second = await resolve_data_plane_identity(app, "sk-mu-test-key")
    assert isinstance(second, Caller)
    async with factory() as session:
        row = (await session.execute(select(UserApiKey))).scalars().one()
        assert row.last_used_at == first_ts  # 节流：未再写
    await engine.dispose()
