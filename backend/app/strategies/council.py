"""council 策略：成员并发回答 → 裁判 Prompt 组装 → 裁判聚合（默认可降级）。

录制脚本（tests/record_scenarios.py）直接 import 本模块的渲染函数构造裁判请求，
保证"录制的裁判 Prompt"与"运行时组装的裁判 Prompt"逐字节一致（快照命中前提）。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
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
    MemberSpec,
    Strategy,
    StrategyContext,
    StrategyExecutionError,
    StrategyResult,
    StreamPlan,
    register_strategy,
)

DEFAULT_JUDGE_TEMPLATE = """你将看到用户的问题，以及多个 AI 模型分别给出的回答。
请综合比较这些回答：找出相互印证的关键信息，识别其中的错误或矛盾，然后基于最可靠的信息，给出一个比任何单个回答都更准确、完整的最终回答。

【用户对话】
{{original_messages}}

【各模型回答】
{{candidate_answers}}

请直接输出最终回答，不要复述过程。"""

_PLACEHOLDER = re.compile(r"\{\{(original_messages|candidate_answers)\}\}")


# ---- T15 裁判 Prompt --------------------------------------------------------------


def serialize_messages(messages: list[dict[str, Any]]) -> str:
    """多轮消息序列化为 [role] content 行（完整保留 system/user/assistant）。"""
    return "\n".join(f"[{m.get('role')}] {m.get('content')}" for m in messages)


def render_judge_prompt(
    template: str,
    messages: list[dict[str, Any]],
    answers: list[tuple[str, str]],
) -> str:
    """渲染裁判 Prompt。单遍替换占位符——答案文本里出现的占位符字样不会被二次展开。"""
    original = serialize_messages(messages)
    candidates = "\n\n".join(
        f"【回答 {i} · {model}】\n{content}" for i, (model, content) in enumerate(answers, start=1)
    )
    parts: list[str] = []
    last = 0
    for match in _PLACEHOLDER.finditer(template):
        parts.append(template[last : match.start()])
        parts.append(original if match.group(1) == "original_messages" else candidates)
        last = match.end()
    parts.append(template[last:])
    return "".join(parts)


# ---- 参数合并与请求构造（三层优先级：模型默认 < 成员覆盖 < 请求参数） ----------------


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


async def run_members(
    ctx: StrategyContext,
    adapter_factory: AdapterFactory | None = None,
) -> list[CallOutcome]:
    """并发执行全部成员：受 max_concurrency 限流、部分失败容错。

    上游一律流式调用（决策 D8）：member_timeout_seconds 是成员适配器的
    TTFT/块间空闲超时预算——复杂问题生成再久也不算超时，只有等不到上游数据才超时。
    容错：默认(skip)只要 ≥1 成功即继续；fault_tolerance.member_failure=strict 时任一失败即
    抛 AllMembersFailed；全部失败时无论模式都抛 AllMembersFailed。
    """
    member_timeout = float(ctx.pipeline.member_timeout_seconds or 60)
    if adapter_factory is None:

        def adapter_factory(provider: Provider, merged: dict[str, Any]) -> BaseAdapter:
            return build_adapter(
                provider,
                fernet_key=ctx.fernet_key,
                timeout_seconds=member_timeout,
                max_retries=int(merged.get("max_retries", 1)),
            )

    strict = (ctx.pipeline.fault_tolerance or {}).get("member_failure") == "strict"
    semaphore = asyncio.Semaphore(max(1, int(ctx.pipeline.max_concurrency or 10)))

    async def run_one(spec: MemberSpec) -> CallOutcome:
        merged = merge_params(
            spec.model.default_params, spec.member.param_overrides, ctx.user_params
        )
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

    outcomes = list(await asyncio.gather(*(run_one(spec) for spec in ctx.members)))

    if strict and any(o.status != "success" for o in outcomes):
        raise AllMembersFailed(
            f"strict 模式下存在失败成员: {_failures_summary(outcomes)}", members=outcomes
        )
    if all(o.status != "success" for o in outcomes):
        raise AllMembersFailed(f"全部成员失败: {_failures_summary(outcomes)}", members=outcomes)
    return outcomes


# ---- T16 council 策略 --------------------------------------------------------------


@register_strategy
class CouncilStrategy(Strategy):
    name = "council"

    def assemble_judge(
        self, ctx: StrategyContext, ok_members: list[CallOutcome], *, stream: bool = False
    ) -> tuple[LlmRequest, dict[str, Any]]:
        template = ctx.pipeline.judge_prompt_template or DEFAULT_JUDGE_TEMPLATE
        answers = [(o.upstream_model_id, o.response_content) for o in ok_members]
        prompt = render_judge_prompt(template, ctx.body["messages"], answers)
        merged = merge_params(ctx.judge_model.default_params, ctx.user_params)
        return build_call_request(
            ctx.judge_model.upstream_model_id,
            [{"role": "user", "content": prompt}],
            merged,
            stream=stream,
        )

    def _judge_adapter(self, ctx: StrategyContext) -> BaseAdapter:
        merged = merge_params(ctx.judge_model.default_params, ctx.user_params)
        return build_adapter(
            ctx.judge_provider,
            fernet_key=ctx.fernet_key,
            timeout_seconds=float(merged.get("timeout_seconds", 60)),
            max_retries=int(merged.get("max_retries", 1)),
        )

    async def run(self, ctx: StrategyContext) -> StrategyResult:
        member_outcomes = await run_members(ctx)
        ok = [o for o in member_outcomes if o.status == "success"]

        request, payload = self.assemble_judge(ctx, ok, stream=True)
        start = time.perf_counter()
        judge = CallOutcome(
            role="judge",
            model_id=ctx.judge_model.id,
            upstream_model_id=ctx.judge_model.upstream_model_id,
            provider_name=ctx.judge_provider.name,
            request_payload=payload,
        )
        try:
            result = await self._judge_adapter(ctx).complete(request)
        except AdapterError as exc:
            judge.status = "failed"
            judge.error_message = str(exc)
            judge.duration_ms = int((time.perf_counter() - start) * 1000)
            if (ctx.pipeline.fault_tolerance or {}).get("judge_failure") == "strict":
                raise StrategyExecutionError(
                    f"裁判调用失败(strict): {exc}", members=member_outcomes, judge=judge
                ) from None
            # 降级：返回首个成功成员的答案（AE-18-2）
            return StrategyResult(
                final_content=ok[0].response_content,
                usage=sum_usage(member_outcomes),
                degraded=True,
                members=member_outcomes,
                judge=judge,
            )

        judge.response_content = result.content
        judge.usage = result.usage
        judge.duration_ms = result.duration_ms
        total = sum_usage(member_outcomes)
        total.prompt_tokens += result.usage.prompt_tokens
        total.completion_tokens += result.usage.completion_tokens
        return StrategyResult(
            final_content=result.content,
            usage=total,
            degraded=False,
            members=member_outcomes,
            judge=judge,
        )

    async def prepare_stream(self, ctx: StrategyContext) -> StreamPlan:
        member_outcomes = await run_members(ctx)
        ok = [o for o in member_outcomes if o.status == "success"]
        request, payload = self.assemble_judge(ctx, ok, stream=True)
        return StreamPlan(
            adapter=self._judge_adapter(ctx),
            request=request,
            payload=payload,
            model_id=ctx.judge_model.id,
            upstream_model_id=ctx.judge_model.upstream_model_id,
            provider_name=ctx.judge_provider.name,
            pre_outcomes=member_outcomes,
            base_usage=sum_usage(member_outcomes),
        )
