"""UT-13-1..2：策略引擎框架。"""

import pytest

from app.strategies import StrategyNotFound, get_strategy, register_strategy, registered_strategies
from app.strategies.base import Strategy, StrategyContext, StrategyResult


def test_registry_lookup() -> None:
    """UT-13-1 注册与查找：council 已注册可取到；未知名抛 StrategyNotFound。"""
    assert "council" in registered_strategies()
    strategy_cls = get_strategy("council")
    assert strategy_cls.name == "council"

    with pytest.raises(StrategyNotFound):
        get_strategy("voting-not-registered")

    # 重复 name 或默认 name 不允许注册
    class Bad(Strategy):
        name = "abstract"

        async def run(self, ctx: StrategyContext) -> StrategyResult:  # pragma: no cover
            raise NotImplementedError

        async def prepare_stream(self, ctx: StrategyContext):  # pragma: no cover
            raise NotImplementedError

    with pytest.raises(ValueError):
        register_strategy(Bad)


def test_result_structure() -> None:
    """UT-13-2 结果结构：final_content/usage 汇总/degraded/成员明细。"""
    from app.adapters.base import LlmUsage
    from app.strategies.base import CallOutcome

    outcome = CallOutcome(
        role="member",
        model_id=1,
        upstream_model_id="m-a",
        provider_name="P",
        request_payload={"model": "m-a"},
        response_content="ans",
        usage=LlmUsage(prompt_tokens=3, completion_tokens=2),
    )
    result = StrategyResult(
        final_content="final",
        usage=LlmUsage(prompt_tokens=3, completion_tokens=2),
        degraded=False,
        members=[outcome],
        judge=None,
    )
    assert result.final_content == "final"
    assert result.usage.total_tokens == 5
    assert result.degraded is False
    assert [m.upstream_model_id for m in result.members] == ["m-a"]
    assert result.members[0].usage.prompt_tokens == 3
