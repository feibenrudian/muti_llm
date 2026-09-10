"""数据库：async engine（SQLite WAL + 外键强制）、session 工厂、建表。"""

import json
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.orm import Base

# JSON 落库保留 UTF-8 中文（Trace keyword 的 LIKE 过滤依赖这一点）
_JSON_SERIALIZER = lambda value: json.dumps(value, ensure_ascii=False)  # noqa: E731


def create_db_engine(database_path: str) -> AsyncEngine:
    """SQLite async engine；:memory: 时用 StaticPool 保持单连接。"""
    if database_path == ":memory:":
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            json_serializer=_JSON_SERIALIZER,
        )
    else:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path}",
            json_serializer=_JSON_SERIALIZER,
        )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# 轻量补列（ICE D14）：create_all 不会给已存表加列。SQLite ADD COLUMN 的默认值必须是常量。
_SCHEMA_PATCHES: dict[str, dict[str, str]] = {
    "pipelines": {
        "strategy_params": "JSON NOT NULL DEFAULT '{}'",
        "stream_process": "INTEGER NOT NULL DEFAULT 0",
    },
    "models": {"upstream_missing": "INTEGER NOT NULL DEFAULT 0"},
    "model_call_logs": {"round": "INTEGER"},
    "request_logs": {"first_token_ms": "INTEGER NOT NULL DEFAULT 0"},
}


async def _ensure_schema(conn: AsyncConnection) -> None:
    """旧库补列：PRAGMA table_info 检测缺列 → ALTER TABLE ADD COLUMN（幂等）。"""
    for table, columns in _SCHEMA_PATCHES.items():
        rows = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        existing = {row[1] for row in rows.fetchall()}
        for name, ddl in columns.items():
            if name not in existing:
                await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


async def init_db(engine: AsyncEngine) -> None:
    """建表 + 旧库补列（幂等）；本地工具 v1 不引入 Alembic（tech-plan 决策 D2）。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_schema(conn)


async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """提供一个自动提交/回滚的 session（业务代码与脚本用）。"""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
