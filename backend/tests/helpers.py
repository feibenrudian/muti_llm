"""AE/UT 共享辅助：读取快照期望值、通过管理 API 造种子数据。"""

from pathlib import Path
from typing import Any

import httpx

SNAPSHOTS_DIR = Path(__file__).resolve().parents[0] / "snapshots"


def load_snapshot(scenario: str) -> dict[str, Any]:
    """按场景名读取已录制的快照（期望值的唯一来源，AGENTS.md：严禁手改内容）。"""
    matches = sorted(SNAPSHOTS_DIR.glob(f"{scenario}__*.json"))
    assert matches, f"snapshot for scenario {scenario!r} not found; run `make record`"
    import json

    return json.loads(matches[0].read_text(encoding="utf-8"))


def snapshot_content(scenario: str) -> str:
    return load_snapshot(scenario)["non_stream_response"]["choices"][0]["message"]["content"]


def snapshot_stream_text(scenario: str) -> str:
    chunks = load_snapshot(scenario)["stream_chunks"]
    return "".join(
        (c["chunk"].get("choices") or [{}])[0].get("delta", {}).get("content") or "" for c in chunks
    )


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
