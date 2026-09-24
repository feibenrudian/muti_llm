"""council 策略：成员并发回答 → 两段式裁判（先评论各答案优劣，再产出最终答案）。

录制脚本（tests/record_scenarios.py）直接 import 本模块的渲染函数构造两段裁判请求，
保证"录制的裁判 Prompt"与"运行时组装的裁判 Prompt"逐字节一致（快照命中前提）。

T44 工具聚合模式（pipeline.tool_aggregation=true 且请求带非空 tools）：成员带 tools
并行调用 → 各成员调用意图文本化进裁判上下文 → 评论（参考非约束）→ 裁判带 tools
生成最终调用（或判定无需工具时输出文字回答）→ 程序性校验兜底，失败降级择优成员调用。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
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

# T44 工具聚合终局指令（内置，不开放自定义——与文本答案模板语义不同）：裁判自由推理
# 合成最终调用（评论是参考非约束），也可判定无需工具时输出文字回答。
# 模板为单行字面量（折行会改变渲染输出，快照 hash 依赖逐字节一致）
DEFAULT_TOOL_JUDGE_TEMPLATE = (  # noqa: E501
    "请结合你上面给出的评论，从各模型的工具调用意图中综合推理出最合理的最终决策：选择最合适的工具并给出完整、正确的调用参数（可以修正各模型参数中的遗漏与错误）。做出决策后直接发起该工具调用，不要输出文字说明。若你判断无需调用工具即可回答用户，则直接输出最终文字回答，不要调用任何工具。"
)

_PLACEHOLDER = re.compile(r"\{\{(original_messages|candidate_answers|critique)\}\}")


# ---- T44 工具聚合 -------------------------------------------------------------------


def tool_mode(ctx: StrategyContext) -> bool:
    """工具聚合模式生效条件：pipeline 开关开启且请求带非空 tools。"""
    tools = ctx.body.get("tools")
    return bool(ctx.pipeline.tool_aggregation) and isinstance(tools, list) and len(tools) > 0


def format_tool_intent(outcome: CallOutcome) -> str:
    """成员输出 → 裁判上下文的文本描述：调用意图列 JSON，纯文本回答原样。"""
    if outcome.response_tool_calls:
        calls = "; ".join(
            f"{c['function']['name']}({c['function']['arguments']})"
            for c in outcome.response_tool_calls
        )
        text = f"决定调用工具: {calls}"
        if outcome.response_content:
            text += f"（附说明: {outcome.response_content}）"
        return text
    return outcome.response_content or "（无输出）"


def validate_tool_calls(tool_calls: list[dict[str, Any]], tools: list[dict[str, Any]]) -> bool:
    """程序性兜底校验（防幻觉，不限制裁判判断自由）：function 名在 tools 清单内、
    arguments 是合法 JSON 对象。"""
    known = {
        t["function"]["name"] for t in tools if isinstance(t, dict) and "function" in t
    } - {None}
    if not tool_calls:
        return False
    for call in tool_calls:
        function = (call or {}).get("function") or {}
        if function.get("name") not in known:
            return False
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (TypeError, ValueError):
            return False
        if not isinstance(arguments, dict):
            return False
    return True


def pick_fallback_tool_calls(
    ok_members: list[CallOutcome],
) -> list[dict[str, Any]] | None:
    """降级择优：多数 function.name 派系中取第一个产出该调用工具的成员（确定性）。"""
    calls_by_member = [o.response_tool_calls for o in ok_members if o.response_tool_calls]
    if not calls_by_member:
        return None
    names = [
        c["function"]["name"] for calls in calls_by_member for c in calls if c.get("function")
    ]
    if not names:
        return None
    top = Counter(names).most_common(1)[0][0]
    for calls in calls_by_member:
        if any(c.get("function", {}).get("name") == top for c in calls):
            return calls
    return None  # pragma: no cover


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
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
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
    if tools:
        payload["tools"] = tools
        request.tools = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
            request.tool_choice = tool_choice
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
        usage.cached_tokens += o.usage.cached_tokens
        usage.cache_write_tokens += o.usage.cache_write_tokens
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
    body_tools = ctx.body.get("tools")
    tools = body_tools if tool_mode(ctx) else None
    request, payload = build_call_request(
        spec.model.upstream_model_id,
        ctx.body["messages"],
        merged,
        stream=True,
        tools=tools,
        tool_choice=ctx.body.get("tool_choice") if tools else None,
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
        outcome.response_tool_calls = result.tool_calls
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
        self,
        ctx: StrategyContext,
        ok_members: list[CallOutcome],
        *,
        stream: bool = False,
        answers: list[tuple[str, str]] | None = None,
    ) -> tuple[LlmRequest, dict[str, Any]]:
        """裁判第一段：评论各成员答案的优劣（内置模板，不开放自定义）。

        工具聚合模式传 answers=意图文本化结果（各成员的调用意图/文本回答）。
        """
        if answers is None:
            answers = [(o.upstream_model_id, o.response_content) for o in ok_members]
        prompt = render_judge_prompt(DEFAULT_CRITIQUE_TEMPLATE, ctx.body["messages"], answers)
        merged = merge_params(ctx.judge_model.default_params, ctx.user_params)
        return build_call_request(
            ctx.judge_model.upstream_model_id,
            [{"role": "user", "content": prompt}],
            merged,
            stream=stream,
        )

    def assemble_tool_final(
        self,
        ctx: StrategyContext,
        ok_members: list[CallOutcome],
        critique: str,
        critique_messages: list[dict[str, str]],
        *,
        stream: bool = False,
    ) -> tuple[LlmRequest, dict[str, Any]]:
        """T44 工具聚合终局：内置工具合成指令 + 请求带 tools/tool_choice——裁判借原生
        结构化输出产出最终调用（或判定无需工具时输出文字回答）。"""
        answers = [(o.upstream_model_id, format_tool_intent(o)) for o in ok_members]
        instruction = render_final_instruction(
            DEFAULT_TOOL_JUDGE_TEMPLATE, ctx.body["messages"], answers, critique
        )
        messages = build_final_messages(critique_messages, critique, instruction)
        merged = merge_params(ctx.judge_model.default_params, ctx.user_params)
        return build_call_request(
            ctx.judge_model.upstream_model_id,
            messages,
            merged,
            stream=stream,
            tools=ctx.body["tools"],
            tool_choice=ctx.body.get("tool_choice") or "auto",
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
        self,
        ctx: StrategyContext,
        adapter: BaseAdapter,
        ok_members: list[CallOutcome],
        *,
        answers: list[tuple[str, str]] | None = None,
    ) -> CallOutcome:
        """执行裁判第一段（评论）。上游流式（D8），complete 聚合为全文。"""
        request, payload = self.assemble_critique(
            ctx, ok_members, stream=True, answers=answers
        )
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

    def _tool_degraded(
        self,
        member_outcomes: list[CallOutcome],
        ok: list[CallOutcome],
        *extra_calls: CallOutcome,
    ) -> StrategyResult:
        """工具模式降级（裁判失败/校验失败且无合成结果）：择优成员 tool_calls，无则取其文本。"""
        fallback = pick_fallback_tool_calls(ok)
        total = sum_usage(member_outcomes)
        for call in extra_calls:
            if call.status == "success":
                total.prompt_tokens += call.usage.prompt_tokens
                total.completion_tokens += call.usage.completion_tokens
        if fallback is not None:
            return StrategyResult(
                final_content="",
                usage=total,
                degraded=True,
                calls=[*member_outcomes, *extra_calls],
                final_finish_reason="tool_calls",
                tool_calls=fallback,
            )
        return StrategyResult(
            final_content=ok[0].response_content if ok else "",
            usage=total,
            degraded=True,
            calls=[*member_outcomes, *extra_calls],
        )

    async def _run_tool_aggregation(self, ctx: StrategyContext) -> StrategyResult:
        """T44 工具聚合：成员带 tools 并行 → 意图文本化评论 → 裁判合成最终调用。

        校验失败（裁判产出的 function 名不在清单/arguments 非法 JSON）→ 降级择优成员调用
        （degraded=true）；裁判判定无需工具而输出文字时按普通文本答案返回。
        """
        member_outcomes = await run_members(ctx)
        ok = [o for o in member_outcomes if o.status == "success"]
        strict = (ctx.pipeline.fault_tolerance or {}).get("judge_failure") == "strict"
        adapter = self._judge_adapter(ctx)
        intents = [(o.upstream_model_id, format_tool_intent(o)) for o in ok]

        critique = await self._run_critique(ctx, adapter, ok, answers=intents)
        if critique.status != "success":
            if strict:
                raise StrategyExecutionError(
                    f"裁判调用失败(strict): {critique.error_message}",
                    members=member_outcomes,
                    critique=critique,
                ) from None
            return self._tool_degraded(member_outcomes, ok, critique)

        request, payload = self.assemble_tool_final(
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
            return self._tool_degraded(member_outcomes, ok, critique, judge)

        judge.response_content = result.content
        judge.response_tool_calls = result.tool_calls
        judge.usage = result.usage
        judge.duration_ms = result.duration_ms

        final_calls = result.tool_calls
        finish = result.finish_reason
        degraded = False
        if final_calls and not validate_tool_calls(final_calls, ctx.body["tools"]):
            fallback = pick_fallback_tool_calls(ok)
            if fallback is not None:
                final_calls, finish, degraded = fallback, "tool_calls", True
            else:
                final_calls, finish = None, "stop"
        elif not final_calls:
            finish = "stop"  # 裁判判定无需工具 → 文字回答

        total = sum_usage(member_outcomes)
        total.prompt_tokens += critique.usage.prompt_tokens + result.usage.prompt_tokens
        total.completion_tokens += critique.usage.completion_tokens + result.usage.completion_tokens
        return StrategyResult(
            final_content=result.content,
            usage=total,
            degraded=degraded,
            calls=[*member_outcomes, critique, judge],
            final_finish_reason=finish,
            tool_calls=final_calls,
        )

    async def run(self, ctx: StrategyContext) -> StrategyResult:
        if tool_mode(ctx):
            return await self._run_tool_aggregation(ctx)
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
        intents = (
            [(o.upstream_model_id, format_tool_intent(o)) for o in ok]
            if tool_mode(ctx)
            else None
        )

        critique = await self._run_critique(ctx, adapter, ok, answers=intents)
        if critique.status != "success":
            # 评论失败发生在开流之前：无论容错模式均按失败返回（流式无中途降级语义）
            raise StrategyExecutionError(
                f"裁判调用失败(评论阶段): {critique.error_message}",
                members=member_outcomes,
                critique=critique,
            ) from None

        if intents is not None:
            request, payload = self.assemble_tool_final(
                ctx,
                ok,
                critique.response_content,
                critique.request_payload["messages"],
                stream=True,
            )
        else:
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
