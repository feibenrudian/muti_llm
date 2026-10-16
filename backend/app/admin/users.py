"""用户与 API Key 管理 API（多用户 T47）：用户 CRUD / 防自锁 / Key 签发与交付 / 兼容 Key 特例。

授权（grants）端点在 T49 追加于本文件。Key 明文只在创建响应出现一次；
列表仅掩码，/secret 端点供"复制 Key"（与设置页服务 Key 同款交付语义）。
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_svc import Caller, change_password, write_audit
from app.deps import get_caller, get_session
from app.keys_svc import as_utc_datetime, fernet, key_out, legacy_key_row
from app.orm import Provider, User, UserApiKey
from app.repos import (
    count_enabled_admins,
    delete_user_cascade,
    find_user_by_username,
    list_user_grants,
    replace_user_grants,
    revoke_user_sessions,
    user_key_usage,
)
from app.schemas import (
    GrantsPayload,
    KeyCreate,
    KeyCreatedOut,
    KeyOut,
    KeyUpdate,
    UserCreate,
    UserOut,
    UserPasswordReset,
    UserUpdate,
)
from app.security import (
    decrypt_secret,
    encrypt_secret,
    generate_password_salt,
    generate_user_key,
    hash_password,
    hash_token,
    mask_key,
)
from app.settings import settings

router = APIRouter(tags=["admin-users"])

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")


def _user_out(row: User, usage: dict[int, tuple[int, Any]]) -> UserOut:
    key_count, last_used = usage.get(row.id, (0, None))
    return UserOut(
        id=row.id,
        username=row.username,
        display_name=row.display_name,
        role=row.role,
        enabled=row.enabled,
        remark=row.remark,
        key_count=key_count,
        last_activity_at=as_utc_datetime(last_used),
        created_at=row.created_at,
    )


async def _get_user_or_404(session: AsyncSession, user_id: int) -> User:
    row = await session.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="user not found")
    return row


async def _assert_can_demote_or_disable(session: AsyncSession, row: User) -> None:
    """内置 admin 不可停用/降级/删除；其余管理员需保证仍有启用管理员（FR-17 防自锁）。"""
    if row.username == "admin":
        raise HTTPException(status_code=409, detail="内置管理员不可删除、停用或降级")
    if row.role == "admin" and (await count_enabled_admins(session, exclude_user_id=row.id)) == 0:
        raise HTTPException(status_code=409, detail="至少保留一名启用状态的管理员")


# ---- 用户 --------------------------------------------------------------------------


@router.get("/users")
async def list_users(
    session: AsyncSession = Depends(get_session),
    keyword: str | None = None,
    role: str | None = None,
    enabled: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    query = select(User)
    if keyword:
        like = f"%{keyword}%"
        query = query.where(User.username.like(like) | User.display_name.like(like))
    if role:
        query = query.where(User.role == role)
    if enabled is not None:
        query = query.where(User.enabled.is_(enabled))
    total = int(
        (await session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    )
    rows = (
        (
            await session.execute(
                query.order_by(User.id.desc()).offset((page - 1) * page_size).limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    usage = await user_key_usage(session)
    return {
        "items": [_user_out(r, usage) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/users", status_code=201)
async def create_user(
    body: UserCreate,
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
) -> UserOut:
    if not _USERNAME_RE.fullmatch(body.username):
        raise HTTPException(status_code=422, detail="用户名需为 3-32 位字母/数字/._-")
    if body.role == "admin" and body.password is None:
        raise HTTPException(status_code=422, detail="创建管理员必须设置初始密码")
    if await find_user_by_username(session, body.username) is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    row = User(
        username=body.username,
        display_name=body.display_name,
        role=body.role,
        remark=body.remark,
    )
    if body.password is not None:
        salt = generate_password_salt()
        row.password_salt = salt
        row.password_hash = hash_password(body.password, salt, settings.pbkdf2_iterations)
    session.add(row)
    await session.flush()
    await write_audit(
        session,
        actor=caller,
        action="user.create",
        target_type="user",
        target_id=str(row.id),
        detail={"username": row.username, "role": row.role},
    )
    await session.commit()
    return _user_out(row, await user_key_usage(session))


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    body: UserUpdate,
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
) -> UserOut:
    row = await _get_user_or_404(session, user_id)
    fields = body.model_dump(exclude_unset=True)
    demoting = ("enabled" in fields and not fields["enabled"]) or (
        "role" in fields and fields["role"] != "admin"
    )
    if demoting:
        await _assert_can_demote_or_disable(session, row)
    promoting = fields.get("role") == "admin" and row.role != "admin"
    if promoting:
        # 提升为管理员必须设置初始密码（需求 D23；已有口令的用户可直接提升）
        password = fields.pop("password", None)
        if password is None and not row.password_hash:
            raise HTTPException(status_code=422, detail="提升为管理员必须设置初始密码")
        if password is not None:
            salt = generate_password_salt()
            row.password_salt = salt
            row.password_hash = hash_password(password, salt, settings.pbkdf2_iterations)
    elif "password" in fields:
        # 非提升场景随 PATCH 传密码：视为重置口令（少一个端点，语义等同 /password）
        password = fields.pop("password")
        try:
            await change_password(session, row.id, new_password=password, admin_reset=True)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    for key, value in fields.items():
        setattr(row, key, value)
    await session.flush()
    await write_audit(
        session,
        actor=caller,
        action="user.update",
        target_type="user",
        target_id=str(row.id),
        detail={"fields": sorted(k for k in fields if k != "password")},
    )
    await session.commit()
    return _user_out(row, await user_key_usage(session))


@router.post("/users/{user_id}/password")
async def reset_user_password(
    user_id: int,
    body: UserPasswordReset,
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    row = await _get_user_or_404(session, user_id)
    try:
        await change_password(session, row.id, new_password=body.new_password, admin_reset=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await revoke_user_sessions(session, row.id)  # 被重置者全部会话吊销
    await session.commit()
    return {"ok": True}


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: int,
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
) -> None:
    row = await _get_user_or_404(session, user_id)
    await _assert_can_demote_or_disable(session, row)
    await write_audit(
        session,
        actor=caller,
        action="user.delete",
        target_type="user",
        target_id=str(user_id),
        detail={"username": row.username},
    )
    await delete_user_cascade(session, user_id)
    await session.commit()


# ---- 用户 API Key ------------------------------------------------------------------


async def _get_key_or_special(session: AsyncSession, user_id: int, key_id: int) -> UserApiKey:
    """key_id=0 为兼容 Key 特例行（无库行）：修改一律 409；其余返回行或 404。"""
    if key_id == 0:
        raise HTTPException(status_code=409, detail="兼容 Key 不可修改，重置请到设置页")
    row = await session.get(UserApiKey, key_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="key not found")
    return row


def _key_created_out(row: UserApiKey, plaintext: str) -> KeyCreatedOut:
    return KeyCreatedOut(
        id=row.id,
        name=row.name,
        masked=mask_key(plaintext),
        enabled=row.enabled,
        created_by=row.created_by,
        last_used_at=None,
        created_at=row.created_at,
        api_key=plaintext,
    )


@router.get("/users/{user_id}/keys")
async def list_keys(user_id: int, session: AsyncSession = Depends(get_session)) -> list[KeyOut]:
    user = await _get_user_or_404(session, user_id)
    rows = (
        (await session.execute(select(UserApiKey).where(UserApiKey.user_id == user_id)))
        .scalars()
        .all()
    )
    items = [key_out(r, r.key_encrypted) for r in rows]
    if user.username == "admin":
        items.insert(0, await legacy_key_row(session))
    return items


@router.post("/users/{user_id}/keys", status_code=201)
async def create_key(
    user_id: int,
    body: KeyCreate,
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
) -> KeyCreatedOut:
    await _get_user_or_404(session, user_id)
    plaintext = generate_user_key()
    row = UserApiKey(
        user_id=user_id,
        name=body.name,
        key_hash=hash_token(plaintext),
        key_encrypted=encrypt_secret(plaintext, fernet()),
        created_by=caller.user_id,
    )
    session.add(row)
    await session.flush()
    await write_audit(
        session,
        actor=caller,
        action="key.create",
        target_type="user",
        target_id=str(user_id),
        detail={"key_id": row.id, "name": row.name},
    )
    await session.commit()
    return _key_created_out(row, plaintext)


@router.get("/users/{user_id}/keys/{key_id}/secret")
async def key_secret(
    user_id: int, key_id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, str]:
    """复制 Key：解密 Fernet 副本（明文不落库、不进列表；AGENTS.md 掩码约束针对上游 Key）。"""
    await _get_user_or_404(session, user_id)
    row = await _get_key_or_special(session, user_id, key_id)
    if not row.key_encrypted:
        raise HTTPException(status_code=409, detail="该 Key 无加密副本（旧数据），请重新签发")
    return {"api_key": decrypt_secret(row.key_encrypted, fernet())}


@router.patch("/users/{user_id}/keys/{key_id}")
async def update_key(
    user_id: int,
    key_id: int,
    body: KeyUpdate,
    session: AsyncSession = Depends(get_session),
) -> KeyOut:
    await _get_user_or_404(session, user_id)
    row = await _get_key_or_special(session, user_id, key_id)
    fields = body.model_dump(exclude_unset=True)
    for key, value in fields.items():
        setattr(row, key, value)
    await session.flush()
    await session.commit()
    return key_out(row, row.key_encrypted)


@router.delete("/users/{user_id}/keys/{key_id}", status_code=204)
async def delete_key(
    user_id: int, key_id: int, session: AsyncSession = Depends(get_session)
) -> None:
    await _get_user_or_404(session, user_id)
    row = await _get_key_or_special(session, user_id, key_id)
    await session.delete(row)
    await session.flush()
    await session.commit()


# ---- 模型访问授权（T49）------------------------------------------------------------


async def _validate_grant_targets(
    session: AsyncSession, pipelines: list[int], models: list[int], providers: list[int]
) -> None:
    from app.orm import LlmModel, Pipeline

    for target_id in pipelines:
        if await session.get(Pipeline, target_id) is None:
            raise HTTPException(status_code=422, detail=f"pipeline 不存在: id={target_id}")
    for target_id in models:
        if await session.get(LlmModel, target_id) is None:
            raise HTTPException(status_code=422, detail=f"model 不存在: id={target_id}")
    for target_id in providers:
        if await session.get(Provider, target_id) is None:
            raise HTTPException(status_code=422, detail=f"provider 不存在: id={target_id}")


@router.get("/users/{user_id}/grants")
async def get_grants(
    user_id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, list[int]]:
    await _get_user_or_404(session, user_id)
    rows = await list_user_grants(session, user_id)
    return {
        "pipelines": sorted(r.target_id for r in rows if r.kind == "pipeline"),
        "models": sorted(r.target_id for r in rows if r.kind == "model"),
        "providers": sorted(r.target_id for r in rows if r.kind == "provider"),
    }


@router.put("/users/{user_id}/grants")
async def put_grants(
    user_id: int,
    body: GrantsPayload,
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
) -> dict[str, list[int]]:
    """覆盖式重设授权（即时生效：校验直查库，无缓存）。"""
    await _get_user_or_404(session, user_id)
    await _validate_grant_targets(session, body.pipelines, body.models, body.providers)
    await replace_user_grants(
        session,
        user_id,
        {
            "pipeline": set(body.pipelines),
            "model": set(body.models),
            "provider": set(body.providers),
        },
    )
    await write_audit(
        session,
        actor=caller,
        action="grant.update",
        target_type="user",
        target_id=str(user_id),
        detail={
            "pipelines": len(body.pipelines),
            "models": len(body.models),
            "providers": len(body.providers),
        },
    )
    await session.commit()
    rows = await list_user_grants(session, user_id)
    return {
        "pipelines": sorted(r.target_id for r in rows if r.kind == "pipeline"),
        "models": sorted(r.target_id for r in rows if r.kind == "model"),
        "providers": sorted(r.target_id for r in rows if r.kind == "provider"),
    }
