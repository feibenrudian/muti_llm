"""基础仓储：泛型 CRUD + 特殊查询。管理 API（T08+）与策略层统一经由此层访问 ORM。"""

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.orm import (
    AdminSession,
    AppSetting,
    LlmModel,
    ModelCallLog,
    Pipeline,
    PipelineMember,
    User,
    UserApiKey,
    UserGrant,
    utcnow,
)
from app.security import hash_token

_SLUG_CLEAN_RE = re.compile(r"-{2,}")


def derive_slug(name: str, taken: set[str], fallback: str) -> str:
    """供应商显示名 → 路由命名空间的 URL 标识：小写、非字母数字转连字符、去首尾、查重加序号。

    派生不出可用字符（如纯中文名）时用 fallback（provider-{id}），仍冲突则继续加序号。
    """
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    base = _SLUG_CLEAN_RE.sub("-", base)
    candidate = base or fallback
    slug = candidate
    serial = 2
    while slug in taken:
        slug = f"{candidate}-{serial}"
        serial += 1
    return slug


class Repository[ModelT]:
    def __init__(self, session: AsyncSession, model: type[ModelT]) -> None:
        self.session = session
        self.model = model

    async def create(self, **fields: Any) -> ModelT:
        obj = self.model(**fields)
        self.session.add(obj)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def get(self, id_: int) -> ModelT | None:
        return await self.session.get(self.model, id_)

    async def list(self) -> Sequence[ModelT]:
        result = await self.session.execute(select(self.model).order_by(self.model.id))  # type: ignore[attr-defined]
        return list(result.scalars())

    async def update(self, id_: int, **fields: Any) -> ModelT | None:
        obj = await self.get(id_)
        if obj is None:
            return None
        for key, value in fields.items():
            setattr(obj, key, value)
        await self.session.flush()
        await self.session.refresh(obj)
        return obj

    async def delete(self, id_: int) -> bool:
        obj = await self.get(id_)
        if obj is None:
            return False
        await self.session.delete(obj)
        await self.session.flush()
        return True


async def get_setting(session: AsyncSession, key: str) -> str | None:
    row = await session.get(AppSetting, key)
    return row.value if row is not None else None


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await session.flush()


async def find_enabled_pipeline_by_name(session: AsyncSession, name: str) -> Pipeline | None:
    result = await session.execute(
        select(Pipeline).where(Pipeline.name == name, Pipeline.enabled.is_(True))
    )
    return result.scalars().first()


async def find_pipeline_by_name(session: AsyncSession, name: str) -> Pipeline | None:
    """名称唯一性检查（含已停用的）。"""
    result = await session.execute(select(Pipeline).where(Pipeline.name == name))
    return result.scalars().first()


async def find_enabled_model_by_upstream_id(
    session: AsyncSession, upstream_model_id: str
) -> LlmModel | None:
    result = await session.execute(
        select(LlmModel).where(
            LlmModel.upstream_model_id == upstream_model_id, LlmModel.enabled.is_(True)
        )
    )
    return result.scalars().first()


async def delete_models_cascade(session: AsyncSession, model_ids: Sequence[int]) -> set[int]:
    """删除模型并级联清理引用，保持"存留的 Pipeline 均可运行"：
    引用这些模型的成员行直接删；裁判失效或成员因此被清空的 Pipeline 一并删除；
    上述引用全部清理后才删除模型行（外键强制开启，顺序不可颠倒）。
    返回被级联删除的 Pipeline id 集合。
    """
    if not model_ids:
        return set()
    affected = set(
        (
            await session.execute(
                select(PipelineMember.pipeline_id)
                .where(PipelineMember.model_id.in_(model_ids))
                .distinct()
            )
        ).scalars()
    )
    await session.execute(delete(PipelineMember).where(PipelineMember.model_id.in_(model_ids)))
    judge_dead = set(
        (
            await session.execute(select(Pipeline.id).where(Pipeline.judge_model_id.in_(model_ids)))
        ).scalars()
    )
    still_has_members = set(
        (await session.execute(select(PipelineMember.pipeline_id).distinct())).scalars()
    )
    broken = {pid for pid in affected if pid not in still_has_members} | judge_dead
    if broken:
        await session.execute(delete(PipelineMember).where(PipelineMember.pipeline_id.in_(broken)))
        await session.execute(delete(Pipeline).where(Pipeline.id.in_(broken)))
        # 级联删除的 Pipeline 的授权行同步清理（多用户 D33）
        await delete_grants_for_targets(session, "pipeline", sorted(broken))
    await delete_grants_for_targets(session, "model", list(model_ids))
    await session.execute(delete(LlmModel).where(LlmModel.id.in_(model_ids)))
    return broken


