"""Model 管理 API：CRUD + 连通性测试（固定测试消息，真实调用一次该模型）。"""

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import AdapterError, LlmRequest, NormalizedMessage
from app.adapters.factory import build_adapter
from app.deps import get_session
from app.orm import LlmModel, Provider
from app.repos import Repository
from app.schemas import ModelCreate, ModelOut, ModelUpdate

router = APIRouter(prefix="/models", tags=["admin-models"])

# 与快照场景 model_test_msg 完全一致（确定性，hash 命中回放）
CONNECTIVITY_TEST_MESSAGE = "连通性测试：请只回复 pong"


@router.post("", status_code=201, response_model=ModelOut)
async def create_model(body: ModelCreate, session: AsyncSession = Depends(get_session)) -> ModelOut:
    provider = await Repository(session, Provider).get(body.provider_id)
    if provider is None:
        raise HTTPException(status_code=422, detail="provider_id 不存在")
    row = await Repository(session, LlmModel).create(**body.model_dump())
    await session.commit()
    return ModelOut.model_validate(row)


@router.get("", response_model=list[ModelOut])
async def list_models(session: AsyncSession = Depends(get_session)) -> list[ModelOut]:
    rows = await Repository(session, LlmModel).list()
    return [ModelOut.model_validate(r) for r in rows]


@router.get("/{model_id}", response_model=ModelOut)
async def get_model(model_id: int, session: AsyncSession = Depends(get_session)) -> ModelOut:
    row = await Repository(session, LlmModel).get(model_id)
    if row is None:
        raise HTTPException(status_code=404, detail="model not found")
    return ModelOut.model_validate(row)


@router.patch("/{model_id}", response_model=ModelOut)
async def update_model(
    model_id: int, body: ModelUpdate, session: AsyncSession = Depends(get_session)
) -> ModelOut:
    fields = body.model_dump(exclude_unset=True)
    if "provider_id" in fields:
        provider = await Repository(session, Provider).get(fields["provider_id"])
        if provider is None:
            raise HTTPException(status_code=422, detail="provider_id 不存在")
    row = await Repository(session, LlmModel).update(model_id, **fields)
    if row is None:
        raise HTTPException(status_code=404, detail="model not found")
    await session.commit()
    return ModelOut.model_validate(row)


@router.delete("/{model_id}", status_code=204)
async def delete_model(model_id: int, session: AsyncSession = Depends(get_session)) -> None:
    if not await Repository(session, LlmModel).delete(model_id):
        raise HTTPException(status_code=404, detail="model not found")
    await session.commit()


@router.post("/{model_id}/test")
async def test_model(
    model_id: int, request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    """连通性测试：业务结果（ok true/false）而非异常，便于 UI 直接展示。"""
    model = await Repository(session, LlmModel).get(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="model not found")
    provider = await Repository(session, Provider).get(model.provider_id)
    if provider is None:
        raise HTTPException(status_code=422, detail="模型所属 provider 不存在")

    adapter = build_adapter(
        provider,
        fernet_key=request.app.state.fernet_key,
        timeout_seconds=15,
        max_retries=0,
    )
    llm_request = LlmRequest(
        model=model.upstream_model_id,
        messages=[NormalizedMessage(role="user", content=CONNECTIVITY_TEST_MESSAGE)],
        temperature=0.0,
    )
    start = time.perf_counter()
    try:
        result = await adapter.complete(llm_request)
        return {"ok": True, "latency_ms": result.duration_ms, "content": result.content[:200]}
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
