"""AE-16-1..5 / AE-17-1..3：council 非流式全链路与流式（快照回放，期望值来自真实 LLM 固化输出）。"""

import asyncio
import json
import time

import httpx

from app.orm import RequestLog
from app.repos import Repository, list_model_calls
from tests.conftest import auth_headers
from tests.helpers import LEGACY_JUDGE_TEMPLATE, snapshot_content, snapshot_stream_text, snapshot_usage

QUANTUM = "用一句话解释量子纠缠"
COUNCIL_BODY = {"model": "council-v1", "messages": [{"role": "user", "content": QUANTUM}]}


async def seed_council(
    client: httpx.AsyncClient,
    srs_base: str,
    *,
    name: str = "council-v1",
    fault_tolerance: dict | None = None,
) -> dict:
    """provider + 2 个模型 + council pipeline（成员A覆盖temp0.7 / 成员B覆盖temp0.9，裁判=m1）。"""
    resp = await client.post(
        "/api/admin/providers",
        json={
            "name": "srs-provider",
            "protocol": "openai_compatible",
            "base_url": f"{srs_base}/v1",
            "api_key": "sk-srs-test",
        },
    )
    provider_id = resp.json()["id"]
    resp = await client.post(
        "/api/admin/models",
        json={
            "provider_id": provider_id,
            "display_name": "flash-07",
            "upstream_model_id": "deepseek-v4-flash",
        },
    )
    m1 = resp.json()["id"]
    resp = await client.post(
        "/api/admin/models",
        json={
            "provider_id": provider_id,
            "display_name": "flash-09",
            "upstream_model_id": "deepseek-v4-flash",
        },
    )
    m2 = resp.json()["id"]

    payload = {
        "name": name,
        "strategy": "council",
        "judge_model_id": m1,
        "members": [
            {"model_id": m1, "param_overrides": {"temperature": 0.7}},
            {"model_id": m2, "param_overrides": {"temperature": 0.9}},
        ],
    }
    if fault_tolerance is not None:
        payload["fault_tolerance"] = fault_tolerance
    resp = await client.post("/api/admin/pipelines", json=payload)
    assert resp.status_code == 201, resp.text
    return {"provider_id": provider_id, "m1": m1, "m2": m2}


async def latest_trace_and_calls() -> tuple[RequestLog, list]:
    from app.main import app

    async with app.state.session_factory() as session:
        requests = await Repository(session, RequestLog).list()
        trace = requests[-1]
        calls = await list_model_calls(session, trace.id)
        return trace, calls


async def srs_recordings(srs_base: str) -> list[dict]:
    """SRS 聊天录像（排除 /v1/models 连通性探测记录，后者无 messages 结构）。"""
    resp = await httpx.AsyncClient().get(f"{srs_base}/_test/requests")
    return [r for r in resp.json()["requests"] if r["model"] != "__models_list__"]


# ---- AE-16 非流式 ------------------------------------------------------------------


