"""Provider 管理 API：CRUD + 启停；api_key Fernet 加密入库、响应只回掩码。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.factory import decrypt_provider_key
from app.deps import get_session
from app.orm import LlmModel, Provider
from app.repos import Repository
from app.schemas import ProviderCreate, ProviderOut, ProviderUpdate
from app.security import encrypt_secret, mask_key

router = APIRouter(prefix="/providers", tags=["admin-providers"])


def to_out(row: Provider, fernet_key: bytes) -> ProviderOut:
    masked = ""
    if row.api_key_encrypted:
        try:
            masked = mask_key(decrypt_provider_key(row.api_key_encrypted, fernet_key))
        except Exception:  # 密钥轮换后无法解密的旧数据，退化为不显示
            masked = "****"
    return ProviderOut(
        id=row.id,
        name=row.name,
        protocol=row.protocol,
        base_url=row.base_url,
        api_key_masked=masked,
        remark=row.remark,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post("", status_code=201, response_model=ProviderOut)
async def create_provider(
    body: ProviderCreate, request: Request, session: AsyncSession = Depends(get_session)
) -> ProviderOut:
    fernet_key: bytes = request.app.state.fernet_key
    row = await Repository(session, Provider).create(
        name=body.name,
        protocol=body.protocol,
        base_url=body.base_url,
        api_key_encrypted=encrypt_secret(body.api_key, fernet_key) if body.api_key else "",
        remark=body.remark,
        enabled=body.enabled,
    )
    await session.commit()
    return to_out(row, fernet_key)


@router.get("", response_model=list[ProviderOut])
async def list_providers(
    request: Request, session: AsyncSession = Depends(get_session)
) -> list[ProviderOut]:
    fernet_key: bytes = request.app.state.fernet_key
    rows = await Repository(session, Provider).list()
    return [to_out(r, fernet_key) for r in rows]


@router.get("/{provider_id}", response_model=ProviderOut)
async def get_provider(
    provider_id: int, request: Request, session: AsyncSession = Depends(get_session)
) -> ProviderOut:
    fernet_key: bytes = request.app.state.fernet_key
    row = await Repository(session, Provider).get(provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")
    return to_out(row, fernet_key)


@router.patch("/{provider_id}", response_model=ProviderOut)
async def update_provider(
    provider_id: int,
    body: ProviderUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ProviderOut:
    fernet_key: bytes = request.app.state.fernet_key
    fields = body.model_dump(exclude_unset=True)
    if "api_key" in fields:
        api_key = fields.pop("api_key")
        fields["api_key_encrypted"] = encrypt_secret(api_key, fernet_key) if api_key else ""
    row = await Repository(session, Provider).update(provider_id, **fields)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")
    await session.commit()
    return to_out(row, fernet_key)


@router.delete("/{provider_id}", status_code=204)
async def delete_provider(provider_id: int, session: AsyncSession = Depends(get_session)) -> None:
    count = (
        await session.execute(
            select(func.count()).select_from(LlmModel).where(LlmModel.provider_id == provider_id)
        )
    ).scalar_one()
    if count > 0:
        raise HTTPException(status_code=409, detail=f"Provider 下仍有 {count} 个模型，先删除模型")
    if not await Repository(session, Provider).delete(provider_id):
        raise HTTPException(status_code=404, detail="provider not found")
    await session.commit()
