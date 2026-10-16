"""门户 API Key 自助管理（T52）：本人 Key 的签发 / 列表 / 复制 / 启停 / 删除。

全部以 caller.user_id 归属，非本人 Key 一律 404（防枚举）。列表无兼容 Key 特例行
（兼容 Key 归内置 admin，仅管理台可见）；签发与删除写审计（action 带 portal. 前缀）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_svc import Caller, write_audit
from app.deps import get_caller, get_session
from app.keys_svc import fernet, key_out
from app.orm import UserApiKey
from app.schemas import KeyCreate, KeyCreatedOut, KeyOut, KeyUpdate
from app.security import decrypt_secret, encrypt_secret, generate_user_key, hash_token, mask_key

router = APIRouter(tags=["portal-keys"])


async def _own_key_or_404(session: AsyncSession, user_id: int, key_id: int) -> UserApiKey:
    row = await session.get(UserApiKey, key_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="key not found")
    return row


@router.get("/keys")
async def list_own_keys(
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
) -> list[KeyOut]:
    rows = (
        (await session.execute(select(UserApiKey).where(UserApiKey.user_id == caller.user_id)))
        .scalars()
        .all()
    )
    return [key_out(r, r.key_encrypted) for r in rows]


@router.post("/keys", status_code=201)
async def create_own_key(
    body: KeyCreate,
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
) -> KeyCreatedOut:
    plaintext = generate_user_key()
    row = UserApiKey(
        user_id=caller.user_id,
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
        action="portal.key.create",
        target_type="user",
        target_id=str(caller.user_id),
        detail={"key_id": row.id, "name": row.name},
    )
    await session.commit()
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


@router.get("/keys/{key_id}/secret")
async def own_key_secret(
    key_id: int,
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
) -> dict[str, str]:
    """复制 Key：解密本人 Key 的 Fernet 副本（语义同管理端 /secret）。"""
    row = await _own_key_or_404(session, caller.user_id, key_id)
    if not row.key_encrypted:
        raise HTTPException(status_code=409, detail="该 Key 无加密副本（旧数据），请重新签发")
    return {"api_key": decrypt_secret(row.key_encrypted, fernet())}


@router.patch("/keys/{key_id}")
async def update_own_key(
    key_id: int,
    body: KeyUpdate,
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
) -> KeyOut:
    row = await _own_key_or_404(session, caller.user_id, key_id)
    fields = body.model_dump(exclude_unset=True)
    for key, value in fields.items():
        setattr(row, key, value)
    await session.flush()
    await session.commit()
    return key_out(row, row.key_encrypted)


@router.delete("/keys/{key_id}", status_code=204)
async def delete_own_key(
    key_id: int,
    session: AsyncSession = Depends(get_session),
    caller: Caller = Depends(get_caller),
) -> None:
    row = await _own_key_or_404(session, caller.user_id, key_id)
    await write_audit(
        session,
        actor=caller,
        action="portal.key.delete",
        target_type="user",
        target_id=str(caller.user_id),
        detail={"key_id": row.id, "name": row.name},
    )
    await session.delete(row)
    await session.flush()
    await session.commit()
