"""UT-53-1..3：validate_chat_body 的工具协议超集校验（透传放宽 / 聚合前置标志 / 非法形态拒绝）。"""

from fastapi.responses import JSONResponse

from app.gateways.llm_gateway import _llm_request, validate_chat_body
from app.orm import LlmModel

USER_MSG = {"role": "user", "content": "查一下天气"}
ASSISTANT_TOOL_CALL = {
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
    ],
}
TOOL_RESULT = {"role": "tool", "content": "晴，25°C", "tool_call_id": "call_1"}


def _validate(messages: list) -> tuple[JSONResponse | None, bool]:
    error, _msgs, _model, _params, _stream, _has_tools, tool_history = validate_chat_body(
        {"model": "m", "messages": messages}
    )
    return error, tool_history


def test_valid_base_messages() -> None:
    """UT-53-1 基础消息（system/user/assistant 字符串）通过且 tool_history=False。"""
    error, tool_history = _validate(
        [
            {"role": "system", "content": "s"},
            USER_MSG,
            {"role": "assistant", "content": "a"},
        ]
    )
    assert error is None
    assert tool_history is False


def test_tool_protocol_messages_accepted() -> None:
    """UT-53-2 工具协议回放放行：assistant(content=null)+tool_calls 与 role=tool 均通过并置位。"""
    error, tool_history = _validate([USER_MSG, ASSISTANT_TOOL_CALL, TOOL_RESULT])
    assert error is None
    assert tool_history is True

    # assistant 字符串 content + tool_calls（声明调用的变体）同样置位
    declared = dict(ASSISTANT_TOOL_CALL, content="我来查一下")
    error, tool_history = _validate([USER_MSG, declared, TOOL_RESULT])
    assert error is None
    assert tool_history is True


def test_invalid_shapes_rejected() -> None:
    """UT-53-3 非法形态一律 400：非法 role/缺 content/非 dict/content=null 无 tool_calls/
    tool 缺 tool_call_id/tool content 非字符串/tool_calls 结构不完整。"""
    bad_cases = [
        [{"role": "wizard", "content": "hi"}],
        [{"role": "user"}],
        ["just a string"],
        [{"role": "assistant", "content": None}],  # 无 tool_calls 的空 content
        [dict(ASSISTANT_TOOL_CALL, tool_calls=[])],  # tool_calls 空数组
        [
            dict(
                ASSISTANT_TOOL_CALL,
                tool_calls=[{"id": "", "function": {"name": "f", "arguments": "{}"}}],
            )
        ],  # id 为空
        [
            dict(
                ASSISTANT_TOOL_CALL,
                tool_calls=[{"id": "c1", "function": {"name": "f"}}],
            )
        ],  # arguments 缺失
        [{"role": "tool", "content": "x"}],  # 缺 tool_call_id
        [dict(TOOL_RESULT, tool_call_id="")],  # tool_call_id 空
        [dict(TOOL_RESULT, content=None)],  # tool content 非字符串
    ]
    for messages in bad_cases:
        error, tool_history = _validate(messages)
        assert error is not None and error.status_code == 400, messages
        assert tool_history is False, messages


def test_llm_request_message_passthrough() -> None:
    """UT-53-4 透传消息字段白名单：tool 字段仅携带时出现；普通消息 payload 与既往一致。"""
    assistant = dict(ASSISTANT_TOOL_CALL, reasoning_content="需要查天气")
    body = {"messages": [USER_MSG, assistant, TOOL_RESULT]}
    _, payload = _llm_request(LlmModel(id=1, upstream_model_id="m-up"), body, {}, stream=False)
    assert payload["messages"][0] == {"role": "user", "content": "查一下天气"}  # 无多余键
    assert payload["messages"][1]["tool_calls"] == ASSISTANT_TOOL_CALL["tool_calls"]
    assert payload["messages"][1]["reasoning_content"] == "需要查天气"
    assert payload["messages"][2]["tool_call_id"] == "call_1"
    llm_request, _ = _llm_request(LlmModel(id=1, upstream_model_id="m-up"), body, {}, stream=False)
    assert llm_request.messages[1].tool_calls == ASSISTANT_TOOL_CALL["tool_calls"]
    assert llm_request.messages[2].tool_call_id == "call_1"
    assert llm_request.messages[0].content == "查一下天气"
