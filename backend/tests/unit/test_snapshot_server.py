"""UT-03-1..6：SRS 自身行为。所有用例使用本地构造的快照，不依赖真实录制数据。"""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from tests.snapshot_server.app import create_app
from tests.snapshot_server.store import Snapshot

# ---- 已知 fixture 数据（确定性） -------------------------------------------------

REQUEST_NS = {
    "model": "m1",
    "messages": [{"role": "user", "content": "hello"}],
    "temperature": 0.5,
    "stream": False,
}
RESPONSE_NS = {
    "id": "chatcmpl-x",
    "object": "chat.completion",
    "model": "m1",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "HELLO_SNAPSHOT"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}
REQUEST_ST = {**REQUEST_NS, "stream": True}
CHUNKS = [
    {
        "chunk": {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": "m1",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "HEL"},
                    "finish_reason": None,
                }
            ],
        },
        "delay_ms": 30,
    },
    {
        "chunk": {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": "m1",
            "choices": [{"index": 0, "delta": {"content": "LO_SNAPSHOT"}, "finish_reason": None}],
        },
        "delay_ms": 20,
    },
]


@pytest.fixture
def seeded_srs_app(srs_app: FastAPI) -> FastAPI:
    """replay 模式 SRS，预置非流式 + 流式两个已知快照。"""
    srs_app.state.srs.store.save(
        Snapshot(scenario="m1", request=REQUEST_NS, non_stream_response=RESPONSE_NS)
    )
    srs_app.state.srs.store.save(Snapshot(scenario="m1", request=REQUEST_ST, stream_chunks=CHUNKS))
    return srs_app


