"""Pipeline 管理 API：CRUD + 成员有序配置 + 校验（≥1 成员 + 1 裁判、名称唯一且合法）。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_session
from app.orm import LlmModel, Pipeline, PipelineMember
from app.repos import Repository, find_pipeline_by_name
from app.schemas import PipelineCreate, PipelineMemberOut, PipelineOut, PipelineUpdate
from app.strategies import StrategyNotFound, get_strategy

router = APIRouter(prefix="/pipelines", tags=["admin-pipelines"])


async def _ordered_members(session: AsyncSession, pipeline_id: int) -> list[PipelineMember]:
    result = await session.execute(
        select(PipelineMember)
        .where(PipelineMember.pipeline_id == pipeline_id)
        .order_by(PipelineMember.sort_order, PipelineMember.id)
    )
    return list(result.scalars())


async def _to_out(session: AsyncSession, row: Pipeline) -> PipelineOut:
    payload = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    payload["members"] = [
        PipelineMemberOut.model_validate(m) for m in await _ordered_members(session, row.id)
    ]
    return PipelineOut(**payload)


def _validate_strategy_params(strategy_name: str, params: dict | None) -> dict:
    """按策略注册表校验 strategy_params（D13）：非法 → 422；返回填充默认值后的参数。"""
    try:
        return get_strategy(strategy_name).validate_params(params or {})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


async def _validate_refs(session: AsyncSession, body: PipelineCreate | PipelineUpdate) -> None:
    if body.strategy is not None:
        try:
            get_strategy(body.strategy)
        except StrategyNotFound:
            raise HTTPException(status_code=422, detail=f"未注册的策略: {body.strategy}") from None
    if body.judge_model_id is not None:
        if await Repository(session, LlmModel).get(body.judge_model_id) is None:
            raise HTTPException(status_code=422, detail="judge_model_id 不存在")
    if body.members is not None:
        for m in body.members:
            if await Repository(session, LlmModel).get(m.model_id) is None:
                raise HTTPException(status_code=422, detail=f"成员 model_id={m.model_id} 不存在")


async def _replace_members(session: AsyncSession, pipeline_id: int, members: list) -> None:
    for m in await _ordered_members(session, pipeline_id):
        await session.delete(m)
    await session.flush()
    for order, m in enumerate(members):
        session.add(
            PipelineMember(
                pipeline_id=pipeline_id,
                model_id=m.model_id,
                sort_order=order,
                param_overrides=m.param_overrides,
            )
        )
    await session.flush()


@router.post("", status_code=201, response_model=PipelineOut)
async def create_pipeline(
    body: PipelineCreate, session: AsyncSession = Depends(get_session)
) -> PipelineOut:
    await _validate_refs(session, body)
    if await find_pipeline_by_name(session, body.name) is not None:
        raise HTTPException(status_code=409, detail=f"Pipeline 名已存在: {body.name}")
    row = await Repository(session, Pipeline).create(
        name=body.name,
        strategy=body.strategy,
        strategy_params=_validate_strategy_params(body.strategy, body.strategy_params),
        judge_model_id=body.judge_model_id,
        judge_prompt_template=body.judge_prompt_template,
        member_timeout_seconds=body.member_timeout_seconds,
        fault_tolerance=body.fault_tolerance,
        max_concurrency=body.max_concurrency,
        enabled=body.enabled,
    )
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"Pipeline 名已存在: {body.name}") from None
    await _replace_members(session, row.id, body.members)
    await session.commit()
    await session.refresh(row)
    return await _to_out(session, row)


@router.get("", response_model=list[PipelineOut])
async def list_pipelines(session: AsyncSession = Depends(get_session)) -> list[PipelineOut]:
    rows = await Repository(session, Pipeline).list()
    return [await _to_out(session, r) for r in rows]


@router.get("/{pipeline_id}", response_model=PipelineOut)
async def get_pipeline(
    pipeline_id: int, session: AsyncSession = Depends(get_session)
) -> PipelineOut:
    row = await Repository(session, Pipeline).get(pipeline_id)
    if row is None:
        raise HTTPException(status_code=404, detail="pipeline not found")
    return await _to_out(session, row)


@router.patch("/{pipeline_id}", response_model=PipelineOut)
async def update_pipeline(
    pipeline_id: int, body: PipelineUpdate, session: AsyncSession = Depends(get_session)
) -> PipelineOut:
    row = await Repository(session, Pipeline).get(pipeline_id)
    if row is None:
        raise HTTPException(status_code=404, detail="pipeline not found")
    await _validate_refs(session, body)

    if body.name is not None and body.name != row.name:
        if await find_pipeline_by_name(session, body.name) is not None:
            raise HTTPException(status_code=409, detail=f"Pipeline 名已存在: {body.name}")

    fields = body.model_dump(exclude_unset=True, exclude={"members", "strategy_params"})
    # 策略/参数任一变更都按"生效策略"重新校验并规范化（未提供 strategy_params 时沿用存量值）
    row.strategy_params = _validate_strategy_params(
        body.strategy if body.strategy is not None else row.strategy,
        body.strategy_params if body.strategy_params is not None else row.strategy_params,
    )
    try:
        for key, value in fields.items():
            setattr(row, key, value)
        await session.flush()
    except IntegrityError:
        await session.rollback()
        name = fields.get("name")
        raise HTTPException(status_code=409, detail=f"Pipeline 名已存在: {name}") from None
    if body.members is not None:
        await _replace_members(session, pipeline_id, body.members)
    await session.commit()
    await session.refresh(row)
    return await _to_out(session, row)


@router.delete("/{pipeline_id}", status_code=204)
async def delete_pipeline(pipeline_id: int, session: AsyncSession = Depends(get_session)) -> None:
    if not await Repository(session, Pipeline).delete(pipeline_id):
        raise HTTPException(status_code=404, detail="pipeline not found")
    await session.commit()
