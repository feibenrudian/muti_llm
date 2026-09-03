"""Anthropic 协议的测试替身：线程起的本地服务，POST /v1/messages 返回可编程响应。

不使用 httpx2.MockTransport 的原因：其对 async generator 响应体的 isinstance 断言
在部分场景下抛 AssertionError（被 SDK 包装为连接错误），行为不可靠；走真实 HTTP 栈稳定。
"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator, Generator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

DEFAULT_NON_STREAM: dict[str, Any] = {
    "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4",
    "content": [{"type": "text", "text": "ANSWER"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 7, "output_tokens": 3},
}


def build_sse_events(deltas: Sequence[str]) -> list[str]:
    """按增量文本生成合法的 Anthropic SSE 事件行序列。"""
    lines = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "role": "assistant",
                    "content": [],
                    "model": "claude-sonnet-4",
                    "usage": {"input_tokens": 5, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
    ]
    for text in deltas:
        lines.append(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
    lines += [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 2},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    import json

    return [f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in lines]


@dataclass
class MockState:
    non_stream: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_NON_STREAM))
    sse_events: list[str] = field(default_factory=lambda: build_sse_events(["你好", "，世界"]))
    fail_times: int = 0
    fail_status: int = 500
    recordings: list[dict[str, Any]] = field(default_factory=list)


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None)
    state = MockState()
    app.state.mock = state

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        body = await request.json()
        state.recordings.append({"body": body, "headers": dict(request.headers)})
        if state.fail_times > 0:
            state.fail_times -= 1
            return JSONResponse(
                status_code=state.fail_status,
                content={
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "mock failure"},
                },
            )
        if body.get("stream"):
            events = state.sse_events

            async def gen() -> AsyncIterator[str]:
                for line in events:
                    yield line

            return StreamingResponse(gen(), media_type="text/event-stream")
        return JSONResponse(state.non_stream)

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        # Provider 连通性探测端点（Anthropic Models API 形状），固定返回一个模型
        state.recordings.append({"body": {"path": "/v1/models"}, "headers": dict(request.headers)})
        return JSONResponse(
            {
                "data": [
                    {"type": "model", "id": "claude-sonnet-4", "display_name": "Claude Sonnet 4"}
                ],
                "first_id": "claude-sonnet-4",
                "has_more": False,
                "last_id": "claude-sonnet-4",
            }
        )

    @app.post("/_test/config")
    async def config(request: Request) -> Response:
        body = await request.json()
        if body.get("reset"):
            state.non_stream = dict(DEFAULT_NON_STREAM)
            state.sse_events = build_sse_events(["你好", "，世界"])
            state.fail_times = 0
            state.fail_status = 500
            state.recordings.clear()
        if "non_stream" in body:
            state.non_stream = body["non_stream"]
        if "sse_events" in body:
            state.sse_events = body["sse_events"]
        if "fail_times" in body:
            state.fail_times = int(body["fail_times"])
        if "fail_status" in body:
            state.fail_status = int(body["fail_status"])
        return JSONResponse({"fail_times": state.fail_times, "fail_status": state.fail_status})

    @app.get("/_test/requests")
    async def requests() -> Response:
        return JSONResponse({"requests": state.recordings})

    return app


@pytest.fixture
def anthropic_mock() -> Generator[str, None, None]:
    """线程起 mock 服务，返回 base_url（anthropic SDK 会拼接 /v1/messages）。"""
    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "anthropic mock failed to start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
