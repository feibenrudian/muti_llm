"""AE-25-1..4：Trace 查询 API。"""

import httpx

from tests.e2e_api.test_council import seed_council
from tests.helpers import snapshot_content

QUANTUM = "用一句话解释量子纠缠"


async def _run_council(client: httpx.AsyncClient, key: str, model: str = "council-v1") -> int:
    """跑一次 council（快照回放）并返回 trace_id。"""
    resp = await client.post(
        "/api/admin/playground/run",
        json={"pipeline_name": model, "message": QUANTUM},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status_code"] == 200, payload
    assert payload["response"]["choices"][0]["message"]["content"] == snapshot_content(
        "council_judge"
    )
    return payload["trace_id"]


async def test_filters(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-25-1 筛选：不同 pipeline/status 的记录各维度命中正确。"""
    await seed_council(asgi_client, srs_live_seeded)
    trace_ok = await _run_council(asgi_client, service_key)

    # 制造一条 failed：注入足够多次失败（成员默认各带 1 次重试，2 成员共 4 个请求）
    import httpx as hx

    async with hx.AsyncClient() as srs_client:
        resp = await srs_client.post(
            f"{srs_live_seeded}/_test/config", json={"fail_times": {"deepseek-v4-flash": 10}}
        )
        assert resp.status_code == 200
    resp = await asgi_client.post(
        "/api/admin/playground/run", json={"pipeline_name": "council-v1", "message": QUANTUM}
    )
    assert resp.json()["status_code"] == 502  # 全成员失败
    trace_failed = resp.json()["trace_id"]

    async def query(**params) -> dict:
        resp = await asgi_client.get("/api/admin/traces", params=params)
        assert resp.status_code == 200
        return resp.json()

    result = await query(status="success")
    assert result["total"] == 1 and result["items"][0]["id"] == trace_ok
    result = await query(status="failed")
    assert result["total"] == 1 and result["items"][0]["id"] == trace_failed

    result = await query(pipeline="council-v1")
    assert result["total"] == 2
    result = await query(pipeline="no-such")
    assert result["total"] == 0


async def test_keyword_filter(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-25-2 keyword：只返回原始请求含关键字的记录。"""
    await seed_council(asgi_client, srs_live_seeded)
    await _run_council(asgi_client, service_key)

    resp = await asgi_client.get("/api/admin/traces", params={"keyword": "量子"})
    assert resp.json()["total"] == 1
    resp = await asgi_client.get("/api/admin/traces", params={"keyword": "不存在的关键字xyz"})
    assert resp.json()["total"] == 0


async def test_trace_detail(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-25-3 详情：council trace 的 calls 恰好 N成员+1裁判、顺序正确、字段齐全。"""
    await seed_council(asgi_client, srs_live_seeded)
    trace_id = await _run_council(asgi_client, service_key)

    resp = await asgi_client.get(f"/api/admin/traces/{trace_id}")
    assert resp.status_code == 200
    detail = resp.json()
    request = detail["request"]
    assert request["pipeline_name"] == "council-v1"
    assert request["status"] == "success"
    assert request["request_messages"] == [{"role": "user", "content": QUANTUM}]

    calls = detail["calls"]
    assert [c["role"] for c in calls] == ["member", "member", "judge"]
    judge = calls[-1]
    assert judge["request_payload"]["messages"][0]["content"].startswith("你将看到用户的问题")
    assert judge["duration_ms"] > 0
    assert all(c["upstream_model_id"] == "deepseek-v4-flash" for c in calls)
    # 成员卡片要素齐全（入参/输出/耗时/token）
    member = calls[0]
    assert member["request_payload"]["messages"]
    assert member["response_content"]
    assert member["duration_ms"] > 0
    assert member["prompt_tokens"] > 0

    # model 筛选维度：按成员模型过滤命中
    resp = await asgi_client.get("/api/admin/traces", params={"model": "deepseek-v4-flash"})
    assert resp.json()["total"] == 1


async def test_pagination(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-25-4 分页：造 25 条 → page=2/page_size=20 返回 5 条，meta 正确。"""
    await seed_council(asgi_client, srs_live_seeded)
    for _ in range(25):
        resp = await asgi_client.post(
            "/api/admin/playground/run", json={"pipeline_name": "council-v1", "message": QUANTUM}
        )
        assert resp.json()["status_code"] == 200

    resp = await asgi_client.get("/api/admin/traces", params={"page": 2, "page_size": 20})
    payload = resp.json()
    assert payload["total"] == 25
    assert payload["page"] == 2 and payload["page_size"] == 20
    assert len(payload["items"]) == 5
    # 默认按时间倒序：第 1 页首条应比第 2 页首条新
    first_page = (
        await asgi_client.get("/api/admin/traces", params={"page": 1, "page_size": 20})
    ).json()
    assert first_page["items"][0]["created_at"] >= payload["items"][0]["created_at"]
