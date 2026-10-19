"""AE-53-1..3：透传路径工具协议多轮回放（T53）——agent 第二轮（工具结果回传）不再 400。

AE-53-1 用线程起真实 HTTP 的上游桩（AGENTS.md mock 纪律，同 tests/mock_anthropic 模式）：
快照录制凭据已失效，且桩可直接断言"网关实际发给上游的 payload"，强于回放断言。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
from sqlalchemy import select

from tests.conftest import auth_headers
from tests.helpers import tool_replay_messages

REPLAY_BODY = {
    "model": "deepseek-v4-flash",
    "messages": tool_replay_messages(),
    "temperature": 0.0,
}


class _MockUpstream:
    """确定性上游桩：记录收到的请求体，回复固定 SSE（content → finish → usage → DONE）。"""

    def __init__(self) -> None:
        self.received: dict[str, Any] = {}
        self.models_seen = False
        upstream = self  # Handler 闭包引用外层状态

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # 供应商创建时的上游模型列表同步
                upstream.models_seen = True
                body = json.dumps(
                    {"object": "list", "data": [{"id": "deepseek-v4-flash", "object": "model"}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                upstream.received.update(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                model = upstream.received.get("model", "")
                parts = [
                    {
                        "id": "chunk-1",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [
                            {"index": 0, "delta": {"content": "天气晴"}, "finish_reason": None}
                        ],
                    },
                    {
                        "id": "chunk-2",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": "stop"},
                        ],
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 3,
                            "total_tokens": 15,
                        },
                    },
                    "[DONE]",
                ]
                lines = [p if isinstance(p, str) else json.dumps(p) for p in parts]
                payload = "".join(f"data: {line}\n\n" for line in lines).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:  # 静默访问日志
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_MockUpstream":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"


async def _seed_mock_provider(asgi_client: httpx.AsyncClient, base_url: str) -> str:
    resp = await asgi_client.post(
        "/api/admin/providers",
        json={
            "name": "mock-replay",
            "protocol": "openai_compatible",
            "base_url": base_url,
            "api_key": "sk-mock",
        },
    )
    assert resp.status_code == 201, resp.text
    slug = resp.json()["slug"]
    models = (await asgi_client.get("/api/admin/models")).json()
    assert any(m["upstream_model_id"] == "deepseek-v4-flash" for m in models)
    return slug


async def test_tool_replay_passthrough(asgi_client: httpx.AsyncClient, service_key: str) -> None:
    """AE-53-1 多轮回放：assistant.tool_calls + role=tool 历史经老入口与 route 均成功，
    上游收到的 messages 完整携带 tool 字段（不再被裁剪成 role+content）。"""
    with _MockUpstream() as upstream:
        slug = await _seed_mock_provider(asgi_client, upstream.base_url)

        resp = await asgi_client.post(
            "/v1/chat/completions", json=REPLAY_BODY, headers=auth_headers(service_key)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["choices"][0]["message"]["content"] == "天气晴"

        # route 命名空间同路径
        resp = await asgi_client.post(
            f"/v1/route/{slug}/chat/completions",
            json=REPLAY_BODY,
            headers=auth_headers(service_key),
        )
        assert resp.status_code == 200, resp.text

        # 上游收到的消息：tool_calls/tool_call_id 完整、content=null 保留、普通消息无多余键
        sent = upstream.received["messages"]
        assert sent[0]["role"] == "user"
        assert "广州现在多少度" in sent[0]["content"]
        assert sent[1]["content"] is None
        assert sent[1]["tool_calls"][0]["function"]["name"] == "get_weather"
        assert sent[1]["reasoning_content"]  # 思考模型回传（DeepSeek 思考模式硬性要求）
        assert sent[2]["tool_call_id"] == sent[1]["tool_calls"][0]["id"]
        assert sent[2]["content"] == "广州 晴，气温 28°C"

    # 调用行落库：发出的 payload 与上游收到的一致
    from app.main import app
    from app.orm import ModelCallLog

    async with app.state.session_factory() as session:
        logs = (
            (
                await session.execute(
                    select(ModelCallLog)
                    .where(ModelCallLog.role == "passthrough")
                    .order_by(ModelCallLog.id.desc())
                )
            )
            .scalars()
            .all()
        )
    assert len(logs) >= 2
    sent = logs[0].request_payload["messages"]
    assert sent[1]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert sent[2]["tool_call_id"] == sent[1]["tool_calls"][0]["id"]


async def test_tool_replay_snapshot_roundtrip(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-53-4 真实快照回放：多轮工具消息经 SRS 回放成功，回答 = 快照全文。"""
    from tests.helpers import seed_provider_and_model, snapshot_content

    await seed_provider_and_model(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions", json=REPLAY_BODY, headers=auth_headers(service_key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == snapshot_content(
        "passthrough_tool_replay"
    )


async def test_pipeline_rejects_tool_history(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-53-2 聚合路径拒绝工具回放：/v1/pipeline 与老入口 pipeline 分支均 400（语义不变）。"""
    from tests.e2e_api.test_council import seed_council

    await seed_council(asgi_client, srs_live_seeded)
    body = dict(REPLAY_BODY, model="council-v1")

    resp = await asgi_client.post(
        "/v1/pipeline/chat/completions", json=body, headers=auth_headers(service_key)
    )
    assert resp.status_code == 400
    assert "工具协议消息回放" in resp.json()["error"]["message"]

    resp = await asgi_client.post(
        "/v1/chat/completions", json=body, headers=auth_headers(service_key)
    )
    assert resp.status_code == 400
    assert "工具协议消息回放" in resp.json()["error"]["message"]


async def test_rejected_request_logged(asgi_client: httpx.AsyncClient, service_key: str) -> None:
    """AE-53-3 校验拒绝落 failed trace：error_message 带原因、原始 messages 保留、归属正确。

    拒绝发生在路由解析之前，无需可达上游（route ident 故意非法）。"""
    bad = dict(REPLAY_BODY, messages=[{"role": "tool", "content": "缺 tool_call_id"}])

    before = (await asgi_client.get("/api/admin/traces")).json()["total"]
    resp = await asgi_client.post(
        "/v1/route/x/chat/completions", json=bad, headers=auth_headers(service_key)
    )
    assert resp.status_code == 400

    listing = (await asgi_client.get("/api/admin/traces")).json()
    assert listing["total"] == before + 1
    item = listing["items"][0]
    assert item["status"] == "failed"
    assert item["user_username"] == "admin"
    assert "tool_call_id" in item["error_message"]

    detail = (await asgi_client.get(f"/api/admin/traces/{item['id']}")).json()
    assert detail["request"]["error_message"]
    assert detail["request"]["request_messages"] == [{"role": "tool", "content": "缺 tool_call_id"}]
