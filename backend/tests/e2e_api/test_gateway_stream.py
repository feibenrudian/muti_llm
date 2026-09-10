"""AE-12-1..4：透传流式 SSE + 客户端取消/断连续跑。"""

import asyncio
import json
import time

import httpx
import pytest

from app.orm import RequestLog
from app.repos import Repository
from tests.conftest import auth_headers
from tests.helpers import seed_provider_and_model, snapshot_stream_text

POEM = "写一首关于秋天的四行短诗，每行不超过10个字"


def stream_body() -> dict:
    return {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": POEM}],
        "temperature": 0.7,
        "max_tokens": 2000,
        "stream": True,
    }


async def _latest_trace() -> RequestLog:
    from app.main import app

    async with app.state.session_factory() as session:
        requests = await Repository(session, RequestLog).list()
        return requests[-1]


async def _sse_payloads(resp: httpx.Response) -> list[str]:
    payloads: list[str] = []
    async for line in resp.aiter_lines():
        if line.startswith("data: "):
            payloads.append(line[len("data: ") :])
    return payloads


async def test_sse_format(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-12-1 SSE 格式：chunk 拼接=完整答案；每行 data: {...}；末行 [DONE]。"""
    await seed_provider_and_model(asgi_client, srs_live_seeded)
    async with asgi_client.stream(
        "POST", "/v1/chat/completions", json=stream_body(), headers=auth_headers(service_key)
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        payloads = await _sse_payloads(resp)

    assert payloads[-1] == "[DONE]"
    chunks = [json.loads(p) for p in payloads[:-1]]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert text == snapshot_stream_text("passthrough_stream")


async def test_stream_logging(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-12-2 流式也落库：读完整流后 request_logs 落最终全文。"""
    await seed_provider_and_model(asgi_client, srs_live_seeded)
    async with asgi_client.stream(
        "POST", "/v1/chat/completions", json=stream_body(), headers=auth_headers(service_key)
    ) as resp:
        payloads = await _sse_payloads(resp)

    # background task 在响应结束后执行，稍等确保落库
    await asyncio.sleep(0.2)
    trace = await _latest_trace()
    assert trace.status == "success"
    text = "".join(json.loads(p)["choices"][0]["delta"].get("content") or "" for p in payloads[:-1])
    assert trace.response_content == text
    assert trace.response_content == snapshot_stream_text("passthrough_stream")

    detail = (await asgi_client.get(f"/api/admin/traces/{trace.id}")).json()
    request = detail["request"]
    assert request["total_duration_ms"] >= request["first_token_ms"] > 0
    # 请求级总耗时从请求入口起算，必然覆盖任一调用行的上游耗时
    assert request["total_duration_ms"] >= detail["calls"][0]["duration_ms"]


async def _wait_terminal_trace(timeout: float = 15.0) -> RequestLog:
    """轮询直到最新 trace 落终态（detached 续跑是后台完成，不能用固定 sleep）。"""
    from app.main import app as backend_app

    deadline = time.monotonic() + timeout
    while True:
        async with backend_app.state.session_factory() as session:
            requests = await Repository(session, RequestLog).list()
            if requests and requests[-1].status != "pending":
                return requests[-1]
        assert time.monotonic() < deadline, "trace 未在超时内落终态"
        await asyncio.sleep(0.1)


async def test_client_cancel(
    backend_live: tuple[str, str], srs_live_seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AE-12-3 取消：读首 chunk 后断开 → 日志 status=client_cancelled。

    走真实 TCP（ASGITransport 不传播断开，见 conftest.backend_live 注释）。
    本用例验证取消语义，显式关闭断连续跑（detach_on_disconnect=False）。
    """
    from app import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "detach_on_disconnect", False)
    base_url, service_key = backend_live
    async with httpx.AsyncClient(timeout=30) as client:
        # 种子：provider/model 指向 SRS
        resp = await client.post(
            f"{base_url}/api/admin/providers",
            json={
                "name": "srs-provider",
                "protocol": "openai_compatible",
                "base_url": f"{srs_live_seeded}/v1",
                "api_key": "sk-srs-test",
            },
        )
        assert resp.status_code == 201
        provider_id = resp.json()["id"]
        resp = await client.post(
            f"{base_url}/api/admin/models",
            json={
                "provider_id": provider_id,
                "display_name": "deepseek-v4-flash",
                "upstream_model_id": "deepseek-v4-flash",
            },
        )
        assert resp.status_code == 201

        async with client.stream(
            "POST",
            f"{base_url}/v1/chat/completions",
            json=stream_body(),
            headers=auth_headers(service_key),
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    break  # 读完首个 chunk 即断开

    # 给取消传播与后台持久化留时间
    await asyncio.sleep(0.5)

    from app.main import app as backend_app

    async with backend_app.state.session_factory() as session:
        requests = await Repository(session, RequestLog).list()
        assert requests, "no request logged"
        assert requests[-1].status == "client_cancelled"


async def test_client_cancel_detached(
    backend_live: tuple[str, str], srs_live_seeded: str
) -> None:
    """AE-12-4 断连续跑：detach 默认开，断开后上游跑完，trace=success 且全文落库。"""
    base_url, service_key = backend_live
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url}/api/admin/providers",
            json={
                "name": "srs-provider",
                "protocol": "openai_compatible",
                "base_url": f"{srs_live_seeded}/v1",
                "api_key": "sk-srs-test",
            },
        )
        assert resp.status_code == 201
        provider_id = resp.json()["id"]
        resp = await client.post(
            f"{base_url}/api/admin/models",
            json={
                "provider_id": provider_id,
                "display_name": "deepseek-v4-flash",
                "upstream_model_id": "deepseek-v4-flash",
            },
        )
        assert resp.status_code == 201

        async with client.stream(
            "POST",
            f"{base_url}/v1/chat/completions",
            json=stream_body(),
            headers=auth_headers(service_key),
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    break  # 读完首个 chunk 即断开（与 AE-12-3 同一 payload，快照回放）

    trace = await _wait_terminal_trace()
    assert trace.status == "success"
    assert trace.response_content == snapshot_stream_text("passthrough_stream")
