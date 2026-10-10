"""T44 工具聚合模式（council + tool_aggregation 开关）：AE-44-1..5。"""

import asyncio
import json

import httpx

from tests.conftest import auth_headers
from tests.helpers import (
    seed_provider_and_model,
    snapshot_content,
    snapshot_tool_calls,
)
from tests.record_scenarios import TOOL_QUESTION, TOOL_WEATHER

PIPELINE = "tool-council-v1"

TOOL_BODY = {
    "model": PIPELINE,
    "messages": [{"role": "user", "content": TOOL_QUESTION}],
    "tools": TOOL_WEATHER,
    "tool_choice": "auto",
    "temperature": 0.0,  # 请求参数只作用于裁判（council 参数合并规则），与录制场景一致
}


async def _seed_tool_pipeline(
    asgi_client: httpx.AsyncClient, srs: str, *, tool_aggregation: bool
) -> int:
    _, model_id = await seed_provider_and_model(asgi_client, srs)
    resp = await asgi_client.post(
        "/api/admin/pipelines",
        json={
            "name": PIPELINE,
            "strategy": "council",
            "judge_model_id": model_id,
            "members": [
                {"model_id": model_id},  # 无参数 → 上游请求形态 = tool_council_member
                {
                    "model_id": model_id,
                    "param_overrides": {"temperature": 0.0},  # = passthrough_tool_calls
                },
            ],
            "tool_aggregation": tool_aggregation,
        },
    )
    assert resp.status_code == 201, resp.text
    return model_id


