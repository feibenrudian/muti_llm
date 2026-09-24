"""Provider 管理 API：CRUD + 连通性测试 + 模型自动同步 + 级联删除；api_key 加密、响应只回掩码。"""

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import AdapterError
from app.adapters.factory import build_adapter, decrypt_provider_key
from app.deps import get_session
from app.orm import LlmModel, Provider
from app.repos import Repository, delete_models_cascade, derive_slug
from app.schemas import ProviderCreate, ProviderOut, ProviderUpdate
from app.security import encrypt_secret, mask_key

router = APIRouter(prefix="/providers", tags=["admin-providers"])


def _invalidate_route_list(provider_id: int) -> None:
    """路由视图的上游列表缓存随供应商配置变更失效（避免跨模块常驻导入，延迟导入）。"""
    from app.gateways.namespaces import invalidate_route_list

    invalidate_route_list(provider_id)


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
        slug=row.slug,
        protocol=row.protocol,
        base_url=row.base_url,
        api_key_masked=masked,
        remark=row.remark,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _sync_models(
    session: AsyncSession, provider_row: Provider, upstream_ids: list[str]
) -> tuple[list[str], list[str]]:
    """按上游模型列表全量 diff 同步该 Provider 的 LlmModel，返回 (新增, 本次下线)。

    缺失的补建（display_name=模型 ID）；上游已消失的标记下线（upstream_missing=True 且
    enabled=False，防止用户继续配置过时模型）；标记过的模型重新上架则自动恢复 enabled。
    手动停用（upstream_missing=False）的模型不受同步影响。"""
    rows = (
        (await session.execute(select(LlmModel).where(LlmModel.provider_id == provider_row.id)))
        .scalars()
        .all()
    )
    existing = {row.upstream_model_id: row for row in rows}
    upstream_set = set(upstream_ids)
    added: list[str] = []
    for upstream_id in upstream_ids:
        if upstream_id in existing:
            continue
        await Repository(session, LlmModel).create(
            provider_id=provider_row.id,
            display_name=upstream_id,
            upstream_model_id=upstream_id,
        )
        added.append(upstream_id)
    removed: list[str] = []
    for row in rows:
        if row.upstream_model_id in upstream_set:
            if row.upstream_missing:  # 重新上架：恢复同步时的联动停用
                await Repository(session, LlmModel).update(
                    row.id, upstream_missing=False, enabled=True
                )
        elif not row.upstream_missing:  # 尚未标记 → 本次新下线；已标记不动（用户手动启用不被覆盖）
            await Repository(session, LlmModel).update(
                row.id, upstream_missing=True, enabled=False
            )
            removed.append(row.upstream_model_id)
    return added, removed


@router.post("", status_code=201, response_model=ProviderOut)
async def create_provider(
    body: ProviderCreate, request: Request, session: AsyncSession = Depends(get_session)
) -> ProviderOut:
    fernet_key: bytes = request.app.state.fernet_key
    row = await Repository(session, Provider).create(
        name=body.name,
        slug="",  # 占位：flush 拿到 id 后立即回填（派生不出时兜底 provider-{id}）
        protocol=body.protocol,
        base_url=body.base_url,
        api_key_encrypted=encrypt_secret(body.api_key, fernet_key) if body.api_key else "",
        remark=body.remark,
        enabled=body.enabled,
    )
    taken = set((await session.execute(select(Provider.slug))).scalars())
    row.slug = derive_slug(body.name, taken, fallback=f"provider-{row.id}")
    await session.flush()
    await session.commit()
    # 建好即按上游模型列表自动同步（best-effort）：上游不可达不阻塞创建，之后点"测试"会再同步
    try:
        adapter = build_adapter(row, fernet_key=fernet_key, timeout_seconds=15, max_retries=0)
        await _sync_models(session, row, await adapter.probe())
        await session.commit()
    except Exception:
        await session.rollback()
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
    _invalidate_route_list(provider_id)
    return to_out(row, fernet_key)


@router.delete("/{provider_id}", status_code=204)
async def delete_provider(provider_id: int, session: AsyncSession = Depends(get_session)) -> None:
    """级联删除：Provider 连同其全部模型；引用这些模型的 Pipeline 成员、
    以及裁判失效/成员被清空的 Pipeline 一并删除（保持存留 Pipeline 均可运行）。"""
    row = await Repository(session, Provider).get(provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")
    model_ids = list(
        (
            await session.execute(select(LlmModel.id).where(LlmModel.provider_id == provider_id))
        ).scalars()
    )
    await delete_models_cascade(session, model_ids)
    await session.execute(delete(Provider).where(Provider.id == provider_id))
    await session.commit()
    _invalidate_route_list(provider_id)


@router.post("/{provider_id}/test")
async def test_provider(
    provider_id: int, request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    """连通性测试（业务结果而非异常，便于 UI 展示）：GET 上游模型列表，验证 URL/Key 可用；
    成功时顺带全量同步模型：新增上游新出现的模型，停用（不下删）上游已消失的模型。"""
    row = await Repository(session, Provider).get(provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    adapter = build_adapter(
        row,
        fernet_key=request.app.state.fernet_key,
        timeout_seconds=15,
        max_retries=0,
    )
    start = time.perf_counter()
    try:
        model_ids = await adapter.probe()
        synced, removed = await _sync_models(session, row, model_ids)
        await session.commit()
        _invalidate_route_list(provider_id)  # 上游列表变了，路由视图缓存即时失效
        return {
            "ok": True,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "models": model_ids[:50],
            "synced": synced,
            "removed": removed,
        }
    except AdapterError as exc:
        return {
            "ok": False,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "error": str(exc),
        }
    except Exception as exc:  # 诊断类端点：未知异常也降级为业务结果，避免 500 让 UI 无反馈
        return {
            "ok": False,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "error": f"未知错误: {exc}",
        }
