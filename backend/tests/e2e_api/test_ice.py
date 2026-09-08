"""AE-34-2..6：ICE 非流式端到端、多轮 Trace、容错注入与换裁判重跑（快照回放）。"""

import httpx

from app.orm import RequestLog
from app.repos import Repository, list_model_calls
from tests.conftest import auth_headers
from tests.helpers import load_snapshot, snapshot_content, snapshot_usage
from tests.record_scenarios import ICE_MAXR_QUESTION, ICE_R0_QUESTION, ICE_REFINE_QUESTION

UPSTREAM_MODEL = "deepseek-v4-flash"


async def seed_ice(
    client: httpx.AsyncClient,
    srs_base: str,
    *,
    name: str = "ice-v1",
    strategy_params: dict | None = None,
    max_concurrency: int | None = None,
    judge_upstream: str = UPSTREAM_MODEL,
) -> dict:
    """provider + 2 个模型 + ice pipeline（成员A覆盖temp0.7 / 成员B覆盖temp0.9，裁判默认=m1）。"""
    resp = await client.post(
        "/api/admin/providers",
        json={
            "name": "srs-provider",
            "protocol": "openai_compatible",
            "base_url": f"{srs_base}/v1",
            "api_key": "sk-srs-test",
        },
    )
    assert resp.status_code == 201, resp.text
    provider_id = resp.json()["id"]
    resp = await client.post(
        "/api/admin/models",
        json={
            "provider_id": provider_id,
            "display_name": "flash-a",
            "upstream_model_id": UPSTREAM_MODEL,
        },
    )
    m1 = resp.json()["id"]
    resp = await client.post(
        "/api/admin/models",
        json={
            "provider_id": provider_id,
            "display_name": "flash-b",
            "upstream_model_id": UPSTREAM_MODEL,
        },
    )
    m2 = resp.json()["id"]

    judge_model_id = m1
    if judge_upstream != UPSTREAM_MODEL:
        resp = await client.post(
            "/api/admin/models",
            json={
                "provider_id": provider_id,
                "display_name": "judge-miss",
                "upstream_model_id": judge_upstream,
            },
        )
        judge_model_id = resp.json()["id"]

    payload: dict = {
        "name": name,
        "strategy": "ice",
        "judge_model_id": judge_model_id,
        "members": [
            {"model_id": m1, "param_overrides": {"temperature": 0.7}},
            {"model_id": m2, "param_overrides": {"temperature": 0.9}},
        ],
    }
    if strategy_params is not None:
        payload["strategy_params"] = strategy_params
    if max_concurrency is not None:
        payload["max_concurrency"] = max_concurrency
    resp = await client.post("/api/admin/pipelines", json=payload)
    assert resp.status_code == 201, resp.text
    return {"provider_id": provider_id, "m1": m1, "m2": m2, "judge": judge_model_id}


async def latest_trace_and_calls() -> tuple[RequestLog, list]:
    from app.main import app

    async with app.state.session_factory() as session:
        requests = await Repository(session, RequestLog).list()
        trace = requests[-1]
        calls = await list_model_calls(session, trace.id)
        return trace, calls


async def srs_recordings(srs_base: str) -> list[dict]:
    resp = await httpx.AsyncClient().get(f"{srs_base}/_test/requests")
    return [r for r in resp.json()["requests"] if r["model"] != "__models_list__"]


async def srs_config(srs_base: str, body: dict) -> None:
    resp = await httpx.AsyncClient().post(f"{srs_base}/_test/config", json=body)
    assert resp.status_code == 200


def _ice_body(question: str) -> dict:
    return {"model": "ice-v1", "messages": [{"role": "user", "content": question}]}


# ---- AE-34-2 端到端（快路径） -------------------------------------------------------