async def test_council_end_to_end(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-16-1 端到端：响应 content=裁判快照固化值，model=council-v1，结构兼容 OpenAI。"""
    await seed_council(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["model"] == "council-v1"
    assert data["choices"][0]["message"]["content"] == snapshot_content("council_judge")
    assert data["choices"][0]["finish_reason"] == "stop"


async def test_council_usage_sum(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-16-2 usage 汇总：= 2成员+评论+最终裁判 四次快照 usage 之和（期望值从快照文件精确求和）。"""
    await seed_council(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200

    usages = [
        snapshot_usage("passthrough_basic"),
        snapshot_usage("param_merge_09"),
        snapshot_usage("council_critique"),
        snapshot_usage("council_judge"),
    ]
    expected_prompt = sum(u["prompt_tokens"] for u in usages)
    expected_completion = sum(u["completion_tokens"] for u in usages)

    got = resp.json()["usage"]
    assert got["prompt_tokens"] == expected_prompt
    assert got["completion_tokens"] == expected_completion
    assert got["total_tokens"] == expected_prompt + expected_completion


async def test_judge_receives_member_answers(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-16-3 两段裁判同会话：评论段含成员答案全文；最终段=同一输入+评论(assistant)+最终指令。"""
    await seed_council(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200

    ans_a = snapshot_content("passthrough_basic")
    ans_b = snapshot_content("param_merge_09")
    judge_requests = [
        r["body"]
        for r in await srs_recordings(srs_live_seeded)
        if r["body"]["messages"][0]["content"].startswith("你将看到用户的问题")
    ]
    assert len(judge_requests) == 2  # 评论 + 最终，串行
    critique_messages = judge_requests[0]["messages"]
    final_messages = judge_requests[1]["messages"]

    assert len(critique_messages) == 1
    critique_prompt = critique_messages[0]["content"]
    assert ans_a in critique_prompt and ans_b in critique_prompt
    assert "【回答 1】" in critique_prompt
    assert "【回答 2】" in critique_prompt
    assert QUANTUM in critique_prompt
    assert "【回答评论】" not in critique_prompt

    # 第二次调用与第一次同会话：首条消息逐字节相同，评论作为 assistant 轮，末轮为最终指令
    assert len(final_messages) == 3
    assert final_messages[0]["content"] == critique_prompt
    assert final_messages[1] == {"role": "assistant", "content": snapshot_content("council_critique")}
    assert final_messages[2]["role"] == "user"
    assert "最终回答" in final_messages[2]["content"]


async def test_council_trace_logging(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-16-4 Trace 完整：1 request + 4 calls(2 member + 评论 + 裁判)，裁判入参含组装 Prompt。"""
    await seed_council(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200

    trace, calls = await latest_trace_and_calls()
    assert trace.pipeline_name == "council-v1"
    assert trace.status == "success"
    assert trace.response_content == snapshot_content("council_judge")
    assert trace.total_prompt_tokens > 0

    assert [c.role for c in calls] == ["member", "member", "judge_critique", "judge"]
    members = [c for c in calls if c.role == "member"]
    assert {c.response_content for c in members} == {
        snapshot_content("passthrough_basic"),
        snapshot_content("param_merge_09"),
    }
    assert calls[2].response_content == snapshot_content("council_critique")
    judge = calls[-1]
    assert len(judge.request_payload["messages"]) == 3
    assert judge.request_payload["messages"][0]["content"].startswith("你将看到用户的问题")
    assert snapshot_content("passthrough_basic") in judge.request_payload["messages"][0]["content"]


async def test_two_phase_judge_chain(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-31-1 两段式链路：先评论后终答，评论载荷不含评论段、最终载荷含评论全文，token 四调用求和。"""
    await seed_council(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200

    trace, calls = await latest_trace_and_calls()
    assert [c.role for c in calls] == ["member", "member", "judge_critique", "judge"]
    critique_messages = calls[2].request_payload["messages"]
    final_messages = calls[3].request_payload["messages"]
    assert len(critique_messages) == 1
    assert "【回答评论】" not in critique_messages[0]["content"]
    # 同会话：最终段首条与第一次输入逐字节相同，评论为 assistant 轮，末轮为最终指令
    assert len(final_messages) == 3
    assert final_messages[0]["content"] == critique_messages[0]["content"]
    assert final_messages[1] == {"role": "assistant", "content": snapshot_content("council_critique")}
    assert "最终回答" in final_messages[2]["content"]
    assert calls[2].response_content == snapshot_content("council_critique")
    assert trace.response_content == snapshot_content("council_judge")

    scenarios = ("passthrough_basic", "param_merge_09", "council_critique", "council_judge")
    assert trace.total_prompt_tokens == sum(snapshot_usage(s)["prompt_tokens"] for s in scenarios)
    assert trace.total_completion_tokens == sum(
        snapshot_usage(s)["completion_tokens"] for s in scenarios
    )


async def test_custom_template_shapes_final_instruction(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-31-2 自定义模板：作为最终指令轮渲染；评论始终以 assistant 轮在会话中，不依赖 {{critique}} 占位符。"""
    await seed_council(asgi_client, srs_live_seeded)
    pipeline_id = (await asgi_client.get("/api/admin/pipelines")).json()[0]["id"]
    resp = await asgi_client.patch(
        f"/api/admin/pipelines/{pipeline_id}",
        json={"judge_prompt_template": LEGACY_JUDGE_TEMPLATE},
    )
    assert resp.status_code == 200

    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == snapshot_content("council_judge_custom")

    trace, calls = await latest_trace_and_calls()
    assert [c.role for c in calls] == ["member", "member", "judge_critique", "judge"]
    final_messages = calls[3].request_payload["messages"]
    assert len(final_messages) == 3
    # 第一次输入逐字节相同 + 评论在 assistant 轮（模板没有 {{critique}} 也必然带上）
    assert final_messages[0]["content"] == calls[2].request_payload["messages"][0]["content"]
    assert final_messages[1] == {"role": "assistant", "content": snapshot_content("council_critique")}
    # 旧版模板作为指令轮：重述原对话与答案（其自身渲染结果），无追加段
    assert "{{critique}}" not in LEGACY_JUDGE_TEMPLATE
    assert final_messages[2]["content"].startswith("你将看到用户的问题")
    assert not final_messages[2]["content"].endswith(snapshot_content("council_critique"))


async def test_request_params_override_member_overrides(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-16-5 请求参数透传：请求 temp 覆盖成员覆盖(0.9→0.7)，命中 0.7 成员与单答案裁判快照。"""
    ids = await seed_council(asgi_client, srs_live_seeded)
    # 单成员 pipeline：成员覆盖 temp=0.9，请求传 0.7 → 实发 0.7
    resp = await asgi_client.post(
        "/api/admin/pipelines",
        json={
            "name": "council-single",
            "strategy": "council",
            "judge_model_id": ids["m1"],
            "members": [{"model_id": ids["m2"], "param_overrides": {"temperature": 0.9}}],
        },
    )
    assert resp.status_code == 201

    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": "council-single",
            "messages": [{"role": "user", "content": QUANTUM}],
            "temperature": 0.7,
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200, resp.text
    # 裁判收到单答案 + 请求级 temperature=0.7 → 命中 council_judge_single_07
    assert resp.json()["choices"][0]["message"]["content"] == snapshot_content(
        "council_judge_single_07"
    )

    # SRS 录像确认成员实发 temperature=0.7（请求优先于成员覆盖 0.9）
    recordings = [
        r["body"]
        for r in await srs_recordings(srs_live_seeded)
        if not r["body"]["messages"][0]["content"].startswith("你将看到用户的问题")
    ]
    assert recordings[0]["temperature"] == 0.7


# ---- AE-17 流式 --------------------------------------------------------------------


async def sse_payloads(resp: httpx.Response) -> list[tuple[str, float]]:
    """收集 (payload, 到达时刻)。"""
    out: list[tuple[str, float]] = []
    async for line in resp.aiter_lines():
        if line.startswith("data: "):
            out.append((line[6:], time.monotonic()))
    return out


async def test_council_stream_sse(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-17-1 SSE 端到端：拼接=裁判流式快照全文；末行 [DONE]；chunk model=council-v1。"""
    await seed_council(asgi_client, srs_live_seeded)
    async with asgi_client.stream(
        "POST",
        "/v1/chat/completions",
        json={**COUNCIL_BODY, "stream": True},
        headers=auth_headers(service_key),
    ) as resp:
        assert resp.status_code == 200
        payloads = await sse_payloads(resp)

    assert payloads[-1][0] == "[DONE]"
    chunks = [json.loads(p) for p, _ in payloads[:-1]]
    assert all(c["model"] == "council-v1" for c in chunks)
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == snapshot_stream_text("council_judge")


async def test_council_stream_timing(backend_live: tuple[str, str], srs_live_seeded: str) -> None:
    """AE-17-2 时序：delay_scale 放大 chunk 间隔 → 分多批收到（真流式非缓冲）。

    走真实 TCP（ASGITransport 会一次性收集响应体，无法测时序）。
    两段式裁判 chunk 总量更大，scale 取 5 并放宽客户端超时。
    """
    base_url, service_key = backend_live
    from tests.e2e_api.test_council import seed_council as _seed

    async with httpx.AsyncClient(timeout=180, base_url=base_url) as client:
        await _seed(client, srs_live_seeded)
        resp = await client.post(f"{srs_live_seeded}/_test/config", json={"delay_scale": 5})
        assert resp.status_code == 200

        async with client.stream(
            "POST",
            f"{base_url}/v1/chat/completions",
            json={**COUNCIL_BODY, "stream": True},
            headers=auth_headers(service_key),
        ) as resp:
            assert resp.status_code == 200
            arrivals = await sse_payloads(resp)

    assert len(arrivals) >= 4
    content_times = [t for _, t in arrivals[:-1]]
    gaps = [b - a for a, b in zip(content_times, content_times[1:], strict=False)]
    assert max(gaps) >= 0.1, f"chunks arrived buffered, gaps={gaps}"


async def test_council_stream_logging(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-17-3 流式 Trace：流结束后落完整记录，response_content=拼接结果。"""
    await seed_council(asgi_client, srs_live_seeded)
    async with asgi_client.stream(
        "POST",
        "/v1/chat/completions",
        json={**COUNCIL_BODY, "stream": True},
        headers=auth_headers(service_key),
    ) as resp:
        payloads = await sse_payloads(resp)

    await asyncio.sleep(0.2)
    trace, calls = await latest_trace_and_calls()
    assert trace.status == "success"
    text = "".join(
        json.loads(p)["choices"][0]["delta"].get("content") or "" for p, _ in payloads[:-1]
    )
    assert trace.response_content == text
    assert trace.response_content == snapshot_stream_text("council_judge")
    assert [c.role for c in calls] == ["member", "member", "judge_critique", "judge"]
    assert calls[-1].status == "success"
