"""AE-18-1..3 + UT-18-1：council 容错矩阵与降级。"""

import asyncio

import httpx

from app.orm import RequestLog
from app.repos import Repository, list_model_calls
from tests.conftest import auth_headers
from tests.helpers import snapshot_content

QUANTUM = "用一句话解释量子纠缠"
COUNCIL_BODY = {"model": "council-v1", "messages": [{"role": "user", "content": QUANTUM}]}


async def _seed_council(client: httpx.AsyncClient, srs_base: str, **kwargs) -> dict:
    from tests.e2e_api.test_council import seed_council

    return await seed_council(client, srs_base, **kwargs)


async def _seed_anthropic_judge_pipeline(
    client: httpx.AsyncClient, srs_base: str, anthropic_base: str, *, strict_judge: bool
) -> None:
    """成员走 SRS，裁判走 anthropic mock（独立失败注入通道）。"""
    await _seed_council(client, srs_base, name="council-v1")
    resp = await client.post(
        "/api/admin/providers",
        json={
            "name": "anthropic-mock",
            "protocol": "anthropic",
            "base_url": anthropic_base,
            "api_key": "sk-ant-test",
        },
    )
    assert resp.status_code == 201
    provider_id = resp.json()["id"]
    resp = await client.post(
        "/api/admin/models",
        json={
            "provider_id": provider_id,
            "display_name": "claude",
            "upstream_model_id": "claude-sonnet-4",
        },
    )
    judge_model_id = resp.json()["id"]

    fault_tolerance = {"judge_failure": "strict"} if strict_judge else {"judge_failure": "degrade"}
    resp = await client.patch(
        f"/api/admin/pipelines/{(await client.get('/api/admin/pipelines')).json()[0]['id']}",
        json={"judge_model_id": judge_model_id, "fault_tolerance": fault_tolerance},
    )
    assert resp.status_code == 200, resp.text


async def latest_trace_and_calls() -> tuple[RequestLog, list]:
    from app.main import app

    async with app.state.session_factory() as session:
        requests = await Repository(session, RequestLog).list()
        trace = requests[-1]
        calls = await list_model_calls(session, trace.id)
        return trace, calls


async def test_all_members_failed(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-18-1 全部成员失败：502，error 含成员名；日志 failed 且 member calls 全败。"""
    await _seed_council(asgi_client, srs_live_seeded)
    resp = await httpx.AsyncClient().post(
        f"{srs_live_seeded}/_test/config", json={"fail_times": {"deepseek-v4-flash": 5}}
    )
    assert resp.status_code == 200

    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 502
    message = resp.json()["error"]["message"]
    assert "全部成员失败" in message
    assert "deepseek-v4-flash" in message

    trace, calls = await latest_trace_and_calls()
    assert trace.status == "failed"
    assert [c.role for c in calls] == ["member", "member"]  # 裁判未执行
    assert all(c.status == "failed" and c.error_message for c in calls)


async def test_judge_failure_degrades(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str, anthropic_mock: str
) -> None:
    """AE-18-2 裁判失败降级：200，content=首个成功成员答案，degraded:true，日志 degraded。"""
    await _seed_anthropic_judge_pipeline(
        asgi_client, srs_live_seeded, anthropic_mock, strict_judge=False
    )
    resp = await httpx.AsyncClient().post(f"{anthropic_mock}/_test/config", json={"fail_times": 3})
    assert resp.status_code == 200

    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["degraded"] is True
    assert data["choices"][0]["message"]["content"] == snapshot_content("passthrough_basic")

    trace, calls = await latest_trace_and_calls()
    assert trace.status == "degraded"
    assert trace.response_content == snapshot_content("passthrough_basic")
    assert [c.role for c in calls] == ["member", "member", "judge"]
    assert calls[-1].status == "failed"
    assert calls[-1].error_message


async def test_judge_failure_strict(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str, anthropic_mock: str
) -> None:
    """AE-18-3 降级关闭：judge_failure=strict → 裁判失败时 502 而非降级。"""
    await _seed_anthropic_judge_pipeline(
        asgi_client, srs_live_seeded, anthropic_mock, strict_judge=True
    )
    resp = await httpx.AsyncClient().post(f"{anthropic_mock}/_test/config", json={"fail_times": 3})
    assert resp.status_code == 200

    resp = await asgi_client.post(
        "/v1/chat/completions", json=COUNCIL_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 502
    assert "裁判调用失败" in resp.json()["error"]["message"]

    trace, calls = await latest_trace_and_calls()
    assert trace.status == "failed"
    assert [c.role for c in calls] == ["member", "member", "judge"]
    assert calls[-1].status == "failed"


async def test_stream_client_cancel_propagates(
    backend_live: tuple[str, str], srs_live_seeded: str
) -> None:
    """UT-18-1 取消传播：流式断开后成员 success、judge call 与请求终态 client_cancelled。"""
    base_url, service_key = backend_live
    async with httpx.AsyncClient(timeout=60, base_url=base_url) as client:
        await _seed_council(client, srs_live_seeded)

        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={**COUNCIL_BODY, "stream": True},
            headers=auth_headers(service_key),
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    break  # 读完首个 chunk 即断开

    await asyncio.sleep(0.5)
    from app.main import app

    async with app.state.session_factory() as session:
        requests = await Repository(session, RequestLog).list()
        trace = requests[-1]
        calls = await list_model_calls(session, trace.id)

    assert trace.status == "client_cancelled"
    assert [c.role for c in calls] == ["member", "member", "judge"]
    assert all(c.status == "success" for c in calls[:2])  # 成员阶段已完成
    assert calls[-1].status == "client_cancelled"  # 裁判流被取消