async def list_model_calls(session: AsyncSession, request_id: int) -> list[ModelCallLog]:
    """某次请求的全部上游调用明细，按发生顺序（created_at, id）。"""
    result = await session.execute(
        select(ModelCallLog)
        .where(ModelCallLog.request_id == request_id)
        .order_by(ModelCallLog.created_at, ModelCallLog.id)
    )
    return list(result.scalars())


# ---- 多用户域（T45）----------------------------------------------------------------


async def find_user_by_username(session: AsyncSession, username: str) -> User | None:
    result = await session.execute(select(User).where(User.username == username))
    return result.scalars().first()


async def find_user_key_by_hash(session: AsyncSession, key_hash: str) -> UserApiKey | None:
    result = await session.execute(select(UserApiKey).where(UserApiKey.key_hash == key_hash))
    return result.scalars().first()


async def count_enabled_admins(session: AsyncSession, exclude_user_id: int = 0) -> int:
    """启用状态的管理员数（可排除指定用户）：防自锁校验用（FR-17）。"""
    query = (
        select(func.count()).select_from(User).where(User.role == "admin", User.enabled.is_(True))
    )
    if exclude_user_id:
        query = query.where(User.id != exclude_user_id)
    return int((await session.execute(query)).scalar_one())


async def delete_user_cascade(session: AsyncSession, user_id: int) -> None:
    """删除用户及其 Key / 授权 / 会话；日志表不删（冗余用户名快照保留，需求 D22）。"""
    await session.execute(delete(UserApiKey).where(UserApiKey.user_id == user_id))
    await session.execute(delete(UserGrant).where(UserGrant.user_id == user_id))
    await session.execute(delete(AdminSession).where(AdminSession.user_id == user_id))
    await session.execute(delete(User).where(User.id == user_id))
    await session.flush()


async def delete_grants_for_targets(
    session: AsyncSession, kind: str, target_ids: Sequence[int]
) -> None:
    """被授权对象（模型/供应商/Pipeline）删除时清理对应授权行（D33）。"""
    if not target_ids:
        return
    await session.execute(
        delete(UserGrant).where(UserGrant.kind == kind, UserGrant.target_id.in_(target_ids))
    )


async def list_user_grants(session: AsyncSession, user_id: int) -> list[UserGrant]:
    result = await session.execute(select(UserGrant).where(UserGrant.user_id == user_id))
    return list(result.scalars())


async def replace_user_grants(
    session: AsyncSession, user_id: int, grants: dict[str, set[int]]
) -> None:
    """覆盖式重设授权（FR-19）：删除该用户全部授权行后按 {kind: target_ids} 重建。"""
    await session.execute(delete(UserGrant).where(UserGrant.user_id == user_id))
    for kind, target_ids in grants.items():
        for target_id in sorted(target_ids):
            session.add(UserGrant(user_id=user_id, kind=kind, target_id=target_id))
    await session.flush()


async def revoke_user_sessions(
    session: AsyncSession, user_id: int, *, keep_token: str = ""
) -> None:
    """吊销用户全部会话（改密/停用/降级后调用）；keep_token 传当前会话明文则保留之。"""
    keep_hash = hash_token(keep_token) if keep_token else ""
    query = delete(AdminSession).where(AdminSession.user_id == user_id)
    if keep_hash:
        query = query.where(AdminSession.token_hash != keep_hash)
    await session.execute(query)
    await session.flush()


async def create_admin_session(
    session: AsyncSession, user_id: int, token: str, ttl_hours: float = 24
) -> AdminSession:
    row = AdminSession(
        user_id=user_id,
        token_hash=hash_token(token),
        expires_at=utcnow() + timedelta(hours=ttl_hours),
        last_seen_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    return row


async def find_valid_session(session: AsyncSession, token: str) -> tuple[AdminSession, User] | None:
    """令牌 → 未过期会话及其用户；过期/未命中/用户停用 → None。"""
    result = await session.execute(
        select(AdminSession).where(AdminSession.token_hash == hash_token(token))
    )
    admin_session = result.scalars().first()
    if admin_session is None:
        return None
    user = await session.get(User, admin_session.user_id)
    if user is None or not user.enabled:
        return None
    expires = admin_session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= datetime.now(UTC):
        return None
    return admin_session, user


async def user_key_usage(session: AsyncSession) -> dict[int, tuple[int, Any]]:
    """按用户聚合（Key 数, 最后使用时间），供用户列表一次查询取数（避免 N+1）。"""
    rows = (
        await session.execute(
            select(
                UserApiKey.user_id,
                func.count(UserApiKey.id),
                func.max(UserApiKey.last_used_at),
            ).group_by(UserApiKey.user_id)
        )
    ).all()
    return {row[0]: (int(row[1]), row[2]) for row in rows}
