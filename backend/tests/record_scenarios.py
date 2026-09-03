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


def council_scenarios(model: str) -> list[tuple[str, dict]]:
    """council 裁判形态：成员请求复用 passthrough_basic(0.7)/param_merge_09(0.9) 快照。

    裁判 Prompt 用生产代码的渲染函数构造，保证与运行时逐字节一致（快照命中前提）。
    """

    from app.strategies.council import DEFAULT_JUDGE_TEMPLATE, render_judge_prompt

    question = [{"role": "user", "content": "用一句话解释量子纠缠"}]
    ans_a = snapshot_content("passthrough_basic")
    ans_b = snapshot_content("param_merge_09")

    def judge_messages(answers: list[tuple[str, str]]) -> list[dict]:
        prompt = render_judge_prompt(DEFAULT_JUDGE_TEMPLATE, question, answers)
        return [{"role": "user", "content": prompt}]

    two_answers = [(model, ans_a), (model, ans_b)]
    one_answer = [(model, ans_a)]
    return [
        (
            "council_judge",
            {"model": model, "messages": judge_messages(two_answers), "stream": False},
        ),
        (
            "council_judge_stream",
            {"model": model, "messages": judge_messages(two_answers), "stream": True},
        ),
        (
            "council_judge_single_07",
            {
                "model": model,
                "messages": judge_messages(one_answer),
                "temperature": 0.7,
                "stream": False,
            },
        ),
    ]


def snapshot_content(scenario: str) -> str:
    import json

    matches = sorted(SNAPSHOTS_DIR.glob(f"{scenario}__*.json"))
    assert matches, f"scenario {scenario} must be recorded before council scenarios"
    data = json.loads(matches[0].read_text(encoding="utf-8"))
    return data["non_stream_response"]["choices"][0]["message"]["content"]


def build_scenarios(model: str) -> list[tuple[str, dict]]:
    """基线场景：确定性请求文本，严禁含时间戳/随机数（否则 hash 失配）。"""
    base = [
        (
            "passthrough_basic",
            {
                "model": model,
                "messages": [{"role": "user", "content": "用一句话解释量子纠缠"}],
                "temperature": 0.7,
                "stream": False,
            },
        ),
        (
            "passthrough_stream",
            {
                "model": model,
                "messages": [
                    {"role": "user", "content": "写一首关于秋天的四行短诗，每行不超过10个字"}
                ],
                "temperature": 0.7,
                "max_tokens": 200,
                "stream": True,
            },
        ),
        (
            "model_test_msg",
            {
                "model": model,
                "messages": [{"role": "user", "content": "连通性测试：请只回复 pong"}],
                "temperature": 0.0,
                "stream": False,
            },
        ),
        (
            "param_merge_09",
            {
                "model": model,
                "messages": [{"role": "user", "content": "用一句话解释量子纠缠"}],
                "temperature": 0.9,
                "stream": False,
            },
        ),
    ]
    return base + council_scenarios(model)


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
            for name, body in build_scenarios(model):
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
                        f"http://127.0.0.1:{port}/v1/chat/completions", json=body, headers=headers
                    )
                    content = resp.json()["choices"][0]["message"]["content"]
                    print(f"[{name}] {resp.status_code}: {content[:120]}")
                if existed:
                    replayed += 1
                else:
                    recorded += 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    after = len(SnapshotStore(SNAPSHOTS_DIR))
    print(f"done: recorded={recorded} replayed={replayed}, snapshots {before} -> {after}")


if __name__ == "__main__":
    main()