async def test_ice_r0_consensus(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-34-2 端到端（快路径）：content=快照终局答案；usage=(M+2) 快照之和；model=pipeline 名。"""
    await seed_ice(asgi_client, srs_live_seeded)
    await srs_config(srs_live_seeded, {"reset": True})

    resp = await asgi_client.post(
        "/v1/chat/completions", json=_ice_body(ICE_R0_QUESTION), headers=auth_headers(service_key)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model"] == "ice-v1"
    assert data["choices"][0]["message"]["content"] == snapshot_content("ice_r0_consensus_final")
    assert "degraded" not in data

    # 快路径 = M 成员 + 评论 + 终局 = M+2 次调用
    chain = [
        "ice_r0_consensus_m0_r0",
        "ice_r0_consensus_m1_r0",
        "ice_r0_consensus_critique_r0",
        "ice_r0_consensus_final",
    ]
    assert len(await srs_recordings(srs_live_seeded)) == len(chain)
    usages = [snapshot_usage(name) for name in chain]
    got = resp.json()["usage"]
    assert got["prompt_tokens"] == sum(u["prompt_tokens"] for u in usages)
    assert got["completion_tokens"] == sum(u["completion_tokens"] for u in usages)
    assert got["total_tokens"] == got["prompt_tokens"] + got["completion_tokens"]


# ---- AE-34-3 多轮 Trace ------------------------------------------------------------


async def test_ice_refine_trace(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-34-3 多轮 Trace：成员(r0)×2 → 评论(r0) → 成员(r1)×2 → 评论(r1) → 终局(round=null)。

    ice_refine_consensus 场景；每行 payload 与对应快照请求逐字节一致（全文入库）。
    """
    await seed_ice(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json=_ice_body(ICE_REFINE_QUESTION),
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == snapshot_content(
        "ice_refine_consensus_final"
    )

    trace, calls = await latest_trace_and_calls()
    assert trace.pipeline_name == "ice-v1"
    assert trace.status == "success"
    assert trace.response_content == snapshot_content("ice_refine_consensus_final")

    assert [c.role for c in calls] == [
        "member",
        "member",
        "judge_critique",
        "member",
        "member",
        "judge_critique",
        "judge",
    ]
    assert [c.round for c in calls] == [0, 0, 0, 1, 1, 1, None]

    # payload 全文入库：每行 request_payload 与对应快照请求逐字节一致
    chain = [
        "ice_refine_consensus_m0_r0",
        "ice_refine_consensus_m1_r0",
        "ice_refine_consensus_critique_r0",
        "ice_refine_consensus_m0_r1",
        "ice_refine_consensus_m1_r1",
        "ice_refine_consensus_critique_r1",
        "ice_refine_consensus_final",
    ]
    for call, scenario in zip(calls, chain, strict=True):
        snapshot = load_snapshot(scenario)
        assert call.request_payload == snapshot["request"], scenario
        assert call.status == "success"
        expected = snapshot_content(scenario)
        assert call.response_content == expected, scenario

    # 仲裁者单会话贯穿：r1 评论携带 r0 的 user/assistant，终局携带全部评论轮
    crit_r1_messages = calls[5].request_payload["messages"]
    assert len(crit_r1_messages) == 3
    assert crit_r1_messages[1] == {
        "role": "assistant",
        "content": snapshot_content("ice_refine_consensus_critique_r0"),
    }
    final_messages = calls[6].request_payload["messages"]
    assert len(final_messages) == 5


# ---- AE-34-4 轮数耗尽 --------------------------------------------------------------


async def test_ice_max_rounds(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-34-4 轮数耗尽：ice_max_rounds → 行数符合 max_rounds=2；status=success 非 degraded。"""
    await seed_ice(
        asgi_client,
        srs_live_seeded,
        strategy_params={"max_rounds": 2, "confidence_threshold": 1.0},
    )
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json=_ice_body(ICE_MAXR_QUESTION),
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "degraded" not in data
    assert data["choices"][0]["message"]["content"] == snapshot_content("ice_max_rounds_final")

    trace, calls = await latest_trace_and_calls()
    assert trace.status == "success"
    # max_rounds=2：成员轮 0/1 各 2 行 + 评论轮 0/1 + 终局 = 7 行
    assert [c.role for c in calls] == [
        "member",
        "member",
        "judge_critique",
        "member",
        "member",
        "judge_critique",
        "judge",
    ]
    assert [c.round for c in calls] == [0, 0, 0, 1, 1, 1, None]
    assert all(c.status == "success" for c in calls)


# ---- AE-34-5 容错注入 --------------------------------------------------------------


async def test_ice_all_members_failed(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-34-5 全成员失败：→ 502 附各成员摘要；trace failed，成员行全败（裁判未执行）。"""
    await seed_ice(asgi_client, srs_live_seeded)
    await srs_config(srs_live_seeded, {"fail_times": {UPSTREAM_MODEL: 10}})

    resp = await asgi_client.post(
        "/v1/chat/completions", json=_ice_body(ICE_R0_QUESTION), headers=auth_headers(service_key)
    )
    assert resp.status_code == 502
    message = resp.json()["error"]["message"]
    assert "全部成员失败" in message
    assert UPSTREAM_MODEL in message

    trace, calls = await latest_trace_and_calls()
    assert trace.status == "failed"
    assert [c.role for c in calls] == ["member", "member"]
    assert [c.round for c in calls] == [0, 0]
    assert all(c.status == "failed" and c.error_message for c in calls)


async def test_ice_round0_critique_failure_degrades(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-34-5 第 0 轮评论失败：→ 200 degraded:true，content=首个成功成员答案。"""
    await seed_ice(asgi_client, srs_live_seeded, judge_upstream="ice-judge-no-snapshot")

    resp = await asgi_client.post(
        "/v1/chat/completions", json=_ice_body(ICE_R0_QUESTION), headers=auth_headers(service_key)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["degraded"] is True
    assert data["choices"][0]["message"]["content"] == snapshot_content("ice_r0_consensus_m0_r0")

    trace, calls = await latest_trace_and_calls()
    assert trace.status == "degraded"
    assert trace.response_content == snapshot_content("ice_r0_consensus_m0_r0")
    assert [c.role for c in calls] == ["member", "member", "judge_critique"]
    assert [c.round for c in calls] == [0, 0, 0]
    assert calls[-1].status == "failed"
    assert calls[-1].error_message


async def test_ice_round1_member_failure_recovers(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-34-5 第 1 轮成员失败（fail_at_calls）：该成员行 failed，沿用上轮答案，终局正常。"""
    await seed_ice(
        asgi_client,
        srs_live_seeded,
        strategy_params={"max_rounds": 2, "confidence_threshold": 1.0},
        max_concurrency=1,  # 成员串行 → 上游调用序号确定：m0r0,m1r0,crit0,m0r1,m1r1,...
    )
    await srs_config(srs_live_seeded, {"reset": True})
    # 序号 5=m1r1 首次尝试、6=适配器 max_retries=1 的重试，两次都注入才算成员失败
    await srs_config(srs_live_seeded, {"fail_at_calls": {UPSTREAM_MODEL: [5, 6]}})

    resp = await asgi_client.post(
        "/v1/chat/completions", json=_ice_body(ICE_MAXR_QUESTION), headers=auth_headers(service_key)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "degraded" not in data
    assert data["choices"][0]["message"]["content"] == snapshot_content(
        "ice_max_rounds_mbfail_final"
    )

    trace, calls = await latest_trace_and_calls()
    assert trace.status == "success"
    assert [c.role for c in calls] == [
        "member",
        "member",
        "judge_critique",
        "member",
        "member",
        "judge_critique",
        "judge",
    ]
    failed = calls[4]
    assert failed.round == 1
    assert failed.status == "failed"
    assert failed.error_message
    # 失败成员沿用上轮答案：r1 评论与终局载荷 = 录制的容错变体（含 m1 的第 0 轮答案）
    assert calls[5].request_payload == load_snapshot("ice_max_rounds_mbfail_critique_r1")["request"]
    assert calls[6].request_payload == load_snapshot("ice_max_rounds_mbfail_final")["request"]
    assert (
        snapshot_content("ice_max_rounds_m1_r0")
        in calls[5].request_payload["messages"][-1]["content"]
    )


# ---- AE-34-6 换裁判重跑 ------------------------------------------------------------


async def test_ice_rejudge(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-34-6 换裁判重跑：裁判输入=各成员最新成功轮答案；评论段为 council 自由文本模板。

    judge_critique+judge_rerun 成对追加；原始 RequestLog 不变。
    """
    seeded = await seed_ice(asgi_client, srs_live_seeded)
    await srs_config(srs_live_seeded, {"reset": True})
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json=_ice_body(ICE_REFINE_QUESTION),
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200, resp.text
    trace, _ = await latest_trace_and_calls()

    detail_before = (await asgi_client.get(f"/api/admin/traces/{trace.id}")).json()

    # 新裁判模型：同 upstream 且无 default_params → 重跑载荷与录制逐字节一致
    resp = await asgi_client.post(
        "/api/admin/models",
        json={
            "provider_id": seeded["provider_id"],
            "display_name": "flash-rerun",
            "upstream_model_id": UPSTREAM_MODEL,
        },
    )
    m3 = resp.json()["id"]

    resp = await asgi_client.post(
        f"/api/admin/traces/{trace.id}/rejudge", json={"judge_model_id": m3}
    )
    assert resp.status_code == 200, resp.text
    call = resp.json()["call"]
    assert call["role"] == "judge_rerun"
    assert call["model_id"] == m3
    assert call["status"] == "success"
    assert call["response_content"] == snapshot_content("ice_refine_consensus_rejudge_judge")

    # 录像断言：重跑两段请求与录制逐字节一致；评论段为 council 自由文本模板（非共识 JSON）
    recordings = await srs_recordings(srs_live_seeded)
    rejudge_critique_body = recordings[-2]["body"]
    rejudge_judge_body = recordings[-1]["body"]
    assert (
        rejudge_critique_body == load_snapshot("ice_refine_consensus_rejudge_critique")["request"]
    )
    assert rejudge_judge_body == load_snapshot("ice_refine_consensus_rejudge_judge")["request"]

    critique_prompt = rejudge_critique_body["messages"][0]["content"]
    assert snapshot_content("ice_refine_consensus_m0_r1") in critique_prompt
    assert snapshot_content("ice_refine_consensus_m1_r1") in critique_prompt
    assert "请按【回答 1】" in critique_prompt  # DEFAULT_CRITIQUE_TEMPLATE 标志句
    assert '"consensus"' not in critique_prompt  # 不是 ICE 共识 JSON 模板

    # judge_critique + judge_rerun 成对追加；原始 RequestLog 不变
    detail_after = (await asgi_client.get(f"/api/admin/traces/{trace.id}")).json()
    assert [c["role"] for c in detail_after["calls"][-2:]] == ["judge_critique", "judge_rerun"]
    assert detail_after["calls"][:-2] == detail_before["calls"]
    assert detail_after["request"] == detail_before["request"]
