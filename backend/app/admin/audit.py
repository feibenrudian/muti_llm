"""管理动作审计查询（T51）：/api/admin/audit 只读列表（动作/时间筛选 + 分页）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session
from app.orm import AdminAuditLog

router = APIRouter(tags=["admin-audit"])


@router.get("/audit")
async def list_audit(
    session: AsyncSession = Depends(get_session),
    action: str | None = None,
    start: str | None = None,
    end: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    query = select(AdminAuditLog)
    if action:
        query = query.where(AdminAuditLog.action == action)
    if start:
        query = query.where(
            AdminAuditLog.created_at >= datetime.fromisoformat(start).astimezone(UTC)
        )
    if end:
        dt = datetime.fromisoformat(end)
        if len(end) <= 10:
            dt += timedelta(days=1)
        query = query.where(AdminAuditLog.created_at < dt.astimezone(UTC))
    total = int(
        (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    )
    rows = (
        (
            await session.execute(
                query.order_by(AdminAuditLog.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "actor_user_id": r.actor_user_id,
                "actor_username": r.actor_username,
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "detail": r.detail,
                "created_at": r.created_at.astimezone(UTC).isoformat(),
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
