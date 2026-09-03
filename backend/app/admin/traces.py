"""Trace 查询 API：列表（筛选/分页/关键字）+ 详情（完整时间线）+ 手动清空。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session
from app.orm import ModelCallLog, RequestLog
from app.repos import list_model_calls

router = APIRouter(prefix="/traces", tags=["admin-traces"])


def row_to_dict(row: Any) -> dict[str, Any]:
    data = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    for key, value in data.items():
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


def _parse_start(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _parse_end_exclusive(value: str) -> datetime:
    """end 视为日期时含当天全天（次日 0 点为界）。"""
    dt = datetime.fromisoformat(value)
    if len(value) <= 10:
        dt += timedelta(days=1)
    return dt.astimezone(UTC)


@router.get("")
async def list_traces(
    session: AsyncSession = Depends(get_session),
    start: str | None = None,
    end: str | None = None,
    pipeline: str | None = None,
    model: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    conditions = []
    if pipeline:
        conditions.append(
            or_(
                RequestLog.pipeline_name == pipeline,
                RequestLog.client_model_field == pipeline,
            )
        )
    if status:
        conditions.append(RequestLog.status == status)
    if start:
        conditions.append(RequestLog.created_at >= _parse_start(start))
    if end:
        conditions.append(RequestLog.created_at < _parse_end_exclusive(end))
    if keyword:
        conditions.append(RequestLog.request_messages.like(f"%{keyword}%"))
    if model:
        conditions.append(
            RequestLog.id.in_(
                select(ModelCallLog.request_id).where(ModelCallLog.upstream_model_id == model)
            )
        )

    base = select(RequestLog)
    count_query = select(func.count()).select_from(RequestLog)
    for condition in conditions:
        base = base.where(condition)
        count_query = count_query.where(condition)

    total = (await session.execute(count_query)).scalar_one()
    rows = (
        (
            await session.execute(
                base.order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    items = []
    for row in rows:
        item = row_to_dict(row)
        item["content_preview"] = (row.response_content or "")[:120]
        item.pop("request_messages", None)
        item.pop("response_content", None)
        items.append(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/{trace_id}")
async def get_trace(trace_id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await session.get(RequestLog, trace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="trace not found")
    calls = await list_model_calls(session, trace_id)
    detail = row_to_dict(row)
    if isinstance(detail.get("request_messages"), str):
        detail["request_messages"] = json.loads(detail["request_messages"])
    return {
        "request": detail,
        "calls": [row_to_dict(call) for call in calls],
    }


@router.post("/clear")
async def clear_traces(session: AsyncSession = Depends(get_session)) -> dict[str, bool]:
    await session.execute(delete(ModelCallLog))
    await session.execute(delete(RequestLog))
    await session.commit()
    return {"cleared": True}
