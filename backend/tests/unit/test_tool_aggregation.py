"""T44 工具聚合纯函数：UT-44-1..3（意图文本化 / schema 兜底校验 / 降级择优）。"""

from app.strategies.base import CallOutcome
from app.strategies.council import (
    format_tool_intent,
    pick_fallback_tool_calls,
    validate_tool_calls,
)

TOOLS = [
    {"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}},
    {"type": "function", "function": {"name": "get_time", "parameters": {"type": "object"}}},
]


def _call(name: str, arguments: str) -> dict:
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _member(**kwargs) -> CallOutcome:
    defaults: dict = {
        "role": "member",
        "model_id": 1,
        "upstream_model_id": "m",
        "provider_name": "p",
        "request_payload": {},
    }
    defaults.update(kwargs)
    return CallOutcome(**defaults)


def test_format_tool_intent() -> None:
    """UT-44-1 意图文本化：纯调用 / 调用+附说明 / 纯文本回答 三形态。"""
    assert (
        format_tool_intent(_member(response_tool_calls=[_call("get_weather", '{"city": "广州"}')]))
        == '决定调用工具: get_weather({"city": "广州"})'
    )
    assert (
        format_tool_intent(
            _member(
                response_content="我来查天气",
                response_tool_calls=[_call("get_weather", '{"city": "广州"}')],
            )
        )
        == '决定调用工具: get_weather({"city": "广州"})（附说明: 我来查天气）'
    )
    assert format_tool_intent(_member(response_content="今天很热")) == "今天很热"
    assert format_tool_intent(_member()) == "（无输出）"


def test_validate_tool_calls() -> None:
    """UT-44-2 schema 兜底校验：合法通过；未知函数名/非法 JSON/非对象/空 拒绝。"""
    assert validate_tool_calls([_call("get_weather", '{"city": "广州"}')], TOOLS)
    assert validate_tool_calls([_call("get_time", "{}")], TOOLS)
    assert not validate_tool_calls([], TOOLS)  # 空
    assert not validate_tool_calls([_call("unknown_tool", "{}")], TOOLS)  # 未知函数名
    assert not validate_tool_calls([_call("get_weather", "not-json")], TOOLS)  # 非法 JSON
    assert not validate_tool_calls([_call("get_weather", '"text"')], TOOLS)  # 非对象


def test_pick_fallback_tool_calls_majority() -> None:
    """UT-44-3 降级择优：多数 function.name 派系中取第一个产出该工具的成员（确定性）。"""
    a = _member(response_tool_calls=[_call("get_weather", "{}")])
    b = _member(response_tool_calls=[_call("get_time", "{}")])
    c = _member(response_tool_calls=[_call("get_weather", '{"city": "广州"}')])
    # get_weather 为多数派，配置序 a 在前
    assert pick_fallback_tool_calls([b, a, c]) == a.response_tool_calls
    assert pick_fallback_tool_calls([b]) == b.response_tool_calls
    assert pick_fallback_tool_calls([_member(), _member()]) is None  # 无人产出调用
