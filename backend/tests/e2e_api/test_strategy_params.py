"""AE-32-4..6：策略参数管理 API、meta 策略列表、Trace round 字段。"""

import httpx

from tests.helpers import seed_provider_and_model


def _pipeline_payload(model_id: int, **overrides) -> dict:
    payload = {
        "name": "ice-v1",
        "strategy": "ice",
        "judge_model_id": model_id,
        "members": [{"model_id": model_id}],
    }
    payload.update(overrides)
    return payload


async def test_ice_pipeline_strategy_params(
    asgi_client: httpx.AsyncClient, srs_live_seeded: str
) -> None:
    """AE-32-4 管理API：ice 合法参数 201 回显填充值；非法/未知键 422；council 带 params 422。"""
    _, model_id = await seed_provider_and_model(asgi_client, srs_live_seeded)

    resp = await asgi_client.post(
        "/api/admin/pipelines",
        json=_pipeline_payload(model_id, strategy_params={"max_rounds": 2}),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["strategy_params"] == {
        "max_rounds": 2,
        "confidence_threshold": 0.8,
        "stagnation": True,
        "progress_comments": True,
    }

    # 不提供 strategy_params：ice 自动填全默认值
    resp = await asgi_client.post(
        "/api/admin/pipelines", json=_pipeline_payload(model_id, name="ice-defaults")
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["strategy_params"]["max_rounds"] == 3

    # council 缺省回显 {}
    resp = await asgi_client.post(
        "/api/admin/pipelines",
        json=_pipeline_payload(model_id, name="council-plain", strategy="council"),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["strategy_params"] == {}

    # 非法参数 / 未知参数键 / council 带非空参数 → 422
    for bad in (
        _pipeline_payload(model_id, name="ice-bad-range", strategy_params={"max_rounds": 6}),
        _pipeline_payload(model_id, name="ice-bad-key", strategy_params={"rounds": 3}),
        _pipeline_payload(
            model_id, name="council-bad", strategy="council", strategy_params={"max_rounds": 3}
        ),
    ):
        resp = await asgi_client.post("/api/admin/pipelines", json=bad)
        assert resp.status_code == 422, (bad["name"], resp.text)


async def test_meta_strategies(asgi_client: httpx.AsyncClient) -> None:
    """AE-32-5 meta：GET /api/admin/meta/strategies 含 "council" 与 "ice"。"""
    resp = await asgi_client.get("/api/admin/meta/strategies")
    assert resp.status_code == 200
    strategies = resp.json()
    assert "council" in strategies
    assert "ice" in strategies


async def test_trace_detail_round(asgi_client: httpx.AsyncClient) -> None:
    """AE-32-6 Trace API：含 round 的 call 行详情带 round；旧 trace 行 round 为 null。"""
    from app.logging_svc import finish_request, record_call, start_request
    from app.main import app

    factory = app.state.session_factory
    async with factory() as s:
        req = await start_request(
            s,
            client_model_field="ice-v1",
            pipeline_name="ice-v1",
            messages=[{"role": "user", "content": "hi"}],
            params={},
        )
        base = {
            "request_id": req.id,
            "model_id": 1,
            "upstream_model_id": "m-a",
            "provider_name": "p",
            "request_payload": {"model": "m-a"},
        }
        await record_call(s, **base, role="member", round_no=0)
        await record_call(s, **base, role="judge_critique", round_no=0)
        await record_call(s, **base, role="judge")  # 旧行语义：无轮次
        await finish_request(s, req.id, status="success", response_content="ok")
        await s.commit()

    resp = await asgi_client.get(f"/api/admin/traces/{req.id}")
    assert resp.status_code == 200, resp.text
    calls = resp.json()["calls"]
    assert [c["round"] for c in calls] == [0, 0, None]
