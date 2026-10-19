"""AE-53b-1..2：上游失败对客户端可见（T53b）——流式补发 SSE 错误事件；非流式按 kind 映射状态码。

背景：此前流式透传失败时静默收流（HTTP 200 + 空内容），ZCode 等客户端只能报
empty_model_response，真实原因（如智谱 1113 余额不足）只留在网关日志里。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest

from tests.conftest import auth_headers

ZHIPU_429_BODY = {"error": {"code": "1113", "message": "余额不足或无可用资源包,请充值。"}}


class _RateLimitedUpstream:
    """恒定返回 429 余额不足的上游桩（智谱 1113 形态）；记录收到的 POST 次数。"""

    def __init__(self) -> None:
        self.post_count = 0
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # 供应商创建时的上游模型列表同步
                body = json.dumps(
                    {"object": "list", "data": [{"id": "glm-5.3-flash", "object": "model"}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                upstream.post_count += 1
                body = json.dumps(ZHIPU_429_BODY).encode()
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_RateLimitedUpstream":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"


@pytest.fixture
async def rate_limited_provider(asgi_client: httpx.AsyncClient):
    upstream = _RateLimitedUpstream()
    with upstream:
        resp = await asgi_client.post(
            "/api/admin/providers",
            json={
                "name": "mock-rate-limited",
                "protocol": "openai_compatible",
                "base_url": upstream.base_url,
                "api_key": "sk-mock",
            },
        )
        assert resp.status_code == 201, resp.text
        yield upstream


async def _latest_trace(asgi_client: httpx.AsyncClient) -> dict[str, Any]:
    listing = (await asgi_client.get("/api/admin/traces")).json()
    assert listing["total"] >= 1
    return listing["items"][0]


async def test_stream_failure_emits_error_event(
    asgi_client: httpx.AsyncClient,
    service_key: str,
    rate_limited_provider: _RateLimitedUpstream,
) -> None:
    """AE-53b-1 流式失败可见：SSE 中出现 OpenAI 兼容错误事件（含上游原文）+ [DONE] 正常收尾。"""
    async with asgi_client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "glm-5.3-flash",
            "messages": [{"role": "user", "content": "回复ok"}],
            "stream": True,
        },
        headers=auth_headers(service_key),
    ) as resp:
        assert resp.status_code == 200  # 首块已发，状态码不可回改；错误靠事件承载
        payloads: list[str] = []
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                payloads.append(line[len("data: ") :])

    errors = [json.loads(p)["error"] for p in payloads if p != "[DONE]" and '"error"' in p]
    assert errors, f"SSE 中缺少错误事件: {payloads}"
    assert "余额不足" in errors[0]["message"]
    assert errors[0]["code"] == "rate_limit"
    assert errors[0]["type"] == "upstream_error"
    assert payloads[-1] == "[DONE]"  # 正常收尾，客户端不会悬挂

    # 除角色块外没有任何内容/工具块（上游没产出任何增量）
    for p in payloads:
        if p == "[DONE]" or '"error"' in p:
            continue
        choices = json.loads(p).get("choices") or []
        delta = choices[0].get("delta", {}) if choices else {}
        assert not delta.get("content") and not delta.get("tool_calls")

    trace = await _latest_trace(asgi_client)
    assert trace["status"] == "failed"
    assert "余额不足" in trace["error_message"]
    assert rate_limited_provider.post_count == 1  # 流式路径无重试（D8 设计：流错误立即透出）


async def test_stream_failure_maps_429_non_stream(
    asgi_client: httpx.AsyncClient,
    service_key: str,
    rate_limited_provider: _RateLimitedUpstream,
) -> None:
    """AE-53b-2 非流式失败按 kind 映射：限流 → 429（不再伪装成 502），错误体含上游原文。"""
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={
            "model": "glm-5.3-flash",
            "messages": [{"role": "user", "content": "回复ok"}],
        },
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 429
    error = resp.json()["error"]
    assert error["type"] == "upstream_error"
    assert error["code"] == "rate_limit"
    assert "余额不足" in error["message"]

    trace = await _latest_trace(asgi_client)
    assert trace["status"] == "failed"
