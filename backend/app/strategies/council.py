"""council 策略：成员并发回答 → 两段式裁判（先评论各答案优劣，再产出最终答案）。

录制脚本（tests/record_scenarios.py）直接 import 本模块的渲染函数构造两段裁判请求，
保证"录制的裁判 Prompt"与"运行时组装的裁判 Prompt"逐字节一致（快照命中前提）。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from app.adapters.base import (
    AdapterError,
    BaseAdapter,
    LlmRequest,
    LlmUsage,
)
from app.adapters.factory import build_adapter
from app.orm import Provider
from app.strategies.base import (
    AllMembersFailed,
    CallOutcome,
    FinalPlanReady,
    MemberSpec,
    ProcessEvent,
    Strategy,
    StrategyContext,
    StrategyExecutionError,
    StrategyResult,
    StreamPlan,
    register_strategy,
)

DEFAULT_CRITIQUE_TEMPLATE = """你将看到用户的问题，以及多个 AI 模型分别给出的回答。
请逐个评价每个回答的优劣：指出事实错误、关键信息遗漏、逻辑或表述问题，并给出简短的可信度结论。
不要输出最终答案，只输出对各回答的评论。

【用户对话】
{{original_messages}}

【各模型回答】
{{candidate_answers}}

请按【回答 1】【回答 2】… 的顺序逐条评论，不要复述回答原文。"""

DEFAULT_JUDGE_TEMPLATE = "请结合你上面给出的评论，鉴别各回答的可信度，给出一个比任何单个回答都更准确、完整的最终回答。直接输出最终回答，不要复述过程。"

_PLACEHOLDER = re.compile(r"\{\{(original_messages|candidate_answers|critique)\}\}")


# ---- T15 裁判 Prompt --------------------------------------------------------------


def serialize_messages(messages: list[dict[str, Any]]) -> str:
    """多轮消息序列化为 [role] content 行（完整保留 system/user/assistant）。"""
    return "\n".join(f"[{m.get('role')}] {m.get('content')}" for m in messages)


def render_judge_prompt(
    template: str,
    messages: list[dict[str, Any]],
    answers: list[tuple[str, str]],
    *,
    critique: str = "",
) -> str:
    """渲染裁判 Prompt。单遍替换占位符——答案文本里出现的占位符字样不会被二次展开。

    候选答案只标序号不带模型名（匿名，防止裁判偏袒自己模型的答案）。
    """
    values = {
        "original_messages": serialize_messages(messages),
        "candidate_answers": "\n\n".join(
            f"【回答 {i}】\n{content}" for i, (_model, content) in enumerate(answers, start=1)
        ),
        "critique": critique,
    }
    parts: list[str] = []
    last = 0
    for match in _PLACEHOLDER.finditer(template):
        parts.append(template[last : match.start()])
        parts.append(values[match.group(1)])
        last = match.end()
    parts.append(template[last:])
    return "".join(parts)


def render_final_instruction(
    template: str,
    messages: list[dict[str, Any]],
    answers: list[tuple[str, str]],
    critique: str,
) -> str:
    """渲染第二次调用的最终指令（模板默认只写指令；占位符可选用——原始对话与答案已在第一轮）。"""
    return render_judge_prompt(template, messages, answers, critique=critique)


def build_final_messages(
    critique_messages: list[dict[str, str]],
    critique: str,
    instruction: str,
) -> list[dict[str, str]]:
    """第二次调用消息：与第一次同会话——第一次输入 → 评论(assistant) → 最终指令。"""
    return [
        *critique_messages,
        {"role": "assistant", "content": critique},
        {"role": "user", "content": instruction},
    ]


# ---- 参数合并与请求构造 --------------------------------------------------------------
# 成员：模型默认 < 成员覆盖——客户端请求参数不作用于成员，只透传给裁判；
# 裁判：模型默认 < 请求参数。


def merge_params(*layers: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for layer in layers:
        if layer:
            merged.update(layer)
    return merged


def build_call_request(
    upstream_model_id: str,
    openai_messages: list[dict[str, str]],
    merged: dict[str, Any],
    *,
    stream: bool,
) -> tuple[LlmRequest, dict[str, Any]]:
    """归一化请求 + 实际发出的 payload（入日志；形态与快照 hash 一致）。"""
    payload: dict[str, Any] = {
        "model": upstream_model_id,
        "messages": openai_messages,
        "stream": stream,
    }
    request = LlmRequest(model=upstream_model_id, messages=[])
    from app.adapters.base import NormalizedMessage

    request.messages = [
        NormalizedMessage(role=m["role"], content=m["content"]) for m in openai_messages
    ]
    for key in ("temperature", "max_tokens", "top_p"):
        if merged.get(key) is not None:
            payload[key] = merged[key]
            setattr(request, key, merged[key])
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return request, payload


# ---- T14 成员并发执行器 ------------------------------------------------------------

AdapterFactory = Callable[[Provider, dict[str, Any]], BaseAdapter]


def _failures_summary(outcomes: list[CallOutcome]) -> str:
    return "; ".join(
        f"{o.upstream_model_id}: {o.error_message or o.status}"
        for o in outcomes
        if o.status != "success"
    )


def sum_usage(outcomes: list[CallOutcome]) -> LlmUsage:
    usage = LlmUsage()
    for o in outcomes:
        usage.prompt_tokens += o.usage.prompt_tokens
        usage.completion_tokens += o.usage.completion_tokens
    return usage


def default_member_adapter_factory(
    ctx: StrategyContext,
) -> AdapterFactory:
    """成员适配器工厂（默认）：member_timeout_seconds 为 TTFT/块间空闲超时预算。"""

    member_timeout = float(ctx.pipeline.member_timeout_seconds or 120)

    def factory(provider: Provider, merged: dict[str, Any]) -> BaseAdapter:
        return build_adapter(
            provider,
            fernet_key=ctx.fernet_key,
            timeout_seconds=member_timeout,
            max_retries=int(merged.get("max_retries", 1)),
        )

    return factory


async def run_one(
    ctx: StrategyContext,
    spec: MemberSpec,
    adapter_factory: AdapterFactory,
    semaphore: asyncio.Semaphore,
) -> CallOutcome:
    """单成员调用：构造请求 → 限流执行 → 成败均回填 CallOutcome（不抛错）。

    上游一律流式调用（决策 D8）：member_timeout_seconds 是成员适配器的
    TTFT/块间空闲超时预算——复杂问题生成再久也不算超时，只有等不到上游数据才超时。
    """
    merged = merge_params(spec.model.default_params, spec.member.param_overrides)
    request, payload = build_call_request(
        spec.model.upstream_model_id, ctx.body["messages"], merged, stream=True
    )
    outcome = CallOutcome(
        role="member",
        model_id=spec.model.id,
        upstream_model_id=spec.model.upstream_model_id,
        provider_name=spec.provider.name,
        request_payload=payload,
    )
    adapter = adapter_factory(spec.provider, merged)
    start = time.perf_counter()
    async with semaphore:
        try:
            result = await adapter.complete(request)
        except AdapterError as exc:
            outcome.status = "timeout" if exc.kind == "timeout" else "failed"
            outcome.error_message = str(exc)
    outcome.duration_ms = int((time.perf_counter() - start) * 1000)
    if outcome.status == "success":
        outcome.response_content = result.content
        outcome.usage = result.usage
        outcome.duration_ms = result.duration_ms
    return outcome


def check_member_failures(outcomes: list[CallOutcome], *, strict: bool) -> None:
    """成员容错矩阵：strict 任一失败 / 全部失败 → AllMembersFailed（带全量明细）。"""
    if strict and any(o.status != "success" for o in outcomes):
        raise AllMembersFailed(
            f"strict 模式下存在失败成员: {_failures_summary(outcomes)}", members=outcomes
        )
    if all(o.status != "success" for o in outcomes):
        raise AllMembersFailed(f"全部成员失败: {_failures_summary(outcomes)}", members=outcomes)


async def indexed_outcome(
    index: int, coro: Coroutine[Any, Any, CallOutcome]
) -> tuple[int, CallOutcome]:
    """as_completed 包装：携带配置序下标（as_completed 产出的是新协程，无法反查原任务）。"""
    return index, await coro


async def run_members(
    ctx: StrategyContext,
    adapter_factory: AdapterFactory | None = None,
) -> list[CallOutcome]:
    """并发执行全部成员：受 max_concurrency 限流、部分失败容错。

    容错：默认(skip)只要 ≥1 成功即继续；fault_tolerance.member_failure=strict 时任一失败即
    抛 AllMembersFailed；全部失败时无论模式都抛 AllMembersFailed。
    """
    if adapter_factory is None:
        adapter_factory = default_member_adapter_factory(ctx)
    strict = (ctx.pipeline.fault_tolerance or {}).get("member_failure") == "strict"
    semaphore = asyncio.Semaphore(max(1, int(ctx.pipeline.max_concurrency or 10)))

    outcomes = list(
        await asyncio.gather(
            *(run_one(ctx, spec, adapter_factory, semaphore) for spec in ctx.members)
        )
    )
    check_member_failures(outcomes, strict=strict)
    return outcomes


# ---- T16/T31 council 策略（两段式裁判） ---------------------------------------------


@register_strategy
class CouncilStrategy(Strategy):
    name = "council"

    def assemble_critique(
        self, ctx: StrategyContext, ok_members: list[CallOutcome], *, stream: bool = False
    ) -> tuple[LlmRequest, dict[str, Any]]:
        """裁判第一段：评论各成员答案的优劣（内置模板，不开放自定义）。"""
        answers = [(o.upstream_model_id, o.response_content) for o in ok_members]
        prompt = render_judge_prompt(DEFAULT_CRITIQUE_TEMPLATE, ctx.body["messages"], answers)
        merged = merge_params(ctx.judge_model.default_params, ctx.user_params)
        return build_call_request(
            ctx.judge_model.upstream_model_id,
            [{"role": "user", "content": prompt}],
            merged,
            stream=stream,
        )

    def assemble_final(
        self,
        ctx: StrategyContext,
        ok_members: list[CallOutcome],
        critique: str,
        critique_messages: list[dict[str, str]],
        *,
        stream: bool = False,
    ) -> tuple[LlmRequest, dict[str, Any]]:
        """裁判第二段：与第一次同会话（输入 → 评论 → 最终指令），产出最终答案。

        judge_prompt_template 仅作用于最终指令轮。
        """
        template = ctx.pipeline.judge_prompt_template or DEFAULT_JUDGE_TEMPLATE
        answers = [(o.upstream_model_id, o.response_content) for o in ok_members]
        instruction = render_final_instruction(template, ctx.body["messages"], answers, critique)
        messages = build_final_messages(critique_messages, critique, instruction)
        merged = merge_params(ctx.judge_model.default_params, ctx.user_params)
        return build_call_request(
            ctx.judge_model.upstream_model_id,
            messages,
            merged,
            stream=stream,
        )

    def _judge_adapter(self, ctx: StrategyContext) -> BaseAdapter:
        merged = merge_params(ctx.judge_model.default_params, ctx.user_params)
        return build_adapter(
            ctx.judge_provider,
            fernet_key=ctx.fernet_key,
            timeout_seconds=float(merged.get("timeout_seconds", 120)),
            max_retries=int(merged.get("max_retries", 1)),
        )

    async def _run_critique(
        self, ctx: StrategyContext, adapter: BaseAdapter, ok_members: list[CallOutcome]
    ) -> CallOutcome:
        """执行裁判第一段（评论）。上游流式（D8），complete 聚合为全文。"""
        request, payload = self.assemble_critique(ctx, ok_members, stream=True)
        critique = CallOutcome(
            role="judge_critique",
            model_id=ctx.judge_model.id,
            upstream_model_id=ctx.judge_model.upstream_model_id,
            provider_name=ctx.judge_provider.name,
            request_payload=payload,
        )
        start = time.perf_counter()
        try:
            result = await adapter.complete(request)
        except AdapterError as exc:
            critique.status = "failed"
            critique.error_message = str(exc)
            critique.duration_ms = int((time.perf_counter() - start) * 1000)
        else:
            critique.response_content = result.content
            critique.usage = result.usage
            critique.duration_ms = result.duration_ms
        return critique

    async def run(self, ctx: StrategyContext) -> StrategyResult:
        member_outcomes = await run_members(ctx)
        ok = [o for o in member_outcomes if o.status == "success"]
        strict = (ctx.pipeline.fault_tolerance or {}).get("judge_failure") == "strict"
        adapter = self._judge_adapter(ctx)

        # 第一段：评论。失败即视为裁判失败（降级返回首个成功成员答案，AE-18-2）
        critique = await self._run_critique(ctx, adapter, ok)
        if critique.status != "success":
            if strict:
                raise StrategyExecutionError(
                    f"裁判调用失败(strict): {critique.error_message}",
                    members=member_outcomes,
                    critique=critique,
                ) from None
            return StrategyResult(
                final_content=ok[0].response_content,
                usage=sum_usage(member_outcomes),
                degraded=True,
                calls=[*member_outcomes, critique],
            )

        # 第二段：最终答案（与第一次同会话）
        request, payload = self.assemble_final(
            ctx, ok, critique.response_content, critique.request_payload["messages"], stream=True
        )
        start = time.perf_counter()
        judge = CallOutcome(
            role="judge",
            model_id=ctx.judge_model.id,
            upstream_model_id=ctx.judge_model.upstream_model_id,
            provider_name=ctx.judge_provider.name,
            request_payload=payload,
        )
        try:
            result = await adapter.complete(request)
        except AdapterError as exc:
            judge.status = "failed"
            judge.error_message = str(exc)
            judge.duration_ms = int((time.perf_counter() - start) * 1000)
            if strict:
                raise StrategyExecutionError(
                    f"裁判调用失败(strict): {exc}",
                    members=member_outcomes,
                    critique=critique,
                    judge=judge,
                ) from None
            # 降级：返回首个成功成员的答案（AE-18-2）
            return StrategyResult(
                final_content=ok[0].response_content,
                usage=sum_usage(member_outcomes),
                degraded=True,
                calls=[*member_outcomes, critique, judge],
            )

        judge.response_content = result.content
        judge.usage = result.usage
        judge.duration_ms = result.duration_ms
        total = sum_usage(member_outcomes)
        total.prompt_tokens += critique.usage.prompt_tokens + result.usage.prompt_tokens
        total.completion_tokens += critique.usage.completion_tokens + result.usage.completion_tokens
        return StrategyResult(
            final_content=result.content,
            usage=total,
            degraded=False,
            calls=[*member_outcomes, critique, judge],
        )

    async def prepare_stream(self, ctx: StrategyContext) -> StreamPlan:
        member_outcomes = await run_members(ctx)
        ok = [o for o in member_outcomes if o.status == "success"]
        adapter = self._judge_adapter(ctx)

        critique = await self._run_critique(ctx, adapter, ok)
        if critique.status != "success":
            # 评论失败发生在开流之前：无论容错模式均按失败返回（流式无中途降级语义）
            raise StrategyExecutionError(
                f"裁判调用失败(评论阶段): {critique.error_message}",
                members=member_outcomes,
                critique=critique,
            ) from None

        request, payload = self.assemble_final(
            ctx,
            ok,
            critique.response_content,
            critique.request_payload["messages"],
            stream=True,
        )
        base_usage = sum_usage(member_outcomes)
        base_usage.prompt_tokens += critique.usage.prompt_tokens
        base_usage.completion_tokens += critique.usage.completion_tokens
        return StreamPlan(
            adapter=adapter,
            request=request,
            payload=payload,
            model_id=ctx.judge_model.id,
            upstream_model_id=ctx.judge_model.upstream_model_id,
            provider_name=ctx.judge_provider.name,
            pre_outcomes=member_outcomes + [critique],
            base_usage=base_usage,
        )

    async def run_stream_process(
        self, ctx: StrategyContext
    ) -> AsyncIterator[ProcessEvent | FinalPlanReady]:
        """过程流式（T38）：成员完成即事件（as_completed 完成序），评论与终局计划随后。

        上游请求与 prepare_stream 逐字节一致（同一 run_one/_run_critique/assemble_final）；
        仅观测顺序变为完成序，下游评论/终局的答案序仍按成员配置序。
        """
        adapter_factory = default_member_adapter_factory(ctx)
        strict = (ctx.pipeline.fault_tolerance or {}).get("member_failure") == "strict"
        semaphore = asyncio.Semaphore(max(1, int(ctx.pipeline.max_concurrency or 10)))
        tasks = [
            asyncio.ensure_future(
                indexed_outcome(i, run_one(ctx, spec, adapter_factory, semaphore))
            )
            for i, spec in enumerate(ctx.members)
        ]
        try:
            ordered: list[CallOutcome | None] = [None] * len(tasks)
            for fut in asyncio.as_completed(tasks):
                i, outcome = await fut
                ordered[i] = outcome
                yield ProcessEvent(
                    kind="member", label=outcome.upstream_model_id, outcome=outcome
                )
            member_outcomes = [o for o in ordered if o is not None]
            check_member_failures(member_outcomes, strict=strict)
            ok = [o for o in member_outcomes if o.status == "success"]

            adapter = self._judge_adapter(ctx)
            critique = await self._run_critique(ctx, adapter, ok)
            yield ProcessEvent(kind="critique", label="评论", outcome=critique)
            if critique.status != "success":
                # 评论失败：成员明细已随事件落地，按 prepare_stream 同形态抛错
                raise StrategyExecutionError(
                    f"裁判调用失败(评论阶段): {critique.error_message}",
                    members=member_outcomes,
                    critique=critique,
                ) from None

            request, payload = self.assemble_final(
                ctx,
                ok,
                critique.response_content,
                critique.request_payload["messages"],
                stream=True,
            )
            yield FinalPlanReady(
                plan=StreamPlan(
                    adapter=adapter,
                    request=request,
                    payload=payload,
                    model_id=ctx.judge_model.id,
                    upstream_model_id=ctx.judge_model.upstream_model_id,
                    provider_name=ctx.judge_provider.name,
                    pre_outcomes=[],
                    base_usage=LlmUsage(),
                )
            )
        finally:
            # detach 关闭时断连会把 CancelledError 传入生成器：取消在飞成员任务
            for task in tasks:
                task.cancel()