async def test_tool_aggregation_non_stream(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-44-1 工具聚合主链路：成员意图 → 评论 → 裁判合成调用，tool_calls 透传客户端并落库。"""
    model_id = await _seed_tool_pipeline(asgi_client, srs_live_seeded, tool_aggregation=True)

    resp = await asgi_client.post(
        "/v1/chat/completions", json=TOOL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    choice = payload["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert "degraded" not in payload  # 合成通过校验，未降级
    tool_calls = choice["message"]["tool_calls"]
    # 与快照期望对账：裁判合成的调用 = 录制时真实产出（确定性）
    assert tool_calls == snapshot_tool_calls("tool_council_final")
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert payload["usage"]["total_tokens"] > 0

    # 落库：2 成员 + 评论 + 裁判 4 行；成员与裁判行均带 response_tool_calls
    from sqlalchemy import select

    from app.main import app
    from app.orm import ModelCallLog, RequestLog

    async with app.state.session_factory() as session:
        trace = (
            (await session.execute(select(RequestLog).order_by(RequestLog.id.desc())))
            .scalars()
            .first()
        )
        logs = (
            (
                await session.execute(
                    select(ModelCallLog)
                    .where(ModelCallLog.request_id == trace.id)
                    .order_by(ModelCallLog.id)
                )
            )
            .scalars()
            .all()
        )
    assert [log.role for log in logs] == ["member", "member", "judge_critique", "judge"]
    assert logs[0].response_tool_calls is not None
    assert logs[3].response_tool_calls == snapshot_tool_calls("tool_council_final")
    assert trace.response_finish_reason == "tool_calls"
    assert trace.pipeline_name == PIPELINE
    _ = model_id


async def test_tool_aggregation_stream(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-44-2 流式工具聚合：终局裁判流的 tool_calls 分片原样下发，finish_reason 透传。"""
    await _seed_tool_pipeline(asgi_client, srs_live_seeded, tool_aggregation=True)
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={**TOOL_BODY, "stream": True},
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200
    deltas: list[dict] = []
    finish_reason = None
    saw_done = False
    async for line in resp.aiter_lines():
        if not line.startswith("data: "):
            continue
        if line == "data: [DONE]":
            saw_done = True
            continue
        chunk = json.loads(line[len("data: ") :])
        choice = (chunk.get("choices") or [{}])[0]
        if choice.get("delta", {}).get("tool_calls"):
            deltas.extend(choice["delta"]["tool_calls"])
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
    assert saw_done and deltas and finish_reason == "tool_calls"


async def test_tool_aggregation_gate(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-44-3/4 闸门：开关关闭 → 400；ICE 策略误开开关 → 400；无 tools 不受影响。"""
    model_id = await _seed_tool_pipeline(asgi_client, srs_live_seeded, tool_aggregation=True)
    pipelines = (await asgi_client.get("/api/admin/pipelines")).json()
    assert pipelines[0]["tool_aggregation"] is True
    pipeline_id = pipelines[0]["id"]

    # 开关关闭 → 恢复 400 明确报错
    resp = await asgi_client.patch(
        f"/api/admin/pipelines/{pipeline_id}", json={"tool_aggregation": False}
    )
    assert resp.status_code == 200
    resp = await asgi_client.post(
        "/v1/chat/completions", json=TOOL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 400
    assert "暂不支持工具调用" in resp.json()["error"]["message"]

    # ICE 策略即使误开开关 → 仍 400（工具聚合仅 council 实现，不静默忽略 tools）
    resp = await asgi_client.post(
        "/api/admin/pipelines",
        json={
            "name": "ice-tool-v1",
            "strategy": "ice",
            "judge_model_id": model_id,
            "strategy_params": {"max_rounds": 1, "confidence_threshold": 1.0},
            "tool_aggregation": True,
            "members": [{"model_id": model_id}],
        },
    )
    assert resp.status_code == 201, resp.text
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={**TOOL_BODY, "model": "ice-tool-v1"},
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 400
    assert "暂不支持工具调用" in resp.json()["error"]["message"]

    # 开关开启、无 tools → 走普通文本聚合（成员温度对齐既有 council 文本快照：0.7/0.9）
    resp = await asgi_client.patch(
        f"/api/admin/pipelines/{pipeline_id}",
        json={
            "members": [
                {"model_id": model_id, "param_overrides": {"temperature": 0.7}},
                {"model_id": model_id, "param_overrides": {"temperature": 0.9}},
            ]
        },
    )
    assert resp.status_code == 200
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": PIPELINE,
            "messages": [{"role": "user", "content": "用一句话解释量子纠缠"}],
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["finish_reason"] == "stop"


async def test_tool_aggregation_stream_process(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-44-5 过程流式 + 工具聚合：成员/评论 reasoning 过程块 → 终局 tool_calls 下发。"""
    await _seed_tool_pipeline(asgi_client, srs_live_seeded, tool_aggregation=True)
    from tests.e2e_api.test_stream_process import _chunks, _collect_payloads, _reasoning_blocks

    async with asgi_client.stream(
        "POST",
        "/v1/chat/completions",
        json={**TOOL_BODY, "stream": True, "stream_process": True},
        headers=auth_headers(service_key),
    ) as resp:
        assert resp.status_code == 200
        payloads = await _collect_payloads(resp)

    chunks, done = _chunks(payloads)
    assert done
    # 过程块：成员 ×2（纯工具调用，仅标题）→ 评论（与非流式工具聚合逐字节一致）
    blocks = _reasoning_blocks(chunks)
    assert [b[0] for b in blocks] == ["【成员 deepseek-v4-flash】\n"] * 2 + ["【评论】\n"]
    assert blocks[2][1] == snapshot_content("tool_council_critique")

    # 终局：tool_calls 分片 + finish_reason=tool_calls；无 content 混入
    deltas: list[dict] = []
    finish_reason = None
    for c in chunks:
        choice = (c.get("choices") or [{}])[0]
        if choice.get("delta", {}).get("tool_calls"):
            deltas.extend(choice["delta"]["tool_calls"])
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
    assert deltas and finish_reason == "tool_calls"
    expected = snapshot_tool_calls("tool_council_final")
    assert [d["function"]["name"] for d in deltas] == [
        call["function"]["name"] for call in expected
    ]
    assert [d["function"]["arguments"] for d in deltas] == [
        call["function"]["arguments"] for call in expected
    ]
    assert all(not c["choices"][0]["delta"].get("content") for c in chunks)

    # 落库与非流式工具聚合一致：4 行 + judge 行带 response_tool_calls + trace 终态
    await asyncio.sleep(0.2)
    from sqlalchemy import select

    from app.main import app
    from app.orm import ModelCallLog, RequestLog

    async with app.state.session_factory() as session:
        trace = (
            (await session.execute(select(RequestLog).order_by(RequestLog.id.desc())))
            .scalars()
            .first()
        )
        logs = (
            (
                await session.execute(
                    select(ModelCallLog)
                    .where(ModelCallLog.request_id == trace.id)
                    .order_by(ModelCallLog.id)
                )
            )
            .scalars()
            .all()
        )
    assert [log.role for log in logs] == ["member", "member", "judge_critique", "judge"]
    assert logs[3].response_tool_calls == snapshot_tool_calls("tool_council_final")
    assert trace.response_finish_reason == "tool_calls"
    assert trace.status == "success"
    assert trace.total_prompt_tokens > 0
