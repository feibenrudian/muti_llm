"""make record：以 record 模式驱动 SRS，真实调用 DeepSeek 录制基线快照。

幂等：已命中快照的场景直接回放，只有新增/变更的请求才真实调用。
凭据读取 backend/.env.test（RECORD_API_KEY / RECORD_BASE_URL / RECORD_MODEL）。
"""

from __future__ import annotations

import json
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
        # AE-16-5：请求参数仅作用裁判——成员按覆盖 0.9 实发（答案=param_merge_09），裁判带请求 temp 0.7
        (
            "council_critique_single_b_07",
            {
                **STREAM_BODY(model=model, messages=critique_messages([(model, ans_b)])),
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
    crit_single_b = snapshot_content("council_critique_single_b_07")

    def final_messages(answers: list[tuple[str, str]], critique: str, template: str) -> list[dict]:
        critique_prompt = render_judge_prompt(DEFAULT_CRITIQUE_TEMPLATE, question, answers)
        instruction = render_final_instruction(template, question, answers, critique)
        return build_final_messages(
            [{"role": "user", "content": critique_prompt}], critique, instruction
        )

    # 上游一律流式后，客户端流式/非流式共用同一裁判请求体（一个快照服务两种客户端形态）
    return [
        (
            "council_judge",
            STREAM_BODY(
                model=model,
                messages=final_messages(
                    [(model, ans_a), (model, ans_b)], crit_two, DEFAULT_JUDGE_TEMPLATE
                ),
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
        (
            "council_judge_single_b_07",
            {
                **STREAM_BODY(
                    model=model,
                    messages=final_messages(
                        [(model, ans_b)], crit_single_b, DEFAULT_JUDGE_TEMPLATE
                    ),
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


# ---- ICE 场景（T34）：多轮依赖链，录制器真实走完链路（逐字节复用 ice.py 渲染函数） ------

# 题目选择（§7.2 重录注意：链形态由真实 LLM 的 consensus 决定，不符则改题重录）：
# r0_consensus 要两模型大概率一致（简单事实题）；refine 要首轮分歧、改进后收敛；
# max_rounds 用 threshold=1.0 使共识不可达、轮数必然耗尽。
ICE_R0_QUESTION = "水的化学式是什么？请只回答化学式本身。"
ICE_REFINE_QUESTION = "《静夜思》「床前明月光」中的「床」指的是什么？请给出你的判断并简要说明理由。"
ICE_MAXR_QUESTION = "豆腐脑甜的正宗还是咸的正宗？请给出你的判断并说明理由。"
ICE_MEMBER_TEMPS = (0.7, 0.9)  # 与 AE 种子的成员 param_overrides 一致


def _ice_member_body(model: str, messages: list[dict], temperature: float) -> dict:
    return {**STREAM_BODY(model=model, messages=messages), "temperature": temperature}


def ice_chain_requests(
    send,
    model: str,
    scenario: str,
    question: str,
    strategy_params: dict,
) -> list[str]:
    """按 IceStrategy.run 的链路逐步构造请求并发送（send 返回 (status, 全文)）。

    与运行时逐字节一致的保证：渲染函数/解析器/终局条件全部 import 自 ice.py。
    成员调用失败（注入）时沿用上轮答案继续，与 §5 容错矩阵一致。
    返回各成员最新成功轮答案（供 rejudge 尾部场景复用）。
    """
    from app.strategies.council import (
        DEFAULT_JUDGE_TEMPLATE,
        render_final_instruction,
        render_judge_prompt,
    )
    from app.strategies.ice import (
        ICE_CRITIQUE_TEMPLATE,
        ICE_REFINE_TEMPLATE,
        ICE_ROUND_CRITIQUE_TEMPLATE,
        IceStrategy,
        parse_verdict,
    )

    params = IceStrategy.validate_params(strategy_params)
    messages = [{"role": "user", "content": question}]
    answers: list[str] = [""] * len(ICE_MEMBER_TEMPS)

    def member_round(round_no: int, critique: str | None) -> None:
        for i, temp in enumerate(ICE_MEMBER_TEMPS):
            if critique is None:
                member_messages = messages
            else:
                refine_prompt = render_judge_prompt(
                    ICE_REFINE_TEMPLATE, messages, [], critique=critique
                )
                member_messages = [
                    *messages,
                    {"role": "assistant", "content": answers[i]},
                    {"role": "user", "content": refine_prompt},
                ]
            status, content = send(
                f"{scenario}_m{i}_r{round_no}",
                _ice_member_body(model, member_messages, temp),
            )
            if status == 200:
                answers[i] = content
            elif round_no == 0:
                raise AssertionError(f"{scenario} member m{i} r0 failed: {content[:200]}")

    member_round(0, None)
    session: list[dict] = []  # 仲裁者会话（D10）
    verdict = None
    round_no = 0
    conf_history: list[float] = []
    critique_text = ""
    while True:
        template = ICE_CRITIQUE_TEMPLATE if not session else ICE_ROUND_CRITIQUE_TEMPLATE
        prompt = render_judge_prompt(template, messages, [(model, a) for a in answers])
        user_message = {"role": "user", "content": prompt}
        status, content = send(
            f"{scenario}_critique_r{round_no}",
            STREAM_BODY(model=model, messages=[*session, user_message]),
        )
        assert status == 200, f"{scenario} critique r{round_no} failed: {content[:200]}"
        session.extend((user_message, {"role": "assistant", "content": content}))
        critique_text = content
        verdict = parse_verdict(content)
        print(
            f"[{scenario}] critique r{round_no}: consensus={verdict.consensus} "
            f"confidence={verdict.confidence} parsed={verdict.parsed}"
        )
        if round_no == 0:
            conf_history = [verdict.confidence] if verdict.parsed else []
        elif verdict.parsed:
            conf_history.append(verdict.confidence)
        if IceStrategy._should_finish(verdict, round_no, conf_history, params):
            break
        round_no += 1
        member_round(round_no, verdict.critique)

    instruction = render_final_instruction(
        DEFAULT_JUDGE_TEMPLATE, messages, [(model, a) for a in answers], critique_text
    )
    status, _ = send(
        f"{scenario}_final",
        STREAM_BODY(model=model, messages=[*session, {"role": "user", "content": instruction}]),
    )
    assert status == 200, f"{scenario} final failed"
    return answers


def ice_rejudge_requests(
    send, model: str, scenario: str, question: str, answers: list[str]
) -> None:
    """ICE trace 换裁判重跑的两段请求：council 自由文本评论模板 + pipeline 终局模板。"""
    from app.strategies.council import (
        DEFAULT_CRITIQUE_TEMPLATE,
        DEFAULT_JUDGE_TEMPLATE,
        build_final_messages,
        render_final_instruction,
        render_judge_prompt,
    )

    question_messages = [{"role": "user", "content": question}]
    answer_pairs = [(model, a) for a in answers]
    prompts = render_judge_prompt(DEFAULT_CRITIQUE_TEMPLATE, question_messages, answer_pairs)
    critique_messages = [{"role": "user", "content": prompts}]
    status, critique = send(
        f"{scenario}_rejudge_critique", STREAM_BODY(model=model, messages=critique_messages)
    )
    assert status == 200
    instruction = render_final_instruction(
        DEFAULT_JUDGE_TEMPLATE, question_messages, answer_pairs, critique
    )
    status, _ = send(
        f"{scenario}_rejudge_judge",
        STREAM_BODY(
            model=model,
            messages=build_final_messages(critique_messages, critique, instruction),
        ),
    )
    assert status == 200


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


# T43 工具透传场景的确定性常量：录制器与 AE 用例共用，保证请求体逐字节一致
TOOL_QUESTION = "广州现在多少度？请用工具查询实时天气"
TOOL_WEATHER = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市当前的实时天气（温度、天气现象）",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名，如 广州"},
                    "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                },
                "required": ["city"],
            },
        },
    }
]


def tool_scenarios(model: str) -> list[tuple[str, dict]]:
    """T43 工具透传：确定性请求（温度 0 + 强工具相关性问题）→ 上游稳定产出 tool_calls。"""
    body = {
        **STREAM_BODY(
            model=model,
            messages=[{"role": "user", "content": TOOL_QUESTION}],
        ),
        "temperature": 0.0,
        "tools": TOOL_WEATHER,
        "tool_choice": "auto",
    }
    return [("passthrough_tool_calls", body)]


def _tool_intent(model: str, scenario: str) -> str:
    """快照成员输出 → 运行时同构的意图文本（复用 council.format_tool_intent 保证逐字节一致）。

    content 与 tool_calls 都取自快照：混合输出（文本+调用）的成员意图带"附说明"部分，
    与运行时 format_tool_intent 的输入形态完全一致。
    """
    from app.strategies.base import CallOutcome
    from app.strategies.council import format_tool_intent
    from tests.helpers import snapshot_content, snapshot_tool_calls

    return format_tool_intent(
        CallOutcome(
            role="member",
            model_id=0,
            upstream_model_id=model,
            provider_name="",
            request_payload={},
            response_content=snapshot_content(scenario),
            response_tool_calls=snapshot_tool_calls(scenario),
        )
    )


def tool_council_member_scenarios(model: str) -> list[tuple[str, dict]]:
    """T44 工具 council 成员（无参数变体，与 passthrough_tool_calls 的温度 0 变体互补）。"""
    return [
        (
            "tool_council_member",
            {
                **STREAM_BODY(
                    model=model,
                    messages=[{"role": "user", "content": TOOL_QUESTION}],
                ),
                "tools": TOOL_WEATHER,
                "tool_choice": "auto",
            },
        )
    ]


def tool_council_critique_scenarios(model: str) -> list[tuple[str, dict]]:
    """T44 工具 council 评论段：输入=两成员调用意图文本（快照聚合，须在成员场景录制后运行）。"""
    from app.strategies.council import DEFAULT_CRITIQUE_TEMPLATE, render_judge_prompt

    question = [{"role": "user", "content": TOOL_QUESTION}]
    intents = [
        (model, _tool_intent(model, "tool_council_member")),
        (model, _tool_intent(model, "passthrough_tool_calls")),
    ]
    prompt = render_judge_prompt(DEFAULT_CRITIQUE_TEMPLATE, question, intents)
    return [
        (
            "tool_council_critique",
            {
                **STREAM_BODY(model=model, messages=[{"role": "user", "content": prompt}]),
                "temperature": 0.0,
            },
        )
    ]


def tool_council_final_scenarios(model: str) -> list[tuple[str, dict]]:
    """T44 工具 council 终局：同会话 + 内置工具合成指令 + tools（裁判产出合成调用）。"""
    from app.strategies.council import (
        DEFAULT_CRITIQUE_TEMPLATE,
        DEFAULT_TOOL_JUDGE_TEMPLATE,
        build_final_messages,
        render_final_instruction,
        render_judge_prompt,
    )

    question = [{"role": "user", "content": TOOL_QUESTION}]
    intents = [
        (model, _tool_intent(model, "tool_council_member")),
        (model, _tool_intent(model, "passthrough_tool_calls")),
    ]
    critique_prompt = render_judge_prompt(DEFAULT_CRITIQUE_TEMPLATE, question, intents)
    critique_text = snapshot_content("tool_council_critique")
    instruction = render_final_instruction(
        DEFAULT_TOOL_JUDGE_TEMPLATE, question, intents, critique_text
    )
    messages = build_final_messages(
        [{"role": "user", "content": critique_prompt}], critique_text, instruction
    )
    return [
        (
            "tool_council_final",
            {
                **STREAM_BODY(model=model, messages=messages),
                "temperature": 0.0,
                "tools": TOOL_WEATHER,
                "tool_choice": "auto",
            },
        )
    ]


def ice_scenario_plan() -> list[tuple[str, str, dict]]:
    """§7.2 非流式 ICE 场景：(scenario, question, strategy_params)。"""
    return [
        ("ice_r0_consensus", ICE_R0_QUESTION, {}),
        ("ice_refine_consensus", ICE_REFINE_QUESTION, {}),
        ("ice_max_rounds", ICE_MAXR_QUESTION, {"max_rounds": 2, "confidence_threshold": 1.0}),
    ]


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
            url = f"http://127.0.0.1:{port}/v1/chat/completions"

            def send_stream(name: str, body: dict) -> tuple[int, str]:
                """流式请求 SRS，返回 (status, delta 拼接全文)；统计录制/回放。"""
                nonlocal recorded, replayed

                if store.get(request_hash(body)) is not None:
                    replayed += 1
                else:
                    recorded += 1
                parts: list[str] = []
                with client.stream(
                    "POST", url, json=body, headers={"X-Srs-Scenario": name}
                ) as resp:
                    status = resp.status_code
                    for line in resp.iter_lines():
                        if line.startswith("data: ") and line != "data: [DONE]":
                            chunk = json.loads(line[len("data: ") :])
                            parts.append(
                                (chunk.get("choices") or [{}])[0].get("delta", {}).get("content")
                                or ""
                            )
                text = "".join(parts)
                print(f"[{name}] {status}: {text[:100]!r}")
                return status, text

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
            run_scenarios(tool_scenarios(model))
            run_scenarios(council_critique_scenarios(model))
            run_scenarios(council_final_scenarios(model))
            # T44 工具 council 依赖链：成员 → 评论（读成员意图）→ 终局（读评论全文）
            run_scenarios(tool_council_member_scenarios(model))
            run_scenarios(tool_council_critique_scenarios(model))
            run_scenarios(tool_council_final_scenarios(model))

            # ICE 多轮依赖链（§7.2）：录制器真实走完链路，链形态由真实 verdict 决定
            refine_answers: list[str] = []
            for scenario, question, sp in ice_scenario_plan():
                answers = ice_chain_requests(send_stream, model, scenario, question, sp)
                if scenario == "ice_refine_consensus":
                    refine_answers = answers
            ice_rejudge_requests(
                send_stream, model, "ice_refine_consensus", ICE_REFINE_QUESTION, refine_answers
            )

            # AE-34-5 容错链：第 1 轮第二个成员注入失败（录制器单次尝试不重试，序号 5；
            # 运行时适配器 max_retries=1 会多打一次，AE 侧对应 fail_at_calls [5, 6]），
            # 评论/终局面对 (m0 新答案, m1 旧答案) 的变体需单独录制；max_rounds=2 保证必然终局
            client.post(f"http://127.0.0.1:{port}/_test/config", json={"reset": True})
            client.post(
                f"http://127.0.0.1:{port}/_test/config",
                json={"fail_at_calls": {model: [5]}},
            )
            ice_chain_requests(
                send_stream,
                model,
                "ice_max_rounds_mbfail",
                ICE_MAXR_QUESTION,
                {"max_rounds": 2, "confidence_threshold": 1.0},
            )
            client.post(f"http://127.0.0.1:{port}/_test/config", json={"reset": True})
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    after = len(SnapshotStore(SNAPSHOTS_DIR))
    print(f"done: recorded={recorded} replayed={replayed}, snapshots {before} -> {after}")


if __name__ == "__main__":
    main()
