"""UT-32-2..3：旧库补列（_ensure_schema 幂等）与 round 落库往返。"""

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db import create_db_engine, create_session_factory, init_db
from app.logging_svc import record_call, start_request
from app.orm import ModelCallLog, Pipeline
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
