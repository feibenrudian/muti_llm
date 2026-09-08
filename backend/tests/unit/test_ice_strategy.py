"""UT-33-1..10：ICE 策略非流式执行核心（打桩适配器按调用序返回预设 JSON/失败，不起网络）。"""

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from app.adapters.base import (
    AdapterError,
    BaseAdapter,
    LlmRequest,
    LlmUsage,
    StreamEvent,
)
from app.orm import LlmModel, Pipeline, PipelineMember, Provider
from app.strategies.base import MemberSpec, StrategyContext, StrategyExecutionError
from app.strategies.council import render_judge_prompt
from app.strategies.ice import (
    ICE_CRITIQUE_TEMPLATE,
    ICE_REFINE_TEMPLATE,
    ICE_ROUND_CRITIQUE_TEMPLATE,
    IceStrategy,
    parse_verdict,
)

MESSAGES = [
    {"role": "system", "content": "you are helpful"},
    {"role": "user", "content": "hi"},
]

Behavior = Callable[[LlmRequest], AsyncIterator[StreamEvent]]


class SeqAdapter(BaseAdapter):
    """按调用序消费预设行为；每次调用记录请求供断言。"""

    def __init__(self, behaviors: list[Behavior]) -> None:
        self.behaviors = list(behaviors)
        self.requests: list[LlmRequest] = []

    def stream_events(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        if not self.behaviors:
            raise AssertionError("桩行为队列耗尽，存在计划外调用")
        return self.behaviors.pop(0)(request)

    async def probe(self) -> list[str]:  # pragma: no cover - 策略不使用
        raise NotImplementedError


def ok(content: str, prompt: int = 1, completion: int = 1) -> Behavior:
    async def behavior(request: LlmRequest) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(text=content)
        yield StreamEvent(usage=LlmUsage(prompt_tokens=prompt, completion_tokens=completion))

    return behavior


def fail(kind: str = "upstream_error") -> Behavior:
    async def behavior(request: LlmRequest) -> AsyncIterator[StreamEvent]:
        raise AdapterError(f"stub failure ({kind})", kind=kind, retryable=False)
        yield StreamEvent(text="")  # pragma: no cover - 使其成为 async generator

    return behavior


def verdict_json(consensus: bool, confidence: float, critique: str = "评论") -> str:
    return json.dumps(
        {"consensus": consensus, "confidence": confidence, "critique": critique},
        ensure_ascii=False,
    )


def make_ctx(
    n_members: int = 2,
    *,
    strategy_params: dict[str, Any] | None = None,
    user_params: dict[str, Any] | None = None,
    fault_tolerance: dict[str, Any] | None = None,
    judge_prompt_template: str = "",
) -> StrategyContext:
    pipeline = Pipeline(
        id=1,
        name="ice-v1",
        strategy="ice",
        strategy_params=strategy_params or {},
        judge_model_id=99,
        judge_prompt_template=judge_prompt_template,
        member_timeout_seconds=60,
        fault_tolerance=fault_tolerance or {},
        max_concurrency=10,
    )
    members = []
    for i in range(n_members):
        model = LlmModel(
            id=i + 1,
            provider_id=i + 1,
            display_name=f"m{i + 1}",
            upstream_model_id=f"model-{i + 1}",
            default_params={},
        )
        members.append(
            MemberSpec(
                member=PipelineMember(
                    id=i + 1, pipeline_id=1, model_id=model.id, sort_order=i, param_overrides={}
                ),
                model=model,
                provider=Provider(
                    id=i + 1, name=f"P{i + 1}", protocol="openai_compatible", base_url="http://x"
                ),
            )
        )
    return StrategyContext(
        body={"model": "ice-v1", "messages": MESSAGES},
        user_params=user_params or {},
        pipeline=pipeline,
        members=members,
        judge_model=LlmModel(
            id=99,
            provider_id=99,
            display_name="judge",
            upstream_model_id="judge-m",
            default_params={},
        ),
        judge_provider=Provider(
            id=99, name="judge-p", protocol="openai_compatible", base_url="http://x"
        ),
        fernet_key=b"k" * 44,
    )


def member_factory(adapters: dict[str, SeqAdapter]) -> Callable[..., BaseAdapter]:
    """按 provider.name 分发成员桩适配器（每成员一个，跨轮共享调用队列）。"""

    def factory(provider: Provider, merged: dict[str, Any]) -> BaseAdapter:
        return adapters[provider.name]

    return factory


async def test_round0_consensus_fast_path() -> None:
    """UT-33-1 第0轮即共识：桩评论 JSON consensus=true conf=0.9
    → 总调用 M+2，无 refine 轮，非降级。"""
    ctx = make_ctx(2)
    members = {"P1": SeqAdapter([ok("a0")]), "P2": SeqAdapter([ok("b0")])}
    judge = SeqAdapter([ok(verdict_json(True, 0.9)), ok("最终答案")])

    result = await IceStrategy().run(
        ctx, adapter_factory=member_factory(members), judge_adapter=judge
    )

    assert len(result.calls) == 4  # M + 评论 + 终局
    assert [c.role for c in result.calls] == ["member", "member", "judge_critique", "judge"]
    assert [c.round_no for c in result.calls] == [0, 0, 0, None]
    assert result.final_content == "最终答案"
    assert result.degraded is False
    assert all(c.status == "success" for c in result.calls)
    assert not judge.behaviors and not members["P1"].behaviors and not members["P2"].behaviors


async def test_refine_then_consensus() -> None:
    """UT-33-2 迭代后共识：refine 轮成员消息 = 原始+assistant(旧答案)
    +user(改进指令，{{critique}} 已渲染)。"""
    ctx = make_ctx(2)
    members = {"P1": SeqAdapter([ok("a0"), ok("a1")]), "P2": SeqAdapter([ok("b0"), ok("b1")])}
    judge = SeqAdapter(
        [
            ok(verdict_json(False, 0.5, critique="回答1遗漏细节")),
            ok(verdict_json(True, 0.9)),
            ok("终局"),
        ]
    )

    result = await IceStrategy().run(
        ctx, adapter_factory=member_factory(members), judge_adapter=judge
    )

    assert len(result.calls) == 7  # 成员×2轮 + 评论×2轮 + 终局
    assert [c.round_no for c in result.calls] == [0, 0, 0, 1, 1, 1, None]
    expected_refine = render_judge_prompt(
        ICE_REFINE_TEMPLATE, MESSAGES, [], critique="回答1遗漏细节"
    )
    for adapter, prev in ((members["P1"], "a0"), (members["P2"], "b0")):
        refine_request = adapter.requests[1]
        got = [(m.role, m.content) for m in refine_request.messages]
        expected = [(m["role"], m["content"]) for m in MESSAGES] + [
            ("assistant", prev),
            ("user", expected_refine),
        ]
        assert got == expected
        assert "回答1遗漏细节" in refine_request.messages[-1].content  # {{critique}} 已渲染
    assert result.final_content == "终局"
    assert result.degraded is False


async def test_anonymity_in_critique() -> None:
    """UT-33-3 匿名性：仲裁评论输入为【回答 N】且不带模型名（复用 render_judge_prompt 断言）。"""
    ctx = make_ctx(2)
    members = {"P1": SeqAdapter([ok("a0")]), "P2": SeqAdapter([ok("b0")])}
    judge = SeqAdapter([ok(verdict_json(True, 0.9)), ok("终局")])

    await IceStrategy().run(ctx, adapter_factory=member_factory(members), judge_adapter=judge)

    critique_prompt = judge.requests[0].messages[0].content
    expected = render_judge_prompt(
        ICE_CRITIQUE_TEMPLATE, MESSAGES, [("model-1", "a0"), ("model-2", "b0")]
    )
    assert critique_prompt == expected
    assert "【回答 1】" in critique_prompt and "【回答 2】" in critique_prompt
    for leaked in ("model-1", "model-2", "P1", "P2"):
        assert leaked not in critique_prompt


async def test_judge_session_continuation() -> None:
    """UT-33-4 仲裁者会话延续：第 k 轮评论请求含第 k-1 轮 JSON 为 assistant 轮；
    user 轮只含最新答案、不重复原对话。user_params 作用于全部 R+1 次裁判调用。"""
    ctx = make_ctx(2, user_params={"temperature": 0.3})
    members = {"P1": SeqAdapter([ok("a0"), ok("a1")]), "P2": SeqAdapter([ok("b0"), ok("b1")])}
    verdict0 = verdict_json(False, 0.5)
    verdict1 = verdict_json(True, 0.9)
    judge = SeqAdapter([ok(verdict0), ok(verdict1), ok("终局")])

    await IceStrategy().run(ctx, adapter_factory=member_factory(members), judge_adapter=judge)

    critique0, critique1, final = judge.requests
    assert [(m.role,) for m in critique0.messages] == [("user",)]
    assert len(critique1.messages) == 3
    assert critique1.messages[0].content == critique0.messages[0].content
    assert (critique1.messages[1].role, critique1.messages[1].content) == ("assistant", verdict0)
    round_user = critique1.messages[2]
    assert round_user.role == "user"
    expected_round = render_judge_prompt(
        ICE_ROUND_CRITIQUE_TEMPLATE, MESSAGES, [("model-1", "a1"), ("model-2", "b1")]
    )
    assert round_user.content == expected_round
    assert "【用户对话】" not in round_user.content  # 原对话不重复
    assert "a0" not in round_user.content and "b0" not in round_user.content  # 只带最新答案
    # 终局：同会话续轮 + 指令轮
    assert [m.role for m in final.messages] == ["user", "assistant", "user", "assistant", "user"]
    assert final.messages[3].content == verdict1  # 第 1 轮 JSON 也入会话
    # user_params 作用于全部裁判调用、不作用于成员
    assert all(r.temperature == 0.3 for r in judge.requests)
    assert all(r.temperature is None for a in members.values() for r in a.requests)


async def test_max_rounds_exhausted() -> None:
    """UT-33-5 轮数耗尽：stagnation=false、confidence 逐轮递增但低于阈值、恒 false
    → 恰好 max_rounds 轮成员 + 等量评论 + 1 终局，degraded=false。"""
    ctx = make_ctx(2, strategy_params={"max_rounds": 3, "stagnation": False})
    members = {
        "P1": SeqAdapter([ok("a0"), ok("a1"), ok("a2")]),
        "P2": SeqAdapter([ok("b0"), ok("b1"), ok("b2")]),
    }
    judge = SeqAdapter(
        [
            ok(verdict_json(False, 0.3)),
            ok(verdict_json(False, 0.4)),
            ok(verdict_json(False, 0.5)),
            ok("终局"),
        ]
    )

    result = await IceStrategy().run(
        ctx, adapter_factory=member_factory(members), judge_adapter=judge
    )

    # 3 轮成员×2 + 3 轮评论 + 1 终局
    assert len(result.calls) == 10
    member_rounds = [c.round_no for c in result.calls if c.role == "member"]
    critique_rounds = [c.round_no for c in result.calls if c.role == "judge_critique"]
    assert member_rounds == [0, 0, 1, 1, 2, 2]
    assert critique_rounds == [0, 1, 2]
    assert result.calls[-1].role == "judge" and result.calls[-1].round_no is None
    assert result.degraded is False
    assert result.final_content == "终局"

    # max_rounds=1 退化：未共识也直接终局，调用数等同 council（M+2）
    ctx = make_ctx(2, strategy_params={"max_rounds": 1})
    members = {"P1": SeqAdapter([ok("a0")]), "P2": SeqAdapter([ok("b0")])}
    judge = SeqAdapter([ok(verdict_json(False, 0.1)), ok("终局")])
    result = await IceStrategy().run(
        ctx, adapter_factory=member_factory(members), judge_adapter=judge
    )
    assert len(result.calls) == 4
    assert [c.role for c in result.calls] == ["member", "member", "judge_critique", "judge"]
    assert result.degraded is False


async def test_stagnation_triggers_early_finish() -> None:
    """UT-33-6 停滞检测：conf 序列 [0.5, 0.5] 两轮未终局 → 提前终局，调用数少于 max_rounds 满轮。"""
    ctx = make_ctx(2, strategy_params={"max_rounds": 3})  # stagnation 默认 true
    members = {
        "P1": SeqAdapter([ok("a0"), ok("a1"), ok("a2-不应被调用")]),
        "P2": SeqAdapter([ok("b0"), ok("b1"), ok("b2-不应被调用")]),
    }
    judge = SeqAdapter([ok(verdict_json(False, 0.5)), ok(verdict_json(False, 0.5)), ok("终局")])

    result = await IceStrategy().run(
        ctx, adapter_factory=member_factory(members), judge_adapter=judge
    )

    # 满轮应为 10 次调用；停滞提前终局 → 2 成员轮 + 2 评论 + 终局 = 7
    assert len(result.calls) == 7
    assert result.degraded is False
    assert result.final_content == "终局"
    assert len(members["P1"].requests) == 2  # 未进入第 2 轮


async def test_lenient_json_parsing() -> None:
    """UT-33-7 JSON 宽松解析：围栏/前后杂文可解析；
    完全非 JSON → consensus=false、critique=原文、继续迭代。"""
    fenced = '```json\n{"consensus": true, "confidence": 0.9, "critique": "c"}\n```'
    verdict = parse_verdict(fenced)
    assert (verdict.consensus, verdict.confidence, verdict.critique, verdict.parsed) == (
        True,
        0.9,
        "c",
        True,
    )
    messy = '前言杂文 {"consensus": false, "confidence": 0.4, "critique": "含{括号}"} 后记'
    verdict = parse_verdict(messy)
    assert (verdict.consensus, verdict.confidence, verdict.critique, verdict.parsed) == (
        False,
        0.4,
        "含{括号}",
        True,
    )
    garbage = "完全不是 JSON 的评论"
    verdict = parse_verdict(garbage)
    assert (verdict.consensus, verdict.confidence, verdict.critique, verdict.parsed) == (
        False,
        0.0,
        garbage,
        False,
    )
    # 字段缺省容错
    verdict = parse_verdict('{"confidence": "x"}')
    assert (verdict.consensus, verdict.confidence, verdict.critique, verdict.parsed) == (
        False,
        0.0,
        '{"confidence": "x"}',
        True,
    )

    # 集成：第 0 轮评论完全非 JSON → 用原文作 critique 继续迭代，下一轮共识
    ctx = make_ctx(2)
    members = {"P1": SeqAdapter([ok("a0"), ok("a1")]), "P2": SeqAdapter([ok("b0"), ok("b1")])}
    judge = SeqAdapter([ok(garbage), ok(verdict_json(True, 0.9)), ok("终局")])

    result = await IceStrategy().run(
        ctx, adapter_factory=member_factory(members), judge_adapter=judge
    )
    assert len(result.calls) == 7  # 发生了 refine 轮
    assert garbage in members["P1"].requests[1].messages[-1].content  # critique=原文进改进指令
    assert result.final_content == "终局"
    assert result.degraded is False


async def test_member_refine_round_failure() -> None:
    """UT-33-8 成员迭代轮失败：第 1 轮一成员失败 → 沿用第 0 轮答案进评论、失败行保留(round=1)；
    第 1 轮全失败 → 沿用全部上轮答案不 502。"""
    # 一成员失败
    ctx = make_ctx(2, strategy_params={"max_rounds": 2, "stagnation": False})
    members = {"P1": SeqAdapter([ok("a0"), fail()]), "P2": SeqAdapter([ok("b0"), ok("b1")])}
    judge = SeqAdapter([ok(verdict_json(False, 0.5)), ok(verdict_json(True, 0.9)), ok("终局")])

    result = await IceStrategy().run(
        ctx, adapter_factory=member_factory(members), judge_adapter=judge
    )

    failed = [c for c in result.calls if c.role == "member" and c.status == "failed"]
    assert len(failed) == 1 and failed[0].round_no == 1
    round1_prompt = judge.requests[1].messages[-1].content
    assert "【回答 1】\na0" in round1_prompt  # 失败成员沿用第 0 轮答案
    assert "【回答 2】\nb1" in round1_prompt  # 成功成员用新答案
    assert result.final_content == "终局"

    # 全部成员第 1 轮失败 → 沿用全部上轮答案，不 502
    ctx = make_ctx(2, strategy_params={"max_rounds": 2, "stagnation": False})
    members = {"P1": SeqAdapter([ok("a0"), fail()]), "P2": SeqAdapter([ok("b0"), fail()])}
    judge = SeqAdapter([ok(verdict_json(False, 0.5)), ok(verdict_json(True, 0.9)), ok("终局")])

    result = await IceStrategy().run(
        ctx, adapter_factory=member_factory(members), judge_adapter=judge
    )
    round1_prompt = judge.requests[1].messages[-1].content
    assert "【回答 1】\na0" in round1_prompt and "【回答 2】\nb0" in round1_prompt
    assert sum(1 for c in result.calls if c.status == "failed") == 2
    assert result.final_content == "终局"


async def test_critique_failure_paths() -> None:
    """UT-33-9 评论失败：第 0 轮评论失败 → 降级首个成功成员/strict 抛错；
    第 1 轮评论失败 → 终局裁决 degraded=true。"""
    # 第 0 轮评论失败（默认容错）→ 降级首个成功成员答案
    ctx = make_ctx(2)
    members = {"P1": SeqAdapter([ok("a0")]), "P2": SeqAdapter([ok("b0")])}
    judge = SeqAdapter([fail()])
    result = await IceStrategy().run(
        ctx, adapter_factory=member_factory(members), judge_adapter=judge
    )
    assert result.degraded is True
    assert result.final_content == "a0"
    assert [c.role for c in result.calls] == ["member", "member", "judge_critique"]
    assert result.calls[-1].status == "failed"

    # 第 0 轮评论失败 + judge_failure=strict → 抛错
    ctx = make_ctx(2, fault_tolerance={"judge_failure": "strict"})
    members = {"P1": SeqAdapter([ok("a0")]), "P2": SeqAdapter([ok("b0")])}
    judge = SeqAdapter([fail()])
    with pytest.raises(StrategyExecutionError):
        await IceStrategy().run(ctx, adapter_factory=member_factory(members), judge_adapter=judge)

    # 第 1 轮评论失败 → 跳过剩余迭代，直接终局裁决，degraded=true
    ctx = make_ctx(2, strategy_params={"max_rounds": 3})
    members = {"P1": SeqAdapter([ok("a0"), ok("a1")]), "P2": SeqAdapter([ok("b0"), ok("b1")])}
    judge = SeqAdapter([ok(verdict_json(False, 0.5)), fail(), ok("终局")])
    result = await IceStrategy().run(
        ctx, adapter_factory=member_factory(members), judge_adapter=judge
    )
    assert result.degraded is True
    assert result.final_content == "终局"
    assert [c.role for c in result.calls] == [
        "member",
        "member",
        "judge_critique",
        "member",
        "member",
        "judge_critique",
        "judge",
    ]
    assert result.calls[-2].status == "failed" and result.calls[-2].round_no == 1


async def test_usage_aggregation() -> None:
    """UT-33-10 usage 汇总：全部调用（成员各轮+评论各轮+终局）token 求和。"""
    ctx = make_ctx(2)
    members = {
        "P1": SeqAdapter([ok("a0", 10, 5), ok("a1", 20, 6)]),
        "P2": SeqAdapter([ok("b0", 30, 7), ok("b1", 40, 8)]),
    }
    judge = SeqAdapter(
        [
            ok(verdict_json(False, 0.5), 50, 9),
            ok(verdict_json(True, 0.9), 60, 10),
            ok("终局", 70, 11),
        ]
    )

    result = await IceStrategy().run(
        ctx, adapter_factory=member_factory(members), judge_adapter=judge
    )

    assert len(result.calls) == 7
    assert result.usage.prompt_tokens == 10 + 20 + 30 + 40 + 50 + 60 + 70
    assert result.usage.completion_tokens == 5 + 6 + 7 + 8 + 9 + 10 + 11
    # 与逐条 outcome 求和一致（含终局）
    assert result.usage.prompt_tokens == sum(c.usage.prompt_tokens for c in result.calls)
    assert result.usage.completion_tokens == sum(c.usage.completion_tokens for c in result.calls)
