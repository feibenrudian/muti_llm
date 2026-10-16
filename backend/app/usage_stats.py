"""用量聚合查询（T41/T50/T52）：model_call_logs 的 token 对账统计，admin 与 portal 共用。

1.2 未规划该文件位置，属用量统计域（T52 自 admin/stats.py 抽取查询原语）：
admin 端点按筛选参数构建 conditions，门户端点强制附加本人归属条件后复用同一口径。
数据源为调用明细表（provider_name/upstream_model_id 冗余存储）：模型/供应商删除后
历史统计仍完整。缓存命中 = cached_tokens（读缓存），未命中 = prompt - cached - cache_write；
prompt_tokens 恒为计费输入总量（含命中/写入，归一化语义见 adapters.base.LlmUsage）。
时间参数语义同 admin/traces.py：end 为日期时含当天全天。命中率由前端由 tokens 计算。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.orm import LlmModel, ModelCallLog


def aggregate_columns() -> tuple[Any, ...]:
    """聚合指标列：调用数、失败数、输入/输出/缓存命中/缓存写入 tokens。"""
    return (
        func.count(ModelCallLog.id).label("calls"),
        func.sum(case((ModelCallLog.status != "success", 1), else_=0)).label("failed_calls"),
        func.coalesce(func.sum(ModelCallLog.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(ModelCallLog.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(ModelCallLog.cached_tokens), 0).label("cached_tokens"),
        func.coalesce(func.sum(ModelCallLog.cache_write_tokens), 0).label("cache_write_tokens"),
    )


def metrics_row(row: Any) -> dict[str, int]:
    return {
        "calls": int(row.calls),
        "failed_calls": int(row.failed_calls or 0),
        "prompt_tokens": int(row.prompt_tokens),
        "completion_tokens": int(row.completion_tokens),
        "cached_tokens": int(row.cached_tokens),
        "cache_write_tokens": int(row.cache_write_tokens),
        "total_tokens": int(row.prompt_tokens) + int(row.completion_tokens),
    }


def filter_conditions(
    start: str | None = None,
    end: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    user: str | None = None,
) -> list[Any]:
    """管理端筛选参数 → WHERE 条件；门户端不传 user，归属条件由调用方强制附加。"""
    conditions: list[Any] = []
    if start:
        conditions.append(ModelCallLog.created_at >= datetime.fromisoformat(start).astimezone(UTC))
    if end:
        dt = datetime.fromisoformat(end)
        if len(end) <= 10:
            dt += timedelta(days=1)
        conditions.append(ModelCallLog.created_at < dt.astimezone(UTC))
    if provider:
        conditions.append(ModelCallLog.provider_name == provider)
    if model:
        conditions.append(ModelCallLog.upstream_model_id == model)
    if user:
        # 数字视为 user_id、非数字视为用户名，取并集（纯数字用户名歧义见方案 §10 风险 6）
        conditions.append(
            or_(
                ModelCallLog.user_id == int(user) if user.isdigit() else false(),
                ModelCallLog.user_username == user,
            )
        )
    return conditions


async def overview(session: AsyncSession, conditions: list[Any]) -> dict[str, int]:
    query = select(*aggregate_columns())
    for condition in conditions:
        query = query.where(condition)
    return metrics_row((await session.execute(query)).one())


async def daily(session: AsyncSession, conditions: list[Any]) -> list[dict[str, Any]]:
    """按 UTC 天分组的时间序列（库内 created_at 为 UTC，与 Trace 列表一致）。"""
    day = func.date(ModelCallLog.created_at).label("date")
    query = select(day, *aggregate_columns()).group_by(day).order_by(day)
    for condition in conditions:
        query = query.where(condition)
    return [{**metrics_row(row), "date": row.date} for row in (await session.execute(query)).all()]


async def by_provider(session: AsyncSession, conditions: list[Any]) -> list[dict[str, Any]]:
    query = select(ModelCallLog.provider_name.label("provider_name"), *aggregate_columns())
    query = query.group_by(ModelCallLog.provider_name)
    for condition in conditions:
        query = query.where(condition)
    rows = [
        metrics_row(row) | {"provider_name": row.provider_name}
        for row in (await session.execute(query)).all()
    ]
    rows.sort(key=lambda r: (-r["total_tokens"], r["provider_name"]))
    return rows


async def by_model(session: AsyncSession, conditions: list[Any]) -> list[dict[str, Any]]:
    """按（供应商, 模型）分组；display_name 左联 models，模型已删除则为 null。"""
    query = select(
        ModelCallLog.provider_name.label("provider_name"),
        ModelCallLog.model_id.label("model_id"),
        ModelCallLog.upstream_model_id.label("upstream_model_id"),
        LlmModel.display_name.label("display_name"),
        *aggregate_columns(),
    ).outerjoin(LlmModel, LlmModel.id == ModelCallLog.model_id)
    query = query.group_by(
        ModelCallLog.provider_name,
        ModelCallLog.model_id,
        ModelCallLog.upstream_model_id,
        LlmModel.display_name,
    )
    for condition in conditions:
        query = query.where(condition)
    rows = [
        metrics_row(row)
        | {
            "provider_name": row.provider_name,
            "model_id": row.model_id,
            "upstream_model_id": row.upstream_model_id,
            "display_name": row.display_name,
        }
        for row in (await session.execute(query)).all()
    ]
    rows.sort(key=lambda r: (-r["total_tokens"], r["provider_name"], r["upstream_model_id"]))
    return rows


async def by_user(session: AsyncSession, conditions: list[Any]) -> list[dict[str, Any]]:
    """按用户分组的用量（user_id=0 为多用户之前的历史数据，前端展示"（历史）"）。"""
    query = select(
        ModelCallLog.user_id.label("user_id"),
        ModelCallLog.user_username.label("user_username"),
        *aggregate_columns(),
    ).group_by(ModelCallLog.user_id, ModelCallLog.user_username)
    for condition in conditions:
        query = query.where(condition)
    rows = [
        metrics_row(row) | {"user_id": row.user_id, "user_username": row.user_username}
        for row in (await session.execute(query)).all()
    ]
    rows.sort(key=lambda r: (-r["total_tokens"], r["user_username"]))
    return rows


async def by_user_model(session: AsyncSession, conditions: list[Any]) -> list[dict[str, Any]]:
    """按（用户, 供应商, 模型）分组：回答"哪个用户、什么时间段、哪个模型、用了多少"。"""
    query = select(
        ModelCallLog.user_id.label("user_id"),
        ModelCallLog.user_username.label("user_username"),
        ModelCallLog.provider_name.label("provider_name"),
        ModelCallLog.model_id.label("model_id"),
        ModelCallLog.upstream_model_id.label("upstream_model_id"),
        LlmModel.display_name.label("display_name"),
        *aggregate_columns(),
    ).outerjoin(LlmModel, LlmModel.id == ModelCallLog.model_id)
    query = query.group_by(
        ModelCallLog.user_id,
        ModelCallLog.user_username,
        ModelCallLog.provider_name,
        ModelCallLog.model_id,
        ModelCallLog.upstream_model_id,
        LlmModel.display_name,
    )
    for condition in conditions:
        query = query.where(condition)
    rows = [
        metrics_row(row)
        | {
            "user_id": row.user_id,
            "user_username": row.user_username,
            "provider_name": row.provider_name,
            "model_id": row.model_id,
            "upstream_model_id": row.upstream_model_id,
            "display_name": row.display_name,
        }
        for row in (await session.execute(query)).all()
    ]
    rows.sort(
        key=lambda r: (
            -r["total_tokens"],
            r["user_username"],
            r["provider_name"],
            r["upstream_model_id"],
        )
    )
    return rows
