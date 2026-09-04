"""make record：以 record 模式驱动 SRS，真实调用 DeepSeek 录制基线快照。

幂等：已命中快照的场景直接回放，只有新增/变更的请求才真实调用。
凭据读取 backend/.env.test（RECORD_API_KEY / RECORD_BASE_URL / RECORD_MODEL）。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from dotenv import load_dotenv

from tests.snapshot_server.app import create_app
from tests.snapshot_server.hashing import request_hash
from tests.snapshot_server.store import SnapshotStore

BACKEND_DIR = Path(__file__).resolve().parent.parent
SNAPSHOTS_DIR = BACKEND_DIR / "tests" / "snapshots"
ENV_FILE = BACKEND_DIR / ".env.test"


# 上游一律流式调用（决策 D8）：录制体 = 实际发出的请求形态（与 hash 一致）
def STREAM_BODY(model: str, messages: list[dict]) -> dict:
    return {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def council_critique_scenarios(model: str) -> list[tuple[str, dict]]:
    """两段式裁判第一段（评论）：内置模板，请求体确定性（原问题 + 成员答案快照全文）。"""
    from app.strategies.council import DEFAULT_CRITIQUE_TEMPLATE, render_judge_prompt

    question = [{"role": "user", "content": "用一句话解释量子纠缠"}]
    ans_a = snapshot_content("passthrough_basic")
    ans_b = snapshot_content("param_merge_09")

    def critique_messages(answers: list[tuple[str, str]]) -> list[dict]:
        prompt = render_judge_prompt(DEFAULT_CRITIQUE_TEMPLATE, question, answers)
        return [{"role": "user", "content": prompt}]

    return [
        (
            "council_critique",
            STREAM_BODY(model=model, messages=critique_messages([(model, ans_a), (model, ans_b)])),
        ),
        (
            "council_critique_single_07",
            {
                **STREAM_BODY(model=model, messages=critique_messages([(model, ans_a)])),
                "temperature": 0.7,
            },
        ),
    ]


def council_final_scenarios(model: str) -> list[tuple[str, dict]]:
    """两段式裁判第二段（最终答案）：与第一次同会话（输入 → 评论 → 最终指令），须在评论场景录制后运行。"""
    from app.strategies.council import (
        DEFAULT_CRITIQUE_TEMPLATE,
        DEFAULT_JUDGE_TEMPLATE,
        build_final_messages,
        render_final_instruction,
        render_judge_prompt,
    )
    from tests.helpers import LEGACY_JUDGE_TEMPLATE

    question = [{"role": "user", "content": "用一句话解释量子纠缠"}]
    ans_a = snapshot_content("passthrough_basic")
    ans_b = snapshot_content("param_merge_09")
    crit_two = snapshot_content("council_critique")
    crit_single = snapshot_content("council_critique_single_07")

    def final_messages(answers: list[tuple[str, str]], critique: str, template: str) -> list[dict]:
        critique_prompt = render_judge_prompt(DEFAULT_CRITIQUE_TEMPLATE, question, answers)
        instruction = render_final_instruction(template, question, answers, critique)
        return build_final_messages([{"role": "user", "content": critique_prompt}], critique, instruction)

    # 上游一律流式后，客户端流式/非流式共用同一裁判请求体（一个快照服务两种客户端形态）
    return [
        (
            "council_judge",
            STREAM_BODY(
                model=model,
                messages=final_messages([(model, ans_a), (model, ans_b)], crit_two, DEFAULT_JUDGE_TEMPLATE),
            ),
        ),
        (
            "council_judge_single_07",
            {
                **STREAM_BODY(
                    model=model,
                    messages=final_messages([(model, ans_a)], crit_single, DEFAULT_JUDGE_TEMPLATE),
                ),
                "temperature": 0.7,
            },
        ),
        # 存量自定义模板（旧版默认文本）：作为最终指令轮渲染（原始对话/答案会在此轮重述）
        (
            "council_judge_custom",
            STREAM_BODY(
                model=model,
                messages=final_messages(
                    [(model, ans_a), (model, ans_b)], crit_two, LEGACY_JUDGE_TEMPLATE
                ),
            ),
        ),
    ]


def snapshot_content(scenario: str) -> str:
    import json

    matches = sorted(SNAPSHOTS_DIR.glob(f"{scenario}__*.json"))
    assert matches, f"scenario {scenario} must be recorded before council scenarios"
    data = json.loads(matches[0].read_text(encoding="utf-8"))
    if data.get("stream_chunks"):
        return "".join(
            (c["chunk"].get("choices") or [{}])[0].get("delta", {}).get("content") or ""
            for c in data["stream_chunks"]
        )
    return data["non_stream_response"]["choices"][0]["message"]["content"]


def base_scenarios(model: str) -> list[tuple[str, dict]]:
    """基线场景：确定性请求文本，严禁含时间戳/随机数（否则 hash 失配）。"""
    base = [
        (
            "passthrough_basic",
            {
                **STREAM_BODY(
                    model=model,
                    messages=[{"role": "user", "content": "用一句话解释量子纠缠"}],
                ),
                "temperature": 0.7,
            },
        ),
        (
            "passthrough_stream",
            {
                **STREAM_BODY(
                    model=model,
                    messages=[
                        {"role": "user", "content": "写一首关于秋天的四行短诗，每行不超过10个字"}
                    ],
                ),
                "temperature": 0.7,
                "max_tokens": 2000,
            },
        ),
        (
            "model_test_msg",
            {
                **STREAM_BODY(
                    model=model,
                    messages=[{"role": "user", "content": "连通性测试：请只回复 pong"}],
                ),
                "temperature": 0.0,
            },
        ),
        (
            "param_merge_09",
            {
                **STREAM_BODY(
                    model=model,
                    messages=[{"role": "user", "content": "用一句话解释量子纠缠"}],
                ),
                "temperature": 0.9,
            },
        ),
    ]
    return base


def main() -> None:
    load_dotenv(ENV_FILE)
    api_key = os.environ.get("RECORD_API_KEY", "")
    base_url = os.environ.get("RECORD_BASE_URL", "")
    model = os.environ.get("RECORD_MODEL", "")
    if not (api_key and base_url and model):
        raise SystemExit(
            f"missing record credentials in {ENV_FILE} (RECORD_API_KEY/BASE_URL/MODEL)"
        )

    store = SnapshotStore(SNAPSHOTS_DIR)
    before = len(store)
    print(f"snapshots dir: {SNAPSHOTS_DIR} (existing: {before})")

    app = create_app(
        SNAPSHOTS_DIR,
        mode="record",
        record_base_url=base_url,
        record_api_key=api_key,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "SRS failed to start"
    port = server.servers[0].sockets[0].getsockname()[1]

    recorded = replayed = 0
    try:
        with httpx.Client(timeout=180.0) as client:

            def run_scenarios(items: list[tuple[str, dict]]) -> None:
                nonlocal recorded, replayed
                for name, body in items:
                    existed = store.get(request_hash(body)) is not None
                    headers = {"X-Srs-Scenario": name}
                    if body.get("stream"):
                        chunks: list[str] = []
                        with client.stream(
                            "POST",
                            f"http://127.0.0.1:{port}/v1/chat/completions",
                            json=body,
                            headers=headers,
                        ) as resp:
                            for line in resp.iter_lines():
                                if line.startswith("data: "):
                                    chunks.append(line[len("data: ") :])
                        tail = " | ".join(chunks[:-1])[:120]
                        print(f"[{name}] stream ok, chunks={len(chunks) - 1}: {tail}...")
                    else:
                        resp = client.post(
                            f"http://127.0.0.1:{port}/v1/chat/completions",
                            json=body,
                            headers=headers,
                        )
                        content = resp.json()["choices"][0]["message"]["content"]
                        print(f"[{name}] {resp.status_code}: {content[:120]}")
                    if existed:
                        replayed += 1
                    else:
                        recorded += 1

            # 三阶段：先录基础场景；评论场景的 Prompt 需要读取已录的成员答案全文；
            # 最终场景的 Prompt 需要读取已录的评论全文（两段式依赖链）
            run_scenarios(base_scenarios(model))
            run_scenarios(council_critique_scenarios(model))
            run_scenarios(council_final_scenarios(model))
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    after = len(SnapshotStore(SNAPSHOTS_DIR))
    print(f"done: recorded={recorded} replayed={replayed}, snapshots {before} -> {after}")


if __name__ == "__main__":
    main()
