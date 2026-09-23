"""UT-32-2..5：旧库补列（_ensure_schema 幂等）、round 落库往返、request_logs/pipelines 补列。"""

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db import create_db_engine, create_session_factory, init_db
from app.logging_svc import finish_request, record_call, start_request
from app.orm import LlmModel, ModelCallLog, Pipeline, RequestLog
from app.strategies.base import CallOutcome

_LEGACY_PIPELINES_DDL = """
CREATE TABLE pipelines (
    id INTEGER NOT NULL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    strategy VARCHAR(30) NOT NULL DEFAULT 'council',
    judge_model_id INTEGER NOT NULL,
    judge_prompt_template TEXT NOT NULL DEFAULT '',
    member_timeout_seconds INTEGER NOT NULL DEFAULT 120,
    fault_tolerance JSON NOT NULL DEFAULT '{}',
    max_concurrency INTEGER NOT NULL DEFAULT 10,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME,
    updated_at DATETIME
)
"""

_LEGACY_CALL_LOGS_DDL = """
CREATE TABLE model_call_logs (
    id INTEGER NOT NULL PRIMARY KEY,
    request_id INTEGER NOT NULL,
    role VARCHAR(20) NOT NULL,
    model_id INTEGER NOT NULL DEFAULT 0,
    upstream_model_id VARCHAR(200) NOT NULL DEFAULT '',
    provider_name VARCHAR(100) NOT NULL DEFAULT '',
    request_payload JSON NOT NULL DEFAULT '{}',
    response_content TEXT NOT NULL DEFAULT '',
    status VARCHAR(30) NOT NULL DEFAULT 'success',
    error_message TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME
)
"""


async def _table_columns(conn: AsyncConnection, table: str) -> set[str]:
    rows = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
    return {row[1] for row in rows.fetchall()}


async def _build_legacy_engine() -> AsyncEngine:
    """手工建只含旧列的 pipelines/model_call_logs（无 strategy_params / round）并插入旧行。"""
    engine = create_db_engine(":memory:")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_LEGACY_PIPELINES_DDL)
        await conn.exec_driver_sql(_LEGACY_CALL_LOGS_DDL)
        await conn.exec_driver_sql(
            "INSERT INTO pipelines (name, strategy, judge_model_id) VALUES ('legacy', 'council', 1)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO model_call_logs (request_id, role) VALUES (1, 'member')"
        )
    return engine


async def test_ensure_schema_patches_legacy_db() -> None:
    """UT-32-2 旧库补列：init_db 后新列存在；旧行读回 strategy_params={}、round=None；幂等。"""
    engine = await _build_legacy_engine()
    async with engine.begin() as conn:
        assert "strategy_params" not in await _table_columns(conn, "pipelines")
        assert "round" not in await _table_columns(conn, "model_call_logs")

    await init_db(engine)
    await init_db(engine)  # 二次执行幂等

    async with engine.begin() as conn:
        assert "strategy_params" in await _table_columns(conn, "pipelines")
        assert "round" in await _table_columns(conn, "model_call_logs")

    factory = create_session_factory(engine)
    async with factory() as s:
        pipeline = await s.get(Pipeline, 1)
        assert pipeline is not None
        assert pipeline.strategy_params == {}
        call = (await s.execute(ModelCallLog.__table__.select())).mappings().one()
        assert call["round"] is None
    await engine.dispose()


async def test_ensure_schema_patches_models_upstream_missing() -> None:
    """UT-40-1 models 补列 upstream_missing：旧行读回 False；幂等。"""
    engine = create_db_engine(":memory:")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            """
CREATE TABLE providers (
    id INTEGER NOT NULL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    protocol VARCHAR(30) NOT NULL,
    base_url VARCHAR(500) NOT NULL,
    api_key_encrypted TEXT,
    remark VARCHAR(500),
    enabled BOOLEAN,
    created_at DATETIME,
    updated_at DATETIME
)
"""
        )
        await conn.exec_driver_sql(
            """
CREATE TABLE models (
    id INTEGER NOT NULL PRIMARY KEY,
    provider_id INTEGER NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    upstream_model_id VARCHAR(200) NOT NULL,
    default_params JSON NOT NULL DEFAULT '{}',
    enabled BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME,
    updated_at DATETIME
)
"""
        )
        await conn.exec_driver_sql(
            "INSERT INTO providers (id, name, protocol, base_url) VALUES (1, 'p', 'openai_compatible', 'http://x')"
        )
        await conn.exec_driver_sql(
            "INSERT INTO models (id, provider_id, display_name, upstream_model_id) VALUES (1, 1, 'm', 'm-up')"
        )
        assert "upstream_missing" not in await _table_columns(conn, "models")

    await init_db(engine)
    await init_db(engine)  # 二次执行幂等

    async with engine.begin() as conn:
        assert "upstream_missing" in await _table_columns(conn, "models")

    factory = create_session_factory(engine)
    async with factory() as s:
        model = await s.get(LlmModel, 1)
        assert model is not None
        assert model.upstream_missing is False
    await engine.dispose()


