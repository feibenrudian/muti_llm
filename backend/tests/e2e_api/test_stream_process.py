"""AE-38-1..5 / AE-39-1..2：过程流式（stream_process）——reasoning 过程块、错误契约、
pipeline 级默认与请求体覆盖（回放）。"""

import asyncio
import json
import re

import httpx

from app.orm import RequestLog
from app.repos import Repository, list_model_calls
from tests.conftest import auth_headers
from tests.e2e_api.test_council import COUNCIL_BODY, seed_council
from tests.e2e_api.test_council_faults import _seed_anthropic_judge_pipeline
from tests.e2e_api.test_gateway_stream import stream_body
from tests.e2e_api.test_ice import seed_ice, srs_config
from tests.helpers import seed_provider_and_model, snapshot_content, snapshot_stream_text
from tests.record_scenarios import ICE_REFINE_QUESTION


async def _collect_payloads(resp: httpx.Response) -> list[str]:
    return [line[len("data: ") :] async for line in resp.aiter_lines() if line.startswith("data: ")]


def _chunks(payloads: list[str]) -> tuple[list[dict], bool]:
    done = bool(payloads) and payloads[-1] == "[DONE]"
    body = payloads[:-1] if done else payloads
    return [json.loads(p) for p in body], done


_HEADER = re.compile(r"^【(成员 .+|评论|第 \d+ 轮评论)】\n$")


def _reasoning_blocks(chunks: list[dict]) -> list[tuple[str, str]]:
    """reasoning_content chunk 序列按标题块切分为 (header, body)；body 为后续行拼接。

    标题块是网关单独发射的整块（如 `【成员 X】\\n`）；正文里的【回答 N】等行不算。
    """
    blocks: list[tuple[str, str]] = []
    for c in chunks:
        text = c["choices"][0]["delta"].get("reasoning_content")
        if text is None:
            continue
        if _HEADER.match(text):
            blocks.append((text, ""))
        else:
            assert blocks, "reasoning 内容先于标题块出现"
            blocks[-1] = (blocks[-1][0], blocks[-1][1] + text)
    return blocks


def _content_text(chunks: list[dict]) -> str:
    return "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)


async def _latest_detail(client: httpx.AsyncClient) -> dict:
    """background persist 在响应结束后执行，稍等再查详情。"""
    await asyncio.sleep(0.2)
    from app.main import app as backend_app

    async with backend_app.state.session_factory() as session:
        trace_id = (await Repository(session, RequestLog).list())[-1].id
    resp = await client.get(f"/api/admin/traces/{trace_id}")
    assert resp.status_code == 200
    return resp.json()


