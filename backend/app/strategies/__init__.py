"""策略包。import 本模块即完成策略注册（网关经 get_strategy 查找）。"""

from app.strategies.base import (
    AllMembersFailed,
    CallOutcome,
    MemberSpec,
    Strategy,
    StrategyContext,
    StrategyNotFound,
    StrategyResult,
    StreamPlan,
    get_strategy,
    register_strategy,
    registered_strategies,
)
from app.strategies.council import CouncilStrategy

__all__ = [
    "AllMembersFailed",
    "CallOutcome",
    "CouncilStrategy",
    "MemberSpec",
    "Strategy",
    "StrategyContext",
    "StrategyNotFound",
    "StrategyResult",
    "StreamPlan",
    "get_strategy",
    "register_strategy",
    "registered_strategies",
]
