"""UT-15-1..3：裁判 Prompt 组装。"""

from app.strategies.council import (
    DEFAULT_JUDGE_TEMPLATE,
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


def test_default_template_renders_members_in_order() -> None:
    """UT-15-1 默认模板：含原问题、每个成员名与答案、顺序=配置顺序。"""
    prompt = render_judge_prompt(DEFAULT_JUDGE_TEMPLATE, MESSAGES, ANSWERS)

    assert "用一句话解释量子纠缠" in prompt
    assert "再解释一次" in prompt
    # 成员标注与答案齐全，且顺序正确
    a = prompt.index("【回答 1 · model-a】")
    b = prompt.index("【回答 2 · model-b】")
    assert a < b
    assert prompt.index("答案甲") > a
    assert prompt.index("答案乙") > b


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
