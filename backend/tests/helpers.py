"""AE/UT 共享辅助：读取快照期望值、通过管理 API 造种子数据。"""

from pathlib import Path
from typing import Any

import httpx

SNAPSHOTS_DIR = Path(__file__).resolve().parents[0] / "snapshots"

# 旧版默认裁判模板（T31 两段式之前的形态）：存量 Pipeline 的真实配置，
# 用于验证"模板无 {{critique}} 占位符时自动追加评论段"的兜底（录制与 AE 共用，保证逐字节一致）
LEGACY_JUDGE_TEMPLATE = """你将看到用户的问题，以及多个 AI 模型分别给出的回答。
请综合比较这些回答：找出相互印证的关键信息，识别其中的错误或矛盾，然后基于最可靠的信息，给出一个比任何单个回答都更准确、完整的最终回答。

【用户对话】
{{original_messages}}

【各模型回答】
{{candidate_answers}}

请直接输出最终回答，不要复述过程。"""


def load_snapshot(scenario: str) -> dict[str, Any]:
    """按场景名读取已录制的快照（期望值的唯一来源，AGENTS.md：严禁手改内容）。"""
    matches = sorted(SNAPSHOTS_DIR.glob(f"{scenario}__*.json"))
    assert matches, f"snapshot for scenario {scenario!r} not found; run `make record`"
    import json

    return json.loads(matches[0].read_text(encoding="utf-8"))


def snapshot_content(scenario: str) -> str:
    """快照答案全文：兼容流式（stream_chunks 拼 delta）与非流式两种录制形态。"""
    data = load_snapshot(scenario)
    if data.get("stream_chunks"):
        return snapshot_stream_text(scenario)
    return data["non_stream_response"]["choices"][0]["message"]["content"]


def snapshot_stream_text(scenario: str) -> str:
    chunks = load_snapshot(scenario)["stream_chunks"]
    return "".join(
        (c["chunk"].get("choices") or [{}])[0].get("delta", {}).get("content") or "" for c in chunks
    )


def snapshot_usage(scenario: str) -> dict[str, int]:
    """快照 usage：流式取最后一个携带 usage 的 chunk，非流式取响应体。"""
    data = load_snapshot(scenario)
    if data.get("stream_chunks"):
        usage = next(
            (
                c["chunk"]["usage"]
                for c in reversed(data["stream_chunks"])
                if c["chunk"].get("usage")
            ),
            None,
        )
        assert usage is not None, f"scenario {scenario!r} stream snapshot has no usage chunk"
        return usage
    return data["non_stream_response"]["usage"]


def snapshot_tool_calls(scenario: str) -> list[dict[str, Any]]:
    """快照流内 tool_calls 分片按 index 聚合（T44 意图文本化的期望值来源）。"""
    from app.adapters.base import merge_tool_call_deltas

    data = load_snapshot(scenario)
    fragments = [
        frag
        for c in data.get("stream_chunks", [])
        for frag in (
            (c["chunk"].get("choices") or [{}])[0].get("delta", {}).get("tool_calls") or []
        )
    ]
    merged = merge_tool_call_deltas(fragments)
    return [merged[i] for i in sorted(merged)]


async def seed_provider_and_model(
    client: httpx.AsyncClient,
    srs_base_url: str,
    *,
    upstream_model_id: str = "deepseek-v4-flash",
    default_params: dict | None = None,
    provider_name: str = "srs-provider",
) -> tuple[int, int]:
    """通过管理 API 建一个指向 SRS 的 provider + model，返回 (provider_id, model_id)。"""
    resp = await client.post(
        "/api/admin/providers",
        json={
            "name": provider_name,
            "protocol": "openai_compatible",
            "base_url": f"{srs_base_url}/v1",
            "api_key": "sk-srs-test",
        },
    )
    assert resp.status_code == 201, resp.text
    provider_id = resp.json()["id"]

    # 创建 Provider 时会按上游 /models 自动同步：同名模型已存在则复用（按需覆盖参数），避免重复行
    models = (await client.get("/api/admin/models")).json()
    existing = next(
        (
            m
            for m in models
            if m["provider_id"] == provider_id and m["upstream_model_id"] == upstream_model_id
        ),
        None,
    )
    if existing is not None:
        if default_params:
            resp = await client.patch(
                f"/api/admin/models/{existing['id']}", json={"default_params": default_params}
            )
            assert resp.status_code == 200, resp.text
        return provider_id, existing["id"]

    resp = await client.post(
        "/api/admin/models",
        json={
            "provider_id": provider_id,
            "display_name": upstream_model_id,
            "upstream_model_id": upstream_model_id,
            "default_params": default_params or {},
        },
    )
    assert resp.status_code == 201, resp.text
    model_id = resp.json()["id"]
    return provider_id, model_id


# ---- 多用户测试辅助（T47）----------------------------------------------------------


async def create_user(
    client: httpx.AsyncClient,
    *,
    username: str,
    display_name: str = "",
    role: str = "user",
    password: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"username": username, "display_name": display_name, "role": role}
    if password is not None:
        payload["password"] = password
    resp = await client.post("/api/admin/users", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def issue_key(client: httpx.AsyncClient, user_id: int, *, name: str = "") -> dict[str, Any]:
    """签发用户 Key：明文仅出现在本响应（api_key 字段）。"""
    resp = await client.post(f"/api/admin/users/{user_id}/keys", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def snapshot_reasoning(scenario: str) -> str:
    """快照流内 reasoning_content 增量拼接（T53 思考模型回传场景的确定性来源）。"""
    data = load_snapshot(scenario)
    parts = [
        (c["chunk"].get("choices") or [{}])[0].get("delta", {}).get("reasoning_content") or ""
        for c in data.get("stream_chunks", [])
    ]
    return "".join(parts)


def tool_replay_messages() -> list[dict[str, Any]]:
    """T53 工具协议多轮回放消息（录制与 AE 共用，保证逐字节一致）。

    assistant.tool_calls 与 reasoning_content 均取自 passthrough_tool_calls 快照（确定性）：
    DeepSeek 思考模式要求工具调用轮把 reasoning_content 原样回传；tool 结果为固定文本，
    无需真实数据——被测对象是网关对消息形态的放行与字段透传，不是上游答案质量。
    """
    from tests.record_scenarios import TOOL_QUESTION

    calls = [
        {"id": c["id"], "type": "function", "function": c["function"]}
        for c in snapshot_tool_calls("passthrough_tool_calls")
    ]
    return [
        {"role": "user", "content": TOOL_QUESTION},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": calls,
            "reasoning_content": snapshot_reasoning("passthrough_tool_calls"),
        },
        {"role": "tool", "content": "广州 晴，气温 28°C", "tool_call_id": calls[0]["id"]},
    ]


async def login_user(client: httpx.AsyncClient, username: str, password: str) -> dict[str, Any]:
    resp = await client.post(
        "/api/admin/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()
