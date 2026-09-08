"""UT-35-1：ICE 流式进度注释行格式与 data chunk 序列隔离。"""

import json

from app.gateways.llm_gateway import _sse, ice_progress_comment, make_chunker


def test_progress_comment_format() -> None:
    """UT-35-1 注释行格式：发射内容 `: ice round k/N\n\n`；data chunk 序列生成不受影响。"""
    assert ice_progress_comment(1, 2) == ": ice round 1/2\n\n"
    assert ice_progress_comment(2, 3) == ": ice round 2/3\n\n"
    # SSE 规范注释行：`:` 开头、空行结尾，OpenAI SDK 解析器忽略
    line = ice_progress_comment(1, 3)
    assert line.startswith(":")
    assert line.endswith("\n\n")
    assert not line.startswith("data:")

    chunk = make_chunker("chatcmpl-test", 0, "ice-v1")
    for emitted in (
        _sse(chunk({"role": "assistant"})),
        _sse(chunk({"content": "答案"})),
        _sse(chunk({}, finish_reason="stop")),
    ):
        assert emitted.startswith("data: ")
        json.loads(emitted[len("data: ") :])  # 合法 JSON chunk，注释行不进入 data 序列

    content = json.loads(_sse(chunk({"content": "答案"}))[len("data: ") :])
    assert content["model"] == "ice-v1"
    assert content["choices"][0]["delta"] == {"content": "答案"}
    finish = json.loads(_sse(chunk({}, finish_reason="stop"))[len("data: ") :])
    assert finish["choices"][0]["finish_reason"] == "stop"
