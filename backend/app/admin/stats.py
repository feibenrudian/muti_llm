"""用量统计 API（T41 对账看板）：按供应商/模型/天聚合 model_call_logs 的 token 用量。

数据源为调用明细表（provider_name/upstream_model_id 冗余存储）：模型/供应商删除后
历史统计仍完整。缓存命中 = cached_tokens（读缓存），未命中 = prompt - cached - cache_write；
prompt_tokens 恒为计费输入总量（含命中/写入，归一化语义见 adapters.base.LlmUsage）。
时间参数语义同 admin/traces.py：end 为日期时含当天全天。命中率由前端由 tokens 计算。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session
from app.orm import LlmModel, ModelCallLog

router = APIRouter(prefix="/stats", tags=["admin-stats"])


def _aggregate_columns() -> tuple[Any, ...]:
    """聚合指标列：调用数、失败数、输入/输出/缓存命中/缓存写入 tokens。"""
    return (
        func.count(ModelCallLog.id).label("calls"),
        func.sum(case((ModelCallLog.status != "success", 1), else_=0)).label("failed_calls"),
        func.coalesce(func.sum(ModelCallLog.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(ModelCallLog.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(ModelCallLog.cached_tokens), 0).label("cached_tokens"),
        func.coalesce(func.sum(ModelCallLog.cache_write_tokens), 0).label("cache_write_tokens"),
    )


def _metrics_row(row: Any) -> dict[str, int]:
    return {
        "calls": int(row.calls),
        "failed_calls": int(row.failed_calls or 0),
        "prompt_tokens": int(row.prompt_tokens),
        "completion_tokens": int(row.completion_tokens),
        "cached_tokens": int(row.cached_tokens),
        "cache_write_tokens": int(row.cache_write_tokens),
        "total_tokens": int(row.prompt_tokens) + int(row.completion_tokens),
    }


def _stats_filters(
    start: str | None = None,
    end: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> list[Any]:
    conditions: list[Any] = []
    if start:
        conditions.append(
            ModelCallLog.created_at >= datetime.fromisoformat(start).astimezone(UTC)
        )
    if end:
        dt = datetime.fromisoformat(end)
        if len(end) <= 10:
            dt += timedelta(days=1)
        conditions.append(ModelCallLog.created_at < dt.astimezone(UTC))
    if provider:
        conditions.append(ModelCallLog.provider_name == provider)
    if model:
        conditions.append(ModelCallLog.upstream_model_id == model)
    return conditions


@router.get("/overview")
async def stats_overview(
    session: AsyncSession = Depends(get_session),
    start: str | None = None,
    end: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, int]:
    query = select(*_aggregate_columns())
    for condition in _stats_filters(start, end, provider, model):
        query = query.where(condition)
    return _metrics_row((await session.execute(query)).one())


@router.get("/daily")
async def stats_daily(
    session: AsyncSession = Depends(get_session),
    start: str | None = None,
    end: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """按 UTC 天分组的时间序列（库内 created_at 为 UTC，与 Trace 列表一致）。"""
    day = func.date(ModelCallLog.created_at).label("date")
    query = select(day, *_aggregate_columns()).group_by(day).order_by(day)
    for condition in _stats_filters(start, end, provider, model):
        query = query.where(condition)
    return [{**_metrics_row(row), "date": row.date} for row in (await session.execute(query)).all()]


@router.get("/by-provider")
async def stats_by_provider(
    session: AsyncSession = Depends(get_session),
    start: str | None = None,
    end: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    query = select(ModelCallLog.provider_name.label("provider_name"), *_aggregate_columns())
    query = query.group_by(ModelCallLog.provider_name)
    for condition in _stats_filters(start, end, None, model):
        query = query.where(condition)
    rows = [
        _metrics_row(row) | {"provider_name": row.provider_name}
        for row in (await session.execute(query)).all()
    ]
    rows.sort(key=lambda r: (-r["total_tokens"], r["provider_name"]))
    return rows


@router.get("/by-model")
async def stats_by_model(
    session: AsyncSession = Depends(get_session),
    start: str | None = None,
    end: str | None = None,
    provider: str | None = None,
) -> list[dict[str, Any]]:
    """按（供应商, 模型）分组；display_name 左联 models，模型已删除则为 null。"""
    query = select(
        ModelCallLog.provider_name.label("provider_name"),
        ModelCallLog.model_id.label("model_id"),
        ModelCallLog.upstream_model_id.label("upstream_model_id"),
        LlmModel.display_name.label("display_name"),
        *_aggregate_columns(),
    ).outerjoin(LlmModel, LlmModel.id == ModelCallLog.model_id)
    query = query.group_by(
        ModelCallLog.provider_name,
        ModelCallLog.model_id,
        ModelCallLog.upstream_model_id,
        LlmModel.display_name,
    )
    for condition in _stats_filters(start, end, provider, None):
        query = query.where(condition)
    rows = [
        _metrics_row(row)
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
