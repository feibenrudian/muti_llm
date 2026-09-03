"""AE-30-1..5：Trace 换裁判重跑（快照回放；重渲染载荷与原裁判逐字节一致，复用 council_judge 快照）。"""

import asyncio
import json

import httpx

from tests.e2e_api.test_admin_traces import QUANTUM, _run_council
from tests.e2e_api.test_council import seed_council
from tests.helpers import snapshot_content


async def _create_model(client: httpx.AsyncClient, provider_id: int, display: str) -> int:
    """新裁判模型：与成员同 upstream 且无 default_params → 重跑载荷与原裁判逐字节一致。"""
    resp = await client.post(
        "/api/admin/models",
        json={
            "provider_id": provider_id,
            "display_name": display,
            "upstream_model_id": "deepseek-v4-flash",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_rejudge_success_versions_kept(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-30-1 重跑成功：按当前配置重渲染裁判 Prompt，多版本全部保留，原始记录不动。"""
    seeded = await seed_council(asgi_client, srs_live_seeded)
    trace_id = await _run_council(asgi_client, service_key)

    detail_before = (await asgi_client.get(f"/api/admin/traces/{trace_id}")).json()
    original_judge = next(c for c in detail_before["calls"] if c["role"] == "judge")

    m3 = await _create_model(asgi_client, seeded["provider_id"], "flash-rerun")
    resp = await asgi_client.post(
        f"/api/admin/traces/{trace_id}/rejudge", json={"judge_model_id": m3}
    )
    assert resp.status_code == 200, resp.text
    call = resp.json()["call"]
    assert call["role"] == "judge_rerun"
    assert call["model_id"] == m3
    assert call["request_payload"] == original_judge["request_payload"]
    assert call["response_content"] == snapshot_content("council_judge")
    assert call["status"] == "success"
    assert call["duration_ms"] > 0

    m4 = await _create_model(asgi_client, seeded["provider_id"], "flash-rerun-2")
    resp = await asgi_client.post(
        f"/api/admin/traces/{trace_id}/rejudge", json={"judge_model_id": m4}
    )
    assert resp.status_code == 200

    detail_after = (await asgi_client.get(f"/api/admin/traces/{trace_id}")).json()
    assert [c["role"] for c in detail_after["calls"]] == [
        "member",
        "member",
        "judge",
        "judge_rerun",
        "judge_rerun",
    ]
    assert detail_after["request"] == detail_before["request"]


async def test_rejudge_failure_kept(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-30-2 重跑失败：失败尝试也落一行（status=failed），原始记录不动。"""
    seeded = await seed_council(asgi_client, srs_live_seeded)
    trace_id = await _run_council(asgi_client, service_key)

    async with httpx.AsyncClient() as srs_client:
        resp = await srs_client.post(
            f"{srs_live_seeded}/_test/config", json={"fail_times": {"deepseek-v4-flash": 10}}
        )
        assert resp.status_code == 200

    m3 = await _create_model(asgi_client, seeded["provider_id"], "flash-rerun")
    resp = await asgi_client.post(
        f"/api/admin/traces/{trace_id}/rejudge", json={"judge_model_id": m3}
    )
    assert resp.status_code == 200  # 裁判调用失败是业务结果，不是 HTTP 错误
    call = resp.json()["call"]
    assert call["role"] == "judge_rerun"
    assert call["status"] == "failed"
    assert call["error_message"]

    detail = (await asgi_client.get(f"/api/admin/traces/{trace_id}")).json()
    assert [c["role"] for c in detail["calls"]] == ["member", "member", "judge", "judge_rerun"]
    assert detail["request"]["response_content"] == snapshot_content("council_judge")
    assert detail["request"]["status"] == "success"


async def test_rejudge_rejections(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-30-3 拒绝路径：trace 不存在 404；无成员答案/透传 409；模型不可用 422。"""
    seeded = await seed_council(asgi_client, srs_live_seeded)
    trace_id = await _run_council(asgi_client, service_key)

    resp = await asgi_client.post(
        "/api/admin/traces/99999/rejudge", json={"judge_model_id": seeded["m1"]}
    )
    assert resp.status_code == 404

    resp = await asgi_client.post(
        f"/api/admin/traces/{trace_id}/rejudge", json={"judge_model_id": 99999}
    )
    assert resp.status_code == 422

    m3 = await _create_model(asgi_client, seeded["provider_id"], "flash-disabled")
    resp = await asgi_client.patch(f"/api/admin/models/{m3}", json={"enabled": False})
    assert resp.status_code == 200
    resp = await asgi_client.post(
        f"/api/admin/traces/{trace_id}/rejudge", json={"judge_model_id": m3}
    )
    assert resp.status_code == 422

    # 透传 trace（无 Pipeline、无成员答案）：passthrough_basic 快照命中需要 temperature=0.7
    resp = await asgi_client.post(
        "/api/admin/playground/run",
        json={"pipeline_name": "deepseek-v4-flash", "message": QUANTUM, "temperature": 0.7},
    )
    assert resp.json()["status_code"] == 200, resp.text
    passthrough_trace = resp.json()["trace_id"]
    resp = await asgi_client.post(
        f"/api/admin/traces/{passthrough_trace}/rejudge", json={"judge_model_id": seeded["m1"]}
    )
    assert resp.status_code == 409

    # 全成员失败的 trace：无成功成员答案可聚合
    async with httpx.AsyncClient() as srs_client:
        resp = await srs_client.post(
            f"{srs_live_seeded}/_test/config", json={"fail_times": {"deepseek-v4-flash": 10}}
        )
        assert resp.status_code == 200
    resp = await asgi_client.post(
        "/api/admin/playground/run", json={"pipeline_name": "council-v1", "message": QUANTUM}
    )
    assert resp.json()["status_code"] == 502
    failed_trace = resp.json()["trace_id"]
    resp = await asgi_client.post(
        f"/api/admin/traces/{failed_trace}/rejudge", json={"judge_model_id": seeded["m1"]}
    )
    assert resp.status_code == 409


async def _rejudge_stream(
    client: httpx.AsyncClient, trace_id: int, model_id: int
) -> list[dict]:
    """调用流式重跑端点并解析全部 SSE 事件。"""
    async with client.stream(
        "POST", f"/api/admin/traces/{trace_id}/rejudge/stream", json={"judge_model_id": model_id}
    ) as resp:
        assert resp.status_code == 200
        events = []
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        return events


async def test_rejudge_stream_success_and_parallel(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-30-4 流式重跑：meta→delta→done 事件实时下发，两路并行均成功落库，原始记录不动。"""
    seeded = await seed_council(asgi_client, srs_live_seeded)
    trace_id = await _run_council(asgi_client, service_key)
    detail_before = (await asgi_client.get(f"/api/admin/traces/{trace_id}")).json()
    original_judge = next(c for c in detail_before["calls"] if c["role"] == "judge")

    m3 = await _create_model(asgi_client, seeded["provider_id"], "flash-stream-a")
    m4 = await _create_model(asgi_client, seeded["provider_id"], "flash-stream-b")
    events_a, events_b = await asyncio.gather(
        _rejudge_stream(asgi_client, trace_id, m3),
        _rejudge_stream(asgi_client, trace_id, m4),
    )

    for events in (events_a, events_b):
        assert events[0]["type"] == "meta"
        assert events[0]["payload"] == original_judge["request_payload"]
        deltas = "".join(e["text"] for e in events if e["type"] == "delta")
        done = events[-1]
        assert done["type"] == "done"
        assert done["status"] == "success"
        assert done["content"] == deltas == snapshot_content("council_judge")
        assert done["prompt_tokens"] > 0

    detail_after = (await asgi_client.get(f"/api/admin/traces/{trace_id}")).json()
    assert [c["role"] for c in detail_after["calls"]] == [
        "member",
        "member",
        "judge",
        "judge_rerun",
        "judge_rerun",
    ]
    assert detail_after["request"] == detail_before["request"]


async def test_rejudge_stream_failure(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-30-5 流式重跑失败：done.status=failed 携带错误信息，失败行照常落库。"""
    seeded = await seed_council(asgi_client, srs_live_seeded)
    trace_id = await _run_council(asgi_client, service_key)

    async with httpx.AsyncClient() as srs_client:
        resp = await srs_client.post(
            f"{srs_live_seeded}/_test/config", json={"fail_times": {"deepseek-v4-flash": 10}}
        )
        assert resp.status_code == 200

    m3 = await _create_model(asgi_client, seeded["provider_id"], "flash-stream-fail")
    events = await _rejudge_stream(asgi_client, trace_id, m3)

    assert events[0]["type"] == "meta"
    assert not [e for e in events if e["type"] == "delta"]
    done = events[-1]
    assert done["type"] == "done" and done["status"] == "failed"
    assert done["error"] and done["content"] == ""

    detail = (await asgi_client.get(f"/api/admin/traces/{trace_id}")).json()
    reruns = [c for c in detail["calls"] if c["role"] == "judge_rerun"]
    assert len(reruns) == 1
    assert reruns[0]["status"] == "failed" and reruns[0]["error_message"]