@pytest.fixture
async def seeded_client(seeded_srs_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=seeded_srs_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://srs.test") as client:
        yield client


async def _sse_payloads(resp: httpx.Response) -> list[str]:
    payloads: list[str] = []
    async for line in resp.aiter_lines():
        if line.startswith("data: "):
            payloads.append(line[len("data: ") :])
    return payloads


# ---- 用例 ------------------------------------------------------------------------


async def test_snapshot_hit_non_stream(seeded_client: httpx.AsyncClient) -> None:
    """UT-03-1 快照命中(非流式)：以快照库中已有请求调用，返回内容与快照逐字段一致。"""
    resp = await seeded_client.post("/v1/chat/completions", json=REQUEST_NS)
    assert resp.status_code == 200
    assert resp.json() == RESPONSE_NS


async def test_snapshot_hit_stream(seeded_client: httpx.AsyncClient) -> None:
    """UT-03-2 快照命中(流式)：按快照 chunk 序列返回，拼接=快照全文，末行 [DONE]。"""
    await seeded_client.post("/_test/config", json={"delay_scale": 0})
    async with seeded_client.stream("POST", "/v1/chat/completions", json=REQUEST_ST) as resp:
        assert resp.status_code == 200
        payloads = await _sse_payloads(resp)
    assert payloads[-1] == "[DONE]"
    chunks = [json.loads(p) for p in payloads[:-1]]
    assert chunks == [entry["chunk"] for entry in CHUNKS]
    text = "".join(c["choices"][0]["delta"]["content"] for c in chunks)
    assert text == "HELLO_SNAPSHOT"


async def test_snapshot_miss_diagnostics(seeded_client: httpx.AsyncClient) -> None:
    """UT-03-3 未命中诊断：库外请求 → 500，错误含请求 hash 与最接近候选列表。"""
    unknown = {"model": "m1", "messages": [{"role": "user", "content": "unknown"}], "stream": False}
    resp = await seeded_client.post("/v1/chat/completions", json=unknown)
    assert resp.status_code == 500
    error = resp.json()["error"]
    assert error["type"] == "snapshot_miss"
    assert error["request_hash"].startswith("sha256:")
    assert "request_hash=sha256:" in error["message"]
    assert len(error["nearest"]) >= 1  # 库非空时应给出候选


async def test_fail_injection_overrides_snapshot(seeded_client: httpx.AsyncClient) -> None:
    """UT-03-4 失败注入优先：fail_times=2 → 前两次 500，第三次起恢复快照回放。"""
    await seeded_client.post("/_test/config", json={"fail_times": {"m1": 2}})
    for _ in range(2):
        resp = await seeded_client.post("/v1/chat/completions", json=REQUEST_NS)
        assert resp.status_code == 500
        assert resp.json()["error"]["type"] == "injected_failure"
    resp = await seeded_client.post("/v1/chat/completions", json=REQUEST_NS)
    assert resp.status_code == 200
    assert resp.json() == RESPONSE_NS


async def test_fail_at_calls_injection(seeded_client: httpx.AsyncClient) -> None:
    """UT-34-1 SRS 按序号注入：fail_at_calls {"m1": [2]} → 第1次成功、第2次500、第3次恢复。

    reset 清空。叠加规则：fail_times 先拦（全局序号照常递增），fail_at_calls 按 1 起序号命中即 500。
    """
    resp = await seeded_client.post("/_test/config", json={"fail_at_calls": {"m1": [2]}})
    assert resp.status_code == 200
    assert resp.json()["fail_at_calls"] == {"m1": [2]}

    resp = await seeded_client.post("/v1/chat/completions", json=REQUEST_NS)
    assert resp.status_code == 200
    resp = await seeded_client.post("/v1/chat/completions", json=REQUEST_NS)
    assert resp.status_code == 500
    assert resp.json()["error"]["type"] == "injected_failure"
    assert "call #2" in resp.json()["error"]["message"]
    for _ in range(2):
        resp = await seeded_client.post("/v1/chat/completions", json=REQUEST_NS)
        assert resp.status_code == 200

    # reset 清空 fail_at_calls 与序号计数
    resp = await seeded_client.post("/_test/config", json={"reset": True})
    assert resp.json()["fail_at_calls"] == {}

    await seeded_client.post(
        "/_test/config", json={"fail_times": {"m1": 1}, "fail_at_calls": {"m1": [3]}}
    )
    statuses = [
        (await seeded_client.post("/v1/chat/completions", json=REQUEST_NS)).status_code
        for _ in range(4)
    ]
    assert statuses == [500, 200, 500, 200]


async def test_request_recording(seeded_client: httpx.AsyncClient) -> None:
    """UT-03-5 请求录像：发送请求后 /_test/requests 能查到完整 messages/参数原文。"""
    await seeded_client.post("/v1/chat/completions", json=REQUEST_NS)
    resp = await seeded_client.get("/_test/requests", params={"model": "m1"})
    assert resp.status_code == 200
    requests = resp.json()["requests"]
    assert len(requests) == 1
    assert requests[0]["body"] == REQUEST_NS


async def test_record_roundtrip(snapshots_dir: Path) -> None:
    """UT-03-6 录制往返：record 模式打桩上游(非流式+流式) → 快照落盘字段完整 → replay 立即命中。"""
    response_ns = {**RESPONSE_NS, "id": "chatcmpl-recorded", "model": "m2"}
    chunk_a = {
        "id": "r1",
        "object": "chat.completion.chunk",
        "model": "m2",
        "choices": [{"index": 0, "delta": {"content": "REC"}, "finish_reason": None}],
    }
    chunk_b = {
        "id": "r1",
        "object": "chat.completion.chunk",
        "model": "m2",
        "choices": [{"index": 0, "delta": {"content": "_OK"}, "finish_reason": None}],
    }
    chunk_done = {
        "id": "r1",
        "object": "chat.completion.chunk",
        "model": "m2",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }

    async def sse_body() -> AsyncIterator[bytes]:
        for obj in (chunk_a, chunk_b, chunk_done):
            yield f"data: {json.dumps(obj)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/completions"
        if json.loads(request.content)["stream"]:
            return httpx.Response(
                200, content=sse_body(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=response_ns)

    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://upstream.test"
    )

    record_app = create_app(
        snapshots_dir, mode="record", record_base_url="http://upstream.test", record_client=upstream
    )
    transport = httpx.ASGITransport(app=record_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://srs.test") as rc:
        request_ns = {
            "model": "m2",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        resp = await rc.post("/v1/chat/completions", json=request_ns)
        assert resp.status_code == 200
        assert resp.json() == response_ns

        request_st = {**request_ns, "stream": True}
        async with rc.stream("POST", "/v1/chat/completions", json=request_st) as resp:
            payloads = await _sse_payloads(resp)
        assert payloads[-1] == "[DONE]"

    files = sorted(snapshots_dir.glob("*.json"))
    assert len(files) == 2, f"expected 2 snapshot files, got {files}"

    by_hash = {}
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        by_hash[data["request_hash"]] = data

    snap_ns = next(d for d in by_hash.values() if d["non_stream_response"] is not None)
    assert snap_ns["scenario"] == "m2"
    assert snap_ns["request"] == request_ns
    assert snap_ns["non_stream_response"] == response_ns

    snap_st = next(d for d in by_hash.values() if d["stream_chunks"])
    assert [e["chunk"] for e in snap_st["stream_chunks"]] == [chunk_a, chunk_b, chunk_done]
    assert all(e["delay_ms"] >= 0 for e in snap_st["stream_chunks"])

    # 切回 replay：同请求立即命中，行为与录制一致
    replay_app = create_app(snapshots_dir, mode="replay")
    replay_transport = httpx.ASGITransport(app=replay_app)
    async with httpx.AsyncClient(transport=replay_transport, base_url="http://srs.test") as pc:
        await pc.post("/_test/config", json={"delay_scale": 0})
        resp = await pc.post("/v1/chat/completions", json=request_ns)
        assert resp.status_code == 200
        assert resp.json() == response_ns

        async with pc.stream("POST", "/v1/chat/completions", json=request_st) as resp:
            payloads = await _sse_payloads(resp)
        assert [json.loads(p) for p in payloads[:-1]] == [chunk_a, chunk_b, chunk_done]