async def test_ensure_schema_patches_model_call_logs_cache_columns() -> None:
    """UT-41-1 model_call_logs 补列 cached_tokens/cache_write_tokens：旧行读回 0；幂等。"""
    engine = await _build_legacy_engine()  # 旧库 model_call_logs 无缓存两列
    async with engine.begin() as conn:
        assert "cached_tokens" not in await _table_columns(conn, "model_call_logs")
        assert "cache_write_tokens" not in await _table_columns(conn, "model_call_logs")

    await init_db(engine)
    await init_db(engine)  # 二次执行幂等

    async with engine.begin() as conn:
        assert "cached_tokens" in await _table_columns(conn, "model_call_logs")
        assert "cache_write_tokens" in await _table_columns(conn, "model_call_logs")

    factory = create_session_factory(engine)
    async with factory() as s:
        call = (await s.execute(ModelCallLog.__table__.select())).mappings().one()
        assert call["cached_tokens"] == 0
        assert call["cache_write_tokens"] == 0
    await engine.dispose()


async def test_record_call_round_no() -> None:
    """UT-32-3 round 落库：record_call 带/不带 round_no 均正确往返（经 CallOutcome 全链）。"""
    engine = create_db_engine(":memory:")
    await init_db(engine)
    factory = create_session_factory(engine)
    async with factory() as s:
        req = await start_request(
            s,
            client_model_field="ice-v1",
            pipeline_name="ice-v1",
            messages=[{"role": "user", "content": "hi"}],
            params={},
        )
        base = {
            "role": "member",
            "model_id": 1,
            "upstream_model_id": "m-a",
            "provider_name": "p",
            "request_payload": {"model": "m-a"},
        }
        outcome_with = CallOutcome(**base, round_no=2)
        outcome_without = CallOutcome(**base)
        with_id = (await record_call(s, request_id=req.id, **outcome_with.to_log_kwargs())).id
        without_id = (await record_call(s, request_id=req.id, **outcome_without.to_log_kwargs())).id
        await s.commit()
    async with factory() as s:
        assert (await s.get(ModelCallLog, with_id)).round == 2
        assert (await s.get(ModelCallLog, without_id)).round is None
    await engine.dispose()


_LEGACY_REQUEST_LOGS_DDL = """
CREATE TABLE request_logs (
    id INTEGER NOT NULL PRIMARY KEY,
    pipeline_name VARCHAR(100) NOT NULL DEFAULT '',
    client_model_field VARCHAR(100) NOT NULL,
    request_messages JSON NOT NULL DEFAULT '[]',
    request_params JSON NOT NULL DEFAULT '{}',
    response_content TEXT NOT NULL DEFAULT '',
    response_finish_reason VARCHAR(30) NOT NULL DEFAULT '',
    status VARCHAR(30) NOT NULL DEFAULT 'success',
    total_duration_ms INTEGER NOT NULL DEFAULT 0,
    total_prompt_tokens INTEGER NOT NULL DEFAULT 0,
    total_completion_tokens INTEGER NOT NULL DEFAULT 0,
    client_ip VARCHAR(64) NOT NULL DEFAULT '',
    created_at DATETIME
)
"""


async def test_ensure_schema_patches_request_logs_first_token_ms() -> None:
    """UT-32-4 request_logs 补列 first_token_ms：旧行读回 0；finish_request 可写入。"""
    engine = create_db_engine(":memory:")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_LEGACY_REQUEST_LOGS_DDL)
        await conn.exec_driver_sql(
            "INSERT INTO request_logs (client_model_field) VALUES ('legacy-model')"
        )
        assert "first_token_ms" not in await _table_columns(conn, "request_logs")

    await init_db(engine)
    await init_db(engine)  # 二次执行幂等

    async with engine.begin() as conn:
        assert "first_token_ms" in await _table_columns(conn, "request_logs")

    factory = create_session_factory(engine)
    async with factory() as s:
        row = await s.get(RequestLog, 1)
        assert row is not None
        assert row.first_token_ms == 0
        await finish_request(s, row.id, status="success", total_duration_ms=120, first_token_ms=45)
        await s.commit()
    async with factory() as s:
        row = await s.get(RequestLog, 1)
        assert row is not None
        assert row.first_token_ms == 45
    await engine.dispose()


async def test_ensure_schema_patches_pipelines_stream_process() -> None:
    """UT-32-5 pipelines 补列 stream_process：旧行读回 False；幂等。"""
    engine = await _build_legacy_engine()  # 旧库 pipelines 无 strategy_params / stream_process
    async with engine.begin() as conn:
        assert "stream_process" not in await _table_columns(conn, "pipelines")

    await init_db(engine)
    await init_db(engine)

    async with engine.begin() as conn:
        assert "stream_process" in await _table_columns(conn, "pipelines")
    factory = create_session_factory(engine)
    async with factory() as s:
        pipeline = await s.get(Pipeline, 1)
        assert pipeline is not None
        assert pipeline.stream_process is False
    await engine.dispose()
