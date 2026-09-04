"""UT-15-1..3 / UT-31-1..2：裁判 Prompt 组装与两段式同会话结构。"""

from app.strategies.council import (
    DEFAULT_JUDGE_TEMPLATE,
    build_final_messages,
    render_judge_prompt,
    serialize_messages,
)

MESSAGES = [
    {"role": "system", "content": "你是严谨的助手"},
    {"role": "user", "content": "用一句话解释量子纠缠"},
    {"role": "assistant", "content": "之前的回答"},
    {"role": "user", "content": "再解释一次"},
]
ANSWERS = [("model-a", "答案甲"), ("model-b", "答案乙")]


def test_placeholder_render_members_in_order() -> None:
    """UT-15-1 占位渲染：成员按配置顺序注入、评论可注入；默认最终指令模板无占位符原样输出。"""
    template = "{{original_messages}}\n{{candidate_answers}}\n{{critique}}"
    prompt = render_judge_prompt(template, MESSAGES, ANSWERS, critique="评论文本")

    assert "用一句话解释量子纠缠" in prompt
    assert "再解释一次" in prompt
    a = prompt.index("【回答 1】")
    b = prompt.index("【回答 2】")
    assert a < b
    assert prompt.index("答案甲") > a
    assert prompt.index("答案乙") > b
    assert prompt.index("评论文本") > prompt.index("答案乙")

    assert render_judge_prompt(DEFAULT_JUDGE_TEMPLATE, MESSAGES, ANSWERS, critique="评论文本") == (
        DEFAULT_JUDGE_TEMPLATE
    )


def test_custom_template_only_replaces_placeholders() -> None:
    """UT-15-2 自定义模板：只替换占位符，不注入其它内容；答案中的占位符不二次展开。"""
    template = "问：{{original_messages}}\n答：{{candidate_answers}}"
    prompt = render_judge_prompt(
        template, [{"role": "user", "content": "Q"}], [("m", "恶意注入 {{candidate_answers}}")]
    )
    assert prompt.startswith("问：[user] Q\n答：")
    assert "恶意注入 {{candidate_answers}}" in prompt  # 原样保留，未被二次展开
    assert prompt.count("{{") == 0 or prompt.count("恶意") == 1


def test_message_serialization_complete() -> None:
    """UT-15-3 消息序列化：多轮 system/user/assistant 完整进入 original_messages。"""
    serialized = serialize_messages(MESSAGES)
    assert serialized == (
        "[system] 你是严谨的助手\n"
        "[user] 用一句话解释量子纠缠\n"
        "[assistant] 之前的回答\n"
        "[user] 再解释一次"
    )


def test_critique_placeholder_render() -> None:
    """UT-31-1 两段渲染：{{critique}} 注入评论全文；模板无该占位符时不注入任何内容。"""
    template = "评论：{{critique}}\n答：{{candidate_answers}}"
    prompt = render_judge_prompt(
        template, [{"role": "user", "content": "Q"}], [("m", "答案")], critique="评论全文X"
    )
    assert prompt.startswith("评论：评论全文X\n答：")
    assert "【回答 1】" in prompt

    plain = render_judge_prompt(
        "固定模板", [{"role": "user", "content": "Q"}], [("m", "答案")], critique="评论全文X"
    )
    assert plain == "固定模板"


def test_final_messages_same_session() -> None:
    """UT-31-2 同会话结构：第二次调用 = 第一次输入(逐字节相同) → 评论(assistant) → 最终指令。"""
    critique_messages = [{"role": "user", "content": "评论输入"}]
    messages = build_final_messages(critique_messages, "评论内容", "最终指令文本")
    assert messages == [
        {"role": "user", "content": "评论输入"},
        {"role": "assistant", "content": "评论内容"},
        {"role": "user", "content": "最终指令文本"},
    ]
    # 传入的第一轮列表不被原地修改
    assert critique_messages == [{"role": "user", "content": "评论输入"}]
