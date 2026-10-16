"""门户用量统计（T52）：当前登录用户自己的 token 用量（总览 / 按天 / 按模型）。

聚合口径与 /api/admin/stats 完全一致（app/usage_stats.py），差异仅在归属：
强制 user_id = caller.user_id（user_id=0 的历史数据天然排除），不提供 user 筛选参数。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_svc import Caller
from app.deps import get_caller, get_session
from app.orm import ModelCallLog
from app.usage_stats import by_model, daily, filter_conditions, overview

router = APIRouter(prefix="/stats", tags=["portal-stats"])


def _own_conditions(
    caller: Caller,
    start: str | None,
    end: str | None,
    provider: str | None = None,
    model: str | None = None,
) -> list[Any]:
    """归属强制为当前用户；时间/维度筛选语义同 admin（end 为日期时含当天全天）。"""
    return [
        ModelCallLog.user_id == caller.user_id,
        *filter_conditions(start, end, provider, model),
    ]


@router.get("/overview")
async def portal_stats_overview(
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
    start: str | None = None,
    end: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, int]:
    return await overview(session, _own_conditions(caller, start, end, provider, model))


@router.get("/daily")
async def portal_stats_daily(
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
    start: str | None = None,
    end: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """按 UTC 天分组的时间序列（语义同 admin /stats/daily）。"""
    return await daily(session, _own_conditions(caller, start, end, provider, model))


@router.get("/by-model")
async def portal_stats_by_model(
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
    start: str | None = None,
    end: str | None = None,
    provider: str | None = None,
) -> list[dict[str, Any]]:
    """按（供应商, 模型）分组：回答"什么模型、什么时间段、用量多少"。"""
    return await by_model(session, _own_conditions(caller, start, end, provider))
