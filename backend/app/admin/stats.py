"""用量统计 API（T41 对账看板）：按供应商/模型/天聚合 model_call_logs 的 token 用量。

聚合查询原语在 app/usage_stats.py（T52 抽取，与门户 /api/portal/stats 共用口径）；
本文件只做管理端路由壳。/by-user、/by-user-model 为多用户 T50 增量。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session
from app.usage_stats import (
    by_model,
    by_provider,
    by_user,
    by_user_model,
    daily,
    filter_conditions,
    overview,
)

router = APIRouter(prefix="/stats", tags=["admin-stats"])


@router.get("/overview")
async def stats_overview(
    session: AsyncSession = Depends(get_session),
    start: str | None = None,
    end: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    user: str | None = None,
) -> dict[str, int]:
    return await overview(session, filter_conditions(start, end, provider, model, user))


@router.get("/daily")
async def stats_daily(
    session: AsyncSession = Depends(get_session),
    start: str | None = None,
    end: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    user: str | None = None,
) -> list[dict[str, Any]]:
    """按 UTC 天分组的时间序列（库内 created_at 为 UTC，与 Trace 列表一致）。"""
    return await daily(session, filter_conditions(start, end, provider, model, user))


@router.get("/by-provider")
async def stats_by_provider(
    session: AsyncSession = Depends(get_session),
    start: str | None = None,
    end: str | None = None,
    model: str | None = None,
    user: str | None = None,
) -> list[dict[str, Any]]:
    return await by_provider(session, filter_conditions(start, end, None, model, user))


@router.get("/by-model")
async def stats_by_model(
    session: AsyncSession = Depends(get_session),
    start: str | None = None,
    end: str | None = None,
    provider: str | None = None,
    user: str | None = None,
) -> list[dict[str, Any]]:
    """按（供应商, 模型）分组；display_name 左联 models，模型已删除则为 null。"""
    return await by_model(session, filter_conditions(start, end, provider, None, user))


@router.get("/by-user")
async def stats_by_user(
    session: AsyncSession = Depends(get_session),
    start: str | None = None,
    end: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """按用户分组的用量（user_id=0 为多用户之前的历史数据，前端展示"（历史）"）。"""
    return await by_user(session, filter_conditions(start, end, provider, model, None))


@router.get("/by-user-model")
async def stats_by_user_model(
    session: AsyncSession = Depends(get_session),
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    """按（用户, 供应商, 模型）分组：回答"哪个用户、什么时间段、哪个模型、用了多少"。"""
    return await by_user_model(session, filter_conditions(start, end))
