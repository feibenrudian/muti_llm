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
