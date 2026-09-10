"""reasoning_content 消毒（think 标签中和）的单测。"""

from app.gateways.llm_gateway import _sanitize_reasoning


def test_sanitize_reasoning_neutralizes_think_tags() -> None:
    """UT-38-1 think 标签中和：<think>/</think>/带属性/大小写均被拆断，其余文本不变"""
    raw = "前<think>思考</think>中<THINK>x</THINK><think foo=bar>后"
    out = _sanitize_reasoning(raw)
    assert "<think>" not in out.lower()
    assert "</think>" not in out.lower()
    assert out.startswith("前") and out.endswith("后")
    # 标签名前的零宽空格使文本渲染不变但无法被字符串解析命中
    assert out == _sanitize_reasoning(out)  # 幂等


def test_sanitize_reasoning_keeps_plain_text() -> None:
    """UT-38-2 非 think 文本不受影响：单词 think、其它标签、空串"""
    assert _sanitize_reasoning("think 不是标签") == "think 不是标签"
    assert _sanitize_reasoning("<other>x</other>") == "<other>x</other>"
    assert _sanitize_reasoning("") == ""
