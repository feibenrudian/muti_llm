"""AE-41-1..3：用量统计 API（T41 对账看板）+ 流式 usage 落库修复。"""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from app.main import app
from app.orm import ModelCallLog, RequestLog
from app.repos import Repository
from tests.conftest import auth_headers
from tests.e2e_api.test_council import seed_council
from tests.helpers import snapshot_usage

QUANTUM = "用一句话解释量子纠缠"
POEM = "写一首关于秋天的四行短诗，每行不超过10个字"  # passthrough_stream 快照的请求原文


async def _run_council(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/admin/playground/run", json={"pipeline_name": "council-v1", "message": QUANTUM}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status_code"] == 200, resp.json()


async def _db_rows() -> tuple[list[ModelCallLog], list[RequestLog]]:
    async with app.state.session_factory() as session:
        calls = list((await session.execute(select(ModelCallLog))).scalars())
        requests = list(await Repository(session, RequestLog).list())
    return calls, requests


async def test_aggregates_match_call_rows(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-41-1 聚合正确性：四端点合计与 model_call_logs 逐行求和一致；缓存命中来自快照解析。"""
    await seed_council(asgi_client, srs_live_seeded)
    await _run_council(asgi_client)

    calls, requests = await _db_rows()
    assert len(calls) == 4  # 2 成员 + 评论 + 裁判

    by_model = (await asgi_client.get("/api/admin/stats/by-model")).json()
    # 两个配置模型：flash-07 = 成员A+评论+裁判（3 次），flash-09 = 成员B（1 次）
    assert len(by_model) == 2
    row07 = next(r for r in by_model if r["display_name"] == "flash-07")
    assert row07["provider_name"] == "srs-provider"
    assert row07["upstream_model_id"] == "deepseek-v4-flash"
    assert row07["calls"] == 3
    assert row07["failed_calls"] == 0
    assert row07["prompt_tokens"] == 88 + 259 + 477  # passthrough_basic + critique + judge
    for row in by_model:
        assert row["total_tokens"] == row["prompt_tokens"] + row["completion_tokens"]
    assert sum(r["prompt_tokens"] for r in by_model) == sum(c.prompt_tokens for c in calls)
    cached = sum(c.cached_tokens for c in calls)
    assert sum(r["cached_tokens"] for r in by_model) == cached > 0  # 快照携带真实 cached_tokens
    assert sum(r["cache_write_tokens"] for r in by_model) == 0

    by_provider = (await asgi_client.get("/api/admin/stats/by-provider")).json()
    assert len(by_provider) == 1
    assert by_provider[0]["provider_name"] == "srs-provider"
    assert by_provider[0]["calls"] == 4
    assert by_provider[0]["prompt_tokens"] == sum(c.prompt_tokens for c in calls)

    daily = (await asgi_client.get("/api/admin/stats/daily")).json()
    assert len(daily) == 1
    assert daily[0]["calls"] == 4
    assert daily[0]["date"] == calls[0].created_at.date().isoformat()

    overview = (await asgi_client.get("/api/admin/stats/overview")).json()
    assert overview["calls"] == 4
    assert overview["prompt_tokens"] == sum(c.prompt_tokens for c in calls)
    assert overview["total_tokens"] == sum(c.prompt_tokens + c.completion_tokens for c in calls)

    # 裁判行 token 与快照一致：usage 事件解析 → 落库 → 聚合全链路
    judge = next(c for c in calls if c.role == "judge")
    snap = snapshot_usage("council_judge")
    assert judge.prompt_tokens == snap["prompt_tokens"]
    assert judge.cached_tokens == snap["prompt_tokens_details"]["cached_tokens"]

    # 请求级总计 = 各调用行之和
    assert len(requests) == 1
    assert requests[0].total_prompt_tokens == sum(c.prompt_tokens for c in calls)
    assert requests[0].total_completion_tokens == sum(c.completion_tokens for c in calls)


async def test_filters(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-41-2 筛选：provider/model/日期范围各维度命中正确（end 为日期时含当天全天）。"""
    await seed_council(asgi_client, srs_live_seeded)
    await _run_council(asgi_client)

    async def overview(**params: str) -> dict:
        resp = await asgi_client.get("/api/admin/stats/overview", params=params)
        assert resp.status_code == 200
        return resp.json()

    assert (await overview(provider="srs-provider"))["calls"] == 4
    assert (await overview(provider="no-such"))["calls"] == 0
    assert (await overview(model="deepseek-v4-flash"))["calls"] == 4
    assert (await overview(model="other-model"))["calls"] == 0

    today = datetime.now(UTC).date().isoformat()
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    assert (await overview(start=today))["calls"] == 4
    assert (await overview(start=tomorrow))["calls"] == 0
    assert (await overview(end=today))["calls"] == 4
    assert (await overview(start="2000-01-01", end="2000-01-02"))["calls"] == 0

    by_model = (
        await asgi_client.get("/api/admin/stats/by-model", params={"provider": "srs-provider"})
    ).json()
    assert len(by_model) == 2  # 两个配置模型（同上游 ID，不同 model_id）
    daily = (
        await asgi_client.get("/api/admin/stats/daily", params={"model": "no-such"})
    ).json()
    assert daily == []


async def test_stream_usage_recorded(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-41-3 流式 usage 修复：透传流式与 council 流式的调用行/请求级总计 token 齐全。"""
    await seed_council(asgi_client, srs_live_seeded)

    # 透传流式：请求体与 passthrough_stream 快照逐字段一致（回放纪律）
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": POEM}],
            "temperature": 0.7,
            "max_tokens": 2000,
            "stream": True,
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200, resp.text
    assert "data: [DONE]" in resp.text  # 快照未命中时流内失败，不会发 [DONE]
    snap = snapshot_usage("passthrough_stream")
    await asyncio.sleep(0.2)  # 流式 persist 是后台任务，等它落库（对齐 AE-16-4 惯例）
    calls, requests = await _db_rows()
    assert len(calls) == 1
    assert calls[0].role == "passthrough"
    assert calls[0].prompt_tokens == snap["prompt_tokens"] > 0
    assert calls[0].completion_tokens == snap["completion_tokens"] > 0
    assert requests[0].total_prompt_tokens == snap["prompt_tokens"]
    assert requests[0].total_completion_tokens == snap["completion_tokens"]

    # council 流式：裁判行带 usage（修复前记 0），请求级总计 = 各调用行之和
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": "council-v1",
            "messages": [{"role": "user", "content": QUANTUM}],
            "stream": True,
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200, resp.text
    assert "data: [DONE]" in resp.text
    await asyncio.sleep(0.2)
    calls, requests = await _db_rows()
    assert len(calls) == 5  # 透传 1 + council 4
    judge_snap = snapshot_usage("council_judge")
    judge = next(c for c in calls if c.role == "judge")
    assert judge.prompt_tokens == judge_snap["prompt_tokens"]
    assert judge.cached_tokens == judge_snap["prompt_tokens_details"]["cached_tokens"]
    council_calls = [c for c in calls if c.request_id == requests[-1].id]
    assert requests[-1].total_prompt_tokens == sum(c.prompt_tokens for c in council_calls)
    assert requests[-1].total_completion_tokens == sum(c.completion_tokens for c in council_calls)
