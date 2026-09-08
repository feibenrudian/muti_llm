"""元信息端点：默认裁判模板（前端"恢复默认"按钮的单一数据来源）+ 已注册策略列表。"""

from fastapi import APIRouter

from app.strategies import registered_strategies
from app.strategies.council import DEFAULT_JUDGE_TEMPLATE

router = APIRouter(prefix="/meta", tags=["admin-meta"])


@router.get("/judge-template")
async def default_judge_template() -> dict[str, str]:
    return {"template": DEFAULT_JUDGE_TEMPLATE}


@router.get("/strategies")
async def list_strategies() -> list[str]:
    return registered_strategies()
