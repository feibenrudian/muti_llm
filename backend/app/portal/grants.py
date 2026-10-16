"""门户授权只读视图（T52）：当前用户可调用的流水线 / 模型 / 供应商（名称随 id 返回）。

授权本身仍由管理台维护（PUT /api/admin/users/{id}/grants）；门户只读，
供 Keys 页"可调用范围"展示，用户据此理解 model_not_authorized 的原因。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_svc import Caller
from app.deps import get_caller, get_session
from app.orm import LlmModel, Pipeline, Provider
from app.repos import list_user_grants

router = APIRouter(prefix="/grants", tags=["portal-grants"])


async def _names(
    session: AsyncSession, id_col: Any, name_col: Any, ids: list[int]
) -> dict[int, str]:
    if not ids:
        return {}
    rows = (await session.execute(select(id_col, name_col).where(id_col.in_(ids)))).all()
    return {int(row[0]): str(row[1]) for row in rows}


def _items(ids: list[int], names: dict[int, str]) -> list[dict[str, Any]]:
    # 授权行随目标删除级联清理（D33），名称缺失属瞬态；兜底显示 #id
    return [{"id": i, "name": names.get(i, f"#{i}")} for i in sorted(ids)]


@router.get("")
async def own_grants(
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
) -> dict[str, list[dict[str, Any]]]:
    rows = await list_user_grants(session, caller.user_id)
    ids = {
        "pipelines": sorted(r.target_id for r in rows if r.kind == "pipeline"),
        "models": sorted(r.target_id for r in rows if r.kind == "model"),
        "providers": sorted(r.target_id for r in rows if r.kind == "provider"),
    }
    return {
        "pipelines": _items(
            ids["pipelines"],
            await _names(session, Pipeline.id, Pipeline.name, ids["pipelines"]),
        ),
        "models": _items(
            ids["models"],
            await _names(session, LlmModel.id, LlmModel.display_name, ids["models"]),
        ),
        "providers": _items(
            ids["providers"],
            await _names(session, Provider.id, Provider.name, ids["providers"]),
        ),
    }
