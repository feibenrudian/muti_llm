"""T43 工具透传纯函数与适配器聚合：UT-43-1..3。"""

import json

from app.adapters.base import LlmRequest, NormalizedMessage, merge_tool_call_deltas
from app.adapters.openai_compat import OpenAICompatAdapter
from tests.helpers import load_snapshot
from tests.record_scenarios import TOOL_QUESTION, TOOL_WEATHER


def test_merge_tool_call_deltas_by_index() -> None:
    """UT-43-1 分片合并：arguments 按 index 顺序拼接，id/name 取首个，多 index 互不串。"""
    merged = merge_tool_call_deltas(
        [
            {"index": 0, "id": "call_a", "function": {"name": "get_weather", "arguments": "{\"ci"}},
            {"index": 0, "function": {"arguments": "ty\": \"广州\"}"}},
            {"index": 1, "id": "call_b", "function": {"name": "get_time", "arguments": "{}"}},
        ]
    )
    assert merged[0] == {
        "id": "call_a",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "广州"}'},
    }
    assert merged[1]["function"]["name"] == "get_time"


def test_merge_tool_call_deltas_empty() -> None:
    """UT-43-2 空分片 → 空合并结果（调用方据此置 None）。"""
    assert merge_tool_call_deltas([]) == {}


async def test_adapter_complete_aggregates_tool_calls(srs_live_seeded: str) -> None:
    """UT-43-3 适配器聚合：complete() 从流聚出 tool_calls 与 finish_reason（真实快照）。"""
    adapter = OpenAICompatAdapter(base_url=f"{srs_live_seeded}/v1", api_key="sk-srs-test")
    result = await adapter.complete(
        LlmRequest(
            model="deepseek-v4-flash",
            messages=[NormalizedMessage(role="user", content=TOOL_QUESTION)],
            temperature=0.0,
            tools=TOOL_WEATHER,
            tool_choice="auto",
        )
    )
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls is not None
    assert result.tool_calls[0]["function"]["name"] == "get_weather"
    assert json.loads(result.tool_calls[0]["function"]["arguments"])["city"] == "广州"
    assert result.usage.prompt_tokens > 0

    # 与快照期望对账：聚合结果 = 快照流内分片的重放聚合
    data = load_snapshot("passthrough_tool_calls")
    fragments = [
        frag
        for c in data["stream_chunks"]
        for frag in (
            (c["chunk"].get("choices") or [{}])[0].get("delta", {}).get("tool_calls") or []
        )
    ]
    snapshot_merged = merge_tool_call_deltas(fragments)
    assert result.tool_calls == [snapshot_merged[i] for i in sorted(snapshot_merged)]
