"""数据面授权（多用户 T49）：三类授权项取并集，默认拒绝（需求 FR-19/D18/D32）。

AccessScope 一次装载（单查询），列表过滤与调用判定共用；管理员 unrestricted 零查询短路，
保证其响应与升级前逐字节一致（AC-MU-6）。运行期以启用状态为准：授权不绕过停用/下线。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.orm import Provider, UserGrant


@dataclass(frozen=True)
class AccessScope:
    """一个调用者的授权集合：管理员 unrestricted=True（直通）；一般用户按三类 id 集合判定。"""

    unrestricted: bool
    pipeline_ids: frozenset[int] = field(default=frozenset())
    model_ids: frozenset[int] = field(default=frozenset())
    provider_ids: frozenset[int] = field(default=frozenset())

    def allows_pipeline(self, pipeline_id: int) -> bool:
        return self.unrestricted or pipeline_id in self.pipeline_ids

    def allows_model_row(self, model_row: Any) -> bool:
        """库内模型行：模型级授权或其供应商级授权命中（上游动态模型走 allows_dynamic）。"""
        if self.unrestricted:
            return True
        return model_row.id in self.model_ids or model_row.provider_id in self.provider_ids

    def allows_dynamic(self, provider_id: int) -> bool:
        """库内无记录的上游动态模型：仅供应商级授权覆盖（需求 FR-19）。"""
        return self.unrestricted or provider_id in self.provider_ids


async def load_scope(session: AsyncSession, caller: Any) -> AccessScope:
    """caller → 授权集合。管理员零查询（D32）；一般用户一次取全部授权行。"""
    if caller.role == "admin":
        return AccessScope(unrestricted=True)
    rows = (
        (await session.execute(select(UserGrant).where(UserGrant.user_id == caller.user_id)))
        .scalars()
        .all()
    )
    return AccessScope(
        unrestricted=False,
        pipeline_ids=frozenset(r.target_id for r in rows if r.kind == "pipeline"),
        model_ids=frozenset(r.target_id for r in rows if r.kind == "model"),
        provider_ids=frozenset(r.target_id for r in rows if r.kind == "provider"),
    )


def filter_pipelines(scope: AccessScope, pipelines: list[Any]) -> list[Any]:
    """按授权过滤 Pipeline 列表；unrestricted 直通原列表对象（不重建、不重排序）。"""
    if scope.unrestricted:
        return pipelines
    return [p for p in pipelines if p.id in scope.pipeline_ids]


def filter_models(scope: AccessScope, models: list[Any]) -> list[Any]:
    if scope.unrestricted:
        return models
    return [m for m in models if m.id in scope.model_ids or m.provider_id in scope.provider_ids]


def visible_provider_ids(
    scope: AccessScope, providers: list[Provider], models: list[Any]
) -> set[int]:
    """/v1/route 列表口径：有供应商级授权，或该供应商下存在被授权的模型。"""
    if scope.unrestricted:
        return {p.id for p in providers}
    by_provider: dict[int, bool] = {}
    for p in providers:
        by_provider.setdefault(p.id, p.id in scope.provider_ids)
    for m in models:
        if m.id in scope.model_ids:
            by_provider[m.provider_id] = True
    return {pid for pid, visible in by_provider.items() if visible}


async def cleanup_grants_for(session: AsyncSession, kind: str, target_ids: list[int]) -> None:
    """被授权对象删除时清理对应授权行（D33，由删除路径调用）。"""
    from app.repos import delete_grants_for_targets

    await delete_grants_for_targets(session, kind, target_ids)
