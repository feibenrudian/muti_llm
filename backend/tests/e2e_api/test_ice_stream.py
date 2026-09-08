"""AE-35-2..7：ICE 流式端到端、进度注释行、council 回归、开流前失败、迭代期取消（快照回放）。"""

import asyncio
import json

import httpx

from app.orm import RequestLog
from app.repos import Repository, list_model_calls
from tests.conftest import auth_headers
from tests.e2e_api.test_council import COUNCIL_BODY, seed_council
from tests.e2e_api.test_ice import seed_ice, srs_config
from tests.helpers import snapshot_stream_text
from tests.record_scenarios import ICE_MAXR_QUESTION, ICE_REFINE_QUESTION


def _ice_stream_body(question: str) -> dict:
    return {
        "model": "ice-v1",
        "messages": [{"role": "user", "content": question}],
        "stream": True,
    }


async def _collect_lines(resp: httpx.Response) -> list[str]:
    return [line async for line in resp.aiter_lines() if line]


def _data_chunks(lines: list[str]) -> tuple[list[dict], bool]:
    """从 SSE 行序列解析 data chunk（忽略注释行），返回 (chunks, 是否以 [DONE] 结尾)。"""
    data = [line[len("data: ") :] for line in lines if line.startswith("data: ")]
    done = bool(data) and data[-1] == "[DONE]"
    return [json.loads(p) for p in data[:-1] if done or p != "[DONE]"], done


async def test_ice_stream_e2e(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-35-2 流式端到端：data chunk 拼接=快照终局答案；末行 [DONE]；chunk model=pipeline 名。"""
    await seed_ice(asgi_client, srs_live_seeded)
    await srs_config(srs_live_seeded, {"reset": True})

    async with asgi_client.stream(
        "POST",
        "/v1/chat/completions",
        json=_ice_stream_body(ICE_REFINE_QUESTION),
        headers=auth_headers(service_key),
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = await _collect_lines(resp)

    chunks, done = _data_chunks(lines)
    assert done
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert all(c["model"] == "ice-v1" for c in chunks)
    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == snapshot_stream_text("ice_refine_consensus_final")


async def test_ice_stream_progress_comments(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-35-3 注释行存在且无害：输出含 `: ice round 1/2`；OpenAI 兼容 chunk 序列仍完整可解析。"""
    await seed_ice(asgi_client, srs_live_seeded, strategy_params={"max_rounds": 2})

    async with asgi_client.stream(
        "POST",
        "/v1/chat/completions",
        json=_ice_stream_body(ICE_REFINE_QUESTION),
        headers=auth_headers(service_key),
    ) as resp:
        assert resp.status_code == 200
        lines = await _collect_lines(resp)

    comments = [line for line in lines if line.startswith(":")]
    assert ": ice round 1/2" in comments

    chunks, done = _data_chunks(lines)
    assert done
    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == snapshot_stream_text("ice_refine_consensus_final")


async def test_ice_stream_progress_comments_disabled(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-35-4 注释可关：progress_comments=false → 全程无 `:` 开头行。"""
    await seed_ice(
        asgi_client,
        srs_live_seeded,
        strategy_params={"max_rounds": 2, "progress_comments": False},
    )

    async with asgi_client.stream(
        "POST",
        "/v1/chat/completions",
        json=_ice_stream_body(ICE_REFINE_QUESTION),
        headers=auth_headers(service_key),
    ) as resp:
        assert resp.status_code == 200
        lines = await _collect_lines(resp)

    assert not any(line.startswith(":") for line in lines)
    chunks, done = _data_chunks(lines)
    assert done
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == snapshot_stream_text("ice_refine_consensus_final")


async def test_council_stream_no_comment_lines(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-35-5 council 回归：council 流式输出无注释行（AE-17-1..3 行为不变，由其自身用例覆盖）。"""
    await seed_council(asgi_client, srs_live_seeded)

    async with asgi_client.stream(
        "POST",
        "/v1/chat/completions",
        json={**COUNCIL_BODY, "stream": True},
        headers=auth_headers(service_key),
    ) as resp:
        assert resp.status_code == 200
        lines = await _collect_lines(resp)

    assert not any(line.startswith(":") for line in lines)
    chunks, done = _data_chunks(lines)
    assert done
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == snapshot_stream_text("council_judge")


async def test_ice_stream_round0_critique_failure(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-35-6 开流前失败：第 0 轮评论失败（流式）→ 返回错误 JSON 非 200（同 council 语义）。"""
    await seed_ice(asgi_client, srs_live_seeded, judge_upstream="ice-judge-no-snapshot")

    resp = await asgi_client.post(
        "/v1/chat/completions",
        json=_ice_stream_body(ICE_REFINE_QUESTION),
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 502
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]["message"]


async def test_ice_stream_iteration_cancel(
    backend_live: tuple[str, str], srs_live_seeded: str
) -> None:
    """AE-35-7 迭代期取消：客户端断开 → 在飞轮次任务取消、client_cancelled；已完成轮次行已落库。

    走真实 TCP（ASGITransport 不传播断开，见 conftest.backend_live 注释）。
    delay_scale=0.2：第 0 轮约 3.5s 完成并发出首条注释行，第 1 轮成员约 2.5s——
    客户端收到首条注释行即断开，必然落在迭代期。
    """
    base_url, service_key = backend_live
    async with httpx.AsyncClient(timeout=120, base_url=base_url) as client:
        await seed_ice(
            client,
            srs_live_seeded,
            strategy_params={"max_rounds": 2, "confidence_threshold": 1.0},
        )
        await srs_config(srs_live_seeded, {"reset": True})
        await srs_config(srs_live_seeded, {"delay_scale": 0.2})

        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json=_ice_stream_body(ICE_MAXR_QUESTION),
            headers=auth_headers(service_key),
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith(":"):
                    break  # 收到首条注释行（进入第 1 轮迭代）即断开

    # 给取消传播与后台持久化留时间
    await asyncio.sleep(1.0)

    from app.main import app as backend_app

    async with backend_app.state.session_factory() as session:
        requests = await Repository(session, RequestLog).list()
        trace = requests[-1]
        assert trace.status == "client_cancelled"
        calls = await list_model_calls(session, trace.id)

    # 开流前完成的第 0 轮行照常落库；终局未开始（无 judge 行）
    assert [c.role for c in calls] == ["member", "member", "judge_critique"]
    assert [c.round for c in calls] == [0, 0, 0]
    assert all(c.status == "success" for c in calls)
