"""通用依赖：DB session（来自 app.state.session_factory）与管理端点身份（中间件写入）。"""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_svc import Caller


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_caller(request: Request) -> Caller:
    """端点取操作者身份（authenticate 中间件已保证存在；仅 /api/admin/* 与 /api/portal/* 可达）。"""
    caller: Caller = request.state.caller
    return caller
