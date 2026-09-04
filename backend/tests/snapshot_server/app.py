"""SRS 应用：OpenAI 兼容端点，replay/record 双模式 + 失败/超时注入 + 请求录像。

replay（默认）：请求规范化 hash → 查快照 → 原样回放（非流式返回全文；流式按录制节奏重放）。
record：未命中快照时透传真实上游（DeepSeek），把响应/chunk 序列落盘为新快照。
注入控制（优先于快照）：fail_times 连续失败 N 次后恢复；timeout_ms 模拟挂死；
delay_scale 缩放 chunk 间隔。
请求录像：/_test/requests 返回收到的全部请求原文，是断言"上游实际收到什么"的唯一手段。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from tests.snapshot_server.hashing import request_hash
from tests.snapshot_server.store import Snapshot, SnapshotStore


@dataclass
class SrsState:
    store: SnapshotStore
    mode: str = "replay"
    record_base_url: str | None = None
    record_api_key: str | None = None
    record_client: httpx.AsyncClient | None = None  # 测试注入用（MockTransport）
    fail_times: dict[str, int] = field(default_factory=dict)
    timeout_ms: dict[str, int] = field(default_factory=dict)
    delay_scale: float = 1.0
    # reset 的恢复基准：保持构造时（如 runner 提速）的缩放，而非硬编码 1.0
    initial_delay_scale: float = 1.0
    models_auth_fail: bool = False
    recordings: list[dict[str, Any]] = field(default_factory=list)


def _scenario(body: dict[str, Any]) -> str:
    return str(body.get("model", "default"))


def _sse_lines(entries: list[dict[str, Any]], delay_scale: float) -> AsyncIterator[str]:
    async def gen() -> AsyncIterator[str]:
        for entry in entries:
            delay_ms = entry.get("delay_ms", 0) * delay_scale
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000)
            yield f"data: {json.dumps(entry['chunk'], ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return gen()


def _nearest(hash_: str, candidates: list[str], k: int = 3) -> list[str]:
    def common_prefix(a: str, b: str) -> int:
        n = 0
        for x, y in zip(a, b, strict=False):
            if x != y:
                break
            n += 1
        return n

    return sorted(candidates, key=lambda c: -common_prefix(hash_, c))[:k]


def _new_record_client(state: SrsState) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {state.record_api_key or ''}"}
    return httpx.AsyncClient(base_url=state.record_base_url, headers=headers, timeout=120.0)


async def _record_non_stream(
    client: httpx.AsyncClient, body: dict[str, Any], state: SrsState, scenario: str
) -> Response:
    resp = await client.post("/chat/completions", json=body)
    data = resp.json()
    state.store.save(Snapshot(scenario=scenario, request=body, non_stream_response=data))
    return JSONResponse(data, status_code=resp.status_code)


async def _record_stream(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    state: SrsState,
    own_client: bool,
    scenario: str,
) -> Response:
    request = client.build_request("POST", "/chat/completions", json=body)
    resp = await client.send(request, stream=True)
    if resp.status_code != 200:
        data = resp.json()
        await resp.aclose()
        if own_client:
            await client.aclose()
        state.store.save(Snapshot(scenario=scenario, request=body, non_stream_response=data))
        return JSONResponse(data, status_code=resp.status_code)

    async def gen() -> AsyncIterator[str]:
        entries: list[dict[str, Any]] = []
        last: float | None = None
        saved = False
        try:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[len("data: ") :]
                now = time.perf_counter()
                delay_ms = 0 if last is None else int((now - last) * 1000)
                last = now
                if payload == "[DONE]":
                    state.store.save(
                        Snapshot(scenario=scenario, request=body, stream_chunks=entries)
                    )
                    saved = True
                    yield "data: [DONE]\n\n"
                    break
                entries.append({"chunk": json.loads(payload), "delay_ms": delay_ms})
                yield line + "\n\n"
            if entries and not saved:
                state.store.save(Snapshot(scenario=scenario, request=body, stream_chunks=entries))
        finally:
            await resp.aclose()
            if own_client:
                await client.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream")


def create_app(
    snapshots_dir: Path,
    mode: str = "replay",
    *,
    record_base_url: str | None = None,
    record_api_key: str | None = None,
    record_client: httpx.AsyncClient | None = None,
    delay_scale: float = 1.0,
) -> FastAPI:
    if mode not in ("replay", "record"):
        raise ValueError(f"mode must be replay|record, got {mode!r}")
    if mode == "record" and record_client is None and not record_base_url:
        raise ValueError("record mode requires record_base_url (or an injected record_client)")

    app = FastAPI(title="SRS", docs_url=None)
    state = SrsState(
        store=SnapshotStore(snapshots_dir),
        mode=mode,
        record_base_url=record_base_url,
        record_api_key=record_api_key,
        record_client=record_client,
        delay_scale=delay_scale,
        initial_delay_scale=delay_scale,
    )
    app.state.srs = state

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        body = await request.json()
        model = str(body.get("model", ""))
        state.recordings.append({"model": model, "body": body, "ts": time.time()})

        remaining = state.fail_times.get(model, 0)
        if remaining > 0:
            state.fail_times[model] = remaining - 1
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "message": f"srs injected failure for model {model!r}",
                        "type": "injected_failure",
                    }
                },
            )

        hang_ms = state.timeout_ms.get(model)
        if hang_ms:
            await asyncio.sleep(hang_ms / 1000)

        hash_ = request_hash(body)
        snap = state.store.get(hash_)
        if snap is not None:
            # 命中快照：record 模式也直接回放（make record 幂等，不重复真实调用）
            if snap.non_stream_response is not None:
                return JSONResponse(snap.non_stream_response)
            return StreamingResponse(
                _sse_lines(snap.stream_chunks, state.delay_scale),
                media_type="text/event-stream",
            )

        if state.mode == "record":
            # 录制场景名：驱动脚本可带 X-Srs-Scenario 头（仅元数据，不参与请求 hash）
            scenario = request.headers.get("x-srs-scenario") or _scenario(body)
            if state.record_client is not None:
                if bool(body.get("stream")):
                    return await _record_stream(
                        state.record_client, body, state, own_client=False, scenario=scenario
                    )
                return await _record_non_stream(state.record_client, body, state, scenario)
            client = _new_record_client(state)
            if bool(body.get("stream")):
                return await _record_stream(client, body, state, own_client=True, scenario=scenario)
            async with client:
                return await _record_non_stream(client, body, state, scenario)

        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": (
                        f"snapshot not found: request_hash={hash_}; "
                        f"nearest={_nearest(hash_, state.store.hashes())}"
                    ),
                    "type": "snapshot_miss",
                    "request_hash": hash_,
                    "nearest": _nearest(hash_, state.store.hashes()),
                }
            },
        )

    @app.get("/v1/models")
    async def list_models(request: Request) -> Response:
        # Provider 连通性探测端点：无请求体可 hash，返回确定性的模型 ID 列表（源自快照库）。
        # 不含认证头原文（AGENTS.md 纪律），只记录是否携带以及探测发生本身。
        state.recordings.append(
            {
                "model": "__models_list__",
                "body": {
                    "path": "/v1/models",
                    "has_authorization": bool(request.headers.get("authorization")),
                },
                "ts": time.time(),
            }
        )
        if state.models_auth_fail:
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Incorrect API key provided",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
            )
        data = [
            {"id": mid, "object": "model", "owned_by": "srs"} for mid in state.store.model_ids()
        ]
        return JSONResponse({"object": "list", "data": data})

    @app.post("/_test/config")
    async def test_config(request: Request) -> Response:
        body = await request.json()
        if body.get("reset"):
            state.fail_times.clear()
            state.timeout_ms.clear()
            state.delay_scale = state.initial_delay_scale
            state.models_auth_fail = False
            state.recordings.clear()
        if "fail_times" in body:
            state.fail_times.update(body["fail_times"])
        if "timeout_ms" in body:
            state.timeout_ms.update(body["timeout_ms"])
        if "delay_scale" in body:
            state.delay_scale = float(body["delay_scale"])
        if "models_auth_fail" in body:
            state.models_auth_fail = bool(body["models_auth_fail"])
        return JSONResponse(
            {
                "mode": state.mode,
                "fail_times": state.fail_times,
                "timeout_ms": state.timeout_ms,
                "delay_scale": state.delay_scale,
                "models_auth_fail": state.models_auth_fail,
                "recordings": len(state.recordings),
                "snapshots": len(state.store),
            }
        )

    @app.get("/_test/requests")
    async def test_requests(model: str | None = None) -> Response:
        items = state.recordings
        if model:
            items = [r for r in items if r["model"] == model]
        return JSONResponse({"requests": items})

    return app
