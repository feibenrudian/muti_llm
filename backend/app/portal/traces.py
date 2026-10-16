"""门户调用记录（T52）：当前用户自己的 Trace 列表与只读详情。

筛选/分页/时间窗语义同 /api/admin/traces（row_to_dict 与 parse_* 复用 logging_svc），
差异仅在归属：强制 user_id = caller.user_id；他人或不存在的 trace 一律 404（防枚举）。
不提供清空与重跑（管理台专属）。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_svc import Caller
from app.deps import get_caller, get_session
from app.logging_svc import parse_end_exclusive, parse_start, row_to_dict
from app.orm import ModelCallLog, RequestLog
from app.repos import list_model_calls

router = APIRouter(prefix="/traces", tags=["portal-traces"])


@router.get("")
async def list_own_traces(
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
    start: str | None = None,
    end: str | None = None,
    pipeline: str | None = None,
    model: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    conditions: list[Any] = [RequestLog.user_id == caller.user_id]
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
        conditions.append(RequestLog.created_at >= parse_start(start))
    if end:
        conditions.append(RequestLog.created_at < parse_end_exclusive(end))
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
async def get_own_trace(
    trace_id: int,
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
) -> dict[str, Any]:
    row = await session.get(RequestLog, trace_id)
    if row is None or row.user_id != caller.user_id:
        raise HTTPException(status_code=404, detail="trace not found")
    calls = await list_model_calls(session, trace_id)
    detail = row_to_dict(row)
    if isinstance(detail.get("request_messages"), str):
        detail["request_messages"] = json.loads(detail["request_messages"])
    return {
        "request": detail,
        "calls": [row_to_dict(call) for call in calls],
    }