async def test_council_stream_process(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-38-1 council 过程流式：成员块×2（各自连续）→ 评论块 → content；落库明细不变。"""
    await seed_council(asgi_client, srs_live_seeded)
    async with asgi_client.stream(
        "POST",
        "/v1/chat/completions",
        json={**COUNCIL_BODY, "stream": True, "stream_process": True},
        headers=auth_headers(service_key),
    ) as resp:
        assert resp.status_code == 200
        payloads = await _collect_payloads(resp)

    chunks, done = _chunks(payloads)
    assert done
    # 首 chunk 携带 role + reasoning（成员标题块）
    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
    assert "reasoning_content" in chunks[0]["choices"][0]["delta"]

    blocks = _reasoning_blocks(chunks)
    # 阶段顺序：成员块 ×2 → 评论块（成员完成序不定，不断言具体成员次序）
    assert [b[0] for b in blocks] == ["【成员 deepseek-v4-flash】\n"] * 2 + ["【评论】\n"]
    assert {b[1] for b in blocks[:2]} == {
        snapshot_content("passthrough_basic"),
        snapshot_content("param_merge_09"),
    }
    assert blocks[2][1] == snapshot_content("council_critique")
    # content 阶段全部位于过程块之后
    first_content = next(
        i for i, c in enumerate(chunks) if c["choices"][0]["delta"].get("content")
    )
    last_reasoning = max(
        i for i, c in enumerate(chunks) if "reasoning_content" in c["choices"][0]["delta"]
    )
    assert first_content > last_reasoning
    assert _content_text(chunks) == snapshot_stream_text("council_judge")

    detail = await _latest_detail(asgi_client)
    assert detail["request"]["status"] == "success"
    assert [c["role"] for c in detail["calls"]] == ["member", "member", "judge_critique", "judge"]
    assert detail["request"]["total_duration_ms"] >= detail["request"]["first_token_ms"] > 0


async def test_ice_stream_process(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-38-2 ICE 多轮过程流式：每轮成员块→该轮评论块→终局 content；轮次落库不变。"""
    await seed_ice(asgi_client, srs_live_seeded)
    await srs_config(srs_live_seeded, {"reset": True})

    body = {
        "model": "ice-v1",
        "messages": [{"role": "user", "content": ICE_REFINE_QUESTION}],
        "stream": True,
        "stream_process": True,
    }
    async with asgi_client.stream(
        "POST", "/v1/chat/completions", json=body, headers=auth_headers(service_key)
    ) as resp:
        assert resp.status_code == 200
        payloads = await _collect_payloads(resp)

    chunks, done = _chunks(payloads)
    assert done
    blocks = _reasoning_blocks(chunks)
    headers = [b[0] for b in blocks]
    assert headers == [
        "【成员 deepseek-v4-flash】\n",
        "【成员 deepseek-v4-flash】\n",
        "【第 0 轮评论】\n",
        "【成员 deepseek-v4-flash】\n",
        "【成员 deepseek-v4-flash】\n",
        "【第 1 轮评论】\n",
    ]
    assert {b[1] for b in blocks[0:2]} == {
        snapshot_content("ice_refine_consensus_m0_r0"),
        snapshot_content("ice_refine_consensus_m1_r0"),
    }
    assert blocks[2][1] == snapshot_content("ice_refine_consensus_critique_r0")
    assert {b[1] for b in blocks[3:5]} == {
        snapshot_content("ice_refine_consensus_m0_r1"),
        snapshot_content("ice_refine_consensus_m1_r1"),
    }
    assert blocks[5][1] == snapshot_content("ice_refine_consensus_critique_r1")
    assert _content_text(chunks) == snapshot_stream_text("ice_refine_consensus_final")

    detail = await _latest_detail(asgi_client)
    assert detail["request"]["status"] == "success"
    assert [(c["role"], c["round"]) for c in detail["calls"]] == [
        ("member", 0),
        ("member", 0),
        ("judge_critique", 0),
        ("member", 1),
        ("member", 1),
        ("judge_critique", 1),
        ("judge", None),
    ]


async def test_stream_process_all_members_failed(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-38-3 全部成员失败+flag：开流前 502 JSON（同今日错误形态），成员行照落。"""
    await seed_council(asgi_client, srs_live_seeded)
    await srs_config(srs_live_seeded, {"fail_times": {"deepseek-v4-flash": 5}})

    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={**COUNCIL_BODY, "stream": True, "stream_process": True},
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 502
    message = resp.json()["error"]["message"]
    assert "全部成员失败" in message
    assert "deepseek-v4-flash" in message

    await asyncio.sleep(0.2)
    from app.main import app as backend_app

    async with backend_app.state.session_factory() as session:
        trace = (await Repository(session, RequestLog).list())[-1]
        calls = await list_model_calls(session, trace.id)
    assert trace.status == "failed"
    assert [c.role for c in calls] == ["member", "member"]  # 裁判未执行
    assert all(c.status == "failed" and c.error_message for c in calls)


async def test_stream_process_critique_failed(
    asgi_client: httpx.AsyncClient,
    service_key: str,
    srs_live_seeded: str,
    anthropic_mock: str,
) -> None:
    """AE-38-4 评论失败+flag：200，成员块已发出，流内终止无 [DONE]，trace=failed。"""
    await _seed_anthropic_judge_pipeline(
        asgi_client, srs_live_seeded, anthropic_mock, strict_judge=False
    )
    resp = await httpx.AsyncClient().post(f"{anthropic_mock}/_test/config", json={"fail_times": 3})
    assert resp.status_code == 200

    async with asgi_client.stream(
        "POST",
        "/v1/chat/completions",
        json={**COUNCIL_BODY, "stream": True, "stream_process": True},
        headers=auth_headers(service_key),
    ) as resp:
        assert resp.status_code == 200
        payloads = await _collect_payloads(resp)

    chunks, done = _chunks(payloads)
    assert not done  # 流内终止：无 [DONE]
    blocks = _reasoning_blocks(chunks)
    assert [b[0] for b in blocks] == ["【成员 deepseek-v4-flash】\n"] * 2 + ["【评论】\n"]
    assert "调用失败" in blocks[2][1]
    assert all(not c["choices"][0]["delta"].get("content") for c in chunks)  # 终局未开始

    detail = await _latest_detail(asgi_client)
    assert detail["request"]["status"] == "failed"
    assert [c["role"] for c in detail["calls"]] == ["member", "member", "judge_critique"]
    assert detail["calls"][-1]["status"] == "failed"


async def test_stream_process_pipeline_default_on(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-39-1 pipeline 默认开：请求体不带 stream_process → 流式响应含 reasoning 过程块。"""
    await seed_council(asgi_client, srs_live_seeded, stream_process=True)
    async with asgi_client.stream(
        "POST",
        "/v1/chat/completions",
        json={**COUNCIL_BODY, "stream": True},
        headers=auth_headers(service_key),
    ) as resp:
        assert resp.status_code == 200
        payloads = await _collect_payloads(resp)

    chunks, done = _chunks(payloads)
    assert done
    blocks = _reasoning_blocks(chunks)
    assert [b[0] for b in blocks] == ["【成员 deepseek-v4-flash】\n"] * 2 + ["【评论】\n"]
    assert _content_text(chunks) == snapshot_stream_text("council_judge")


async def test_stream_process_body_overrides_pipeline_default(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-39-2 请求体显式 false 覆盖 pipeline 默认开：经典路径，零 reasoning 块。"""
    await seed_council(asgi_client, srs_live_seeded, stream_process=True)
    async with asgi_client.stream(
        "POST",
        "/v1/chat/completions",
        json={**COUNCIL_BODY, "stream": True, "stream_process": False},
        headers=auth_headers(service_key),
    ) as resp:
        assert resp.status_code == 200
        payloads = await _collect_payloads(resp)

    chunks, done = _chunks(payloads)
    assert done
    assert all("reasoning_content" not in c["choices"][0]["delta"] for c in chunks)
    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
    assert _content_text(chunks) == snapshot_stream_text("council_judge")


async def test_stream_process_flag_ignored(
    asgi_client: httpx.AsyncClient, service_key: str, srs_live_seeded: str
) -> None:
    """AE-38-5 flag 静默忽略：透传流式与非流式 pipeline 带 stream_process → 行为不变。"""
    # 透传流式 + flag：与 AE-12-1 完全同形态，全程无 reasoning_content
    await seed_provider_and_model(asgi_client, srs_live_seeded, provider_name="srs-pt-provider")
    async with asgi_client.stream(
        "POST",
        "/v1/chat/completions",
        json={**stream_body(), "stream_process": True},
        headers=auth_headers(service_key),
    ) as resp:
        assert resp.status_code == 200
        payloads = await _collect_payloads(resp)
    chunks, done = _chunks(payloads)
    assert done
    assert all("reasoning_content" not in c["choices"][0]["delta"] for c in chunks)
    assert chunks[0]["choices"][0]["delta"].get("role") == "assistant"
    assert _content_text(chunks) == snapshot_stream_text("passthrough_stream")

    # 非流式 pipeline + flag：JSON 响应与今日一致
    await seed_council(asgi_client, srs_live_seeded)
    resp = await asgi_client.post(
        "/v1/chat/completions",
        json={**COUNCIL_BODY, "stream_process": True},
        headers=auth_headers(service_key),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == snapshot_content("council_judge")
