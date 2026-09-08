"""UT-32-1：ICE 策略参数校验与注册（council 拒绝非空参数）。"""

import pytest

from app.strategies import get_strategy, registered_strategies
from app.strategies.ice import IceStrategy


def test_ice_registered() -> None:
    """UT-32-1 ICE 参数校验：ice 已注册，可经注册表取到。"""
    assert "ice" in registered_strategies()
    assert get_strategy("ice") is IceStrategy


def test_ice_validate_params_defaults() -> None:
    """UT-32-1 ICE 参数校验：空参数与部分参数均自动填充默认值。"""
    assert IceStrategy.validate_params({}) == {
        "max_rounds": 3,
        "confidence_threshold": 0.8,
        "stagnation": True,
        "progress_comments": True,
    }
    filled = IceStrategy.validate_params({"max_rounds": 2, "stagnation": False})
    assert filled == {
        "max_rounds": 2,
        "confidence_threshold": 0.8,
        "stagnation": False,
        "progress_comments": True,
    }


def test_ice_validate_params_range() -> None:
    """UT-32-1 ICE 参数校验：max_rounds=6/0、threshold=1.5/-0.1 越界 → ValueError。"""
    bad_params = (
        {"max_rounds": 6},
        {"max_rounds": 0},
        {"confidence_threshold": 1.5},
        {"confidence_threshold": -0.1},
    )
    for bad in bad_params:
        with pytest.raises(ValueError):
            IceStrategy.validate_params(bad)
    # 边界值合法
    assert IceStrategy.validate_params({"max_rounds": 1})["max_rounds"] == 1
    assert IceStrategy.validate_params({"max_rounds": 5})["max_rounds"] == 5
    assert IceStrategy.validate_params({"confidence_threshold": 0})["confidence_threshold"] == 0.0
    assert IceStrategy.validate_params({"confidence_threshold": 1})["confidence_threshold"] == 1.0


def test_ice_validate_params_type() -> None:
    """UT-32-1 ICE 参数校验：类型错（字符串/bool 冒充数值）→ ValueError。"""
    for bad in (
        {"max_rounds": "3"},
        {"max_rounds": True},  # bool 是 int 子类，必须显式拒绝
        {"confidence_threshold": "0.8"},
        {"stagnation": "yes"},
        {"progress_comments": 1},
    ):
        with pytest.raises(ValueError):
            IceStrategy.validate_params(bad)


def test_ice_validate_params_unknown_key() -> None:
    """UT-32-1 ICE 参数校验：未知参数键 → ValueError。"""
    with pytest.raises(ValueError):
        IceStrategy.validate_params({"rounds": 3})


def test_council_validate_params_rejects_any() -> None:
    """UT-32-1 ICE 参数校验：council 传非空 strategy_params → ValueError，空参数通过。"""
    council = get_strategy("council")
    assert council.validate_params({}) == {}
    with pytest.raises(ValueError):
        council.validate_params({"max_rounds": 3})
