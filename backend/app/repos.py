"""基础仓储：泛型 CRUD + 特殊查询。管理 API（T08+）与策略层统一经由此层访问 ORM。"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.orm import AppSetting, LlmModel, ModelCallLog, Pipeline


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


async def list_model_calls(session: AsyncSession, request_id: int) -> list[ModelCallLog]:
    """某次请求的全部上游调用明细，按发生顺序（created_at, id）。"""
    result = await session.execute(
        select(ModelCallLog)
        .where(ModelCallLog.request_id == request_id)
        .order_by(ModelCallLog.created_at, ModelCallLog.id)
    )
    return list(result.scalars())
