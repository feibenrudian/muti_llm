"""ICE（迭代共识集成）策略：成员多轮迭代改进 + 仲裁者逐轮评论判断共识 + 同会话终局裁决。

实现蓝本：doc/ICE策略集成技术方案与任务拆分.md §4（伪代码/模板/终局规则/JSON 解析）
与 §5（容错矩阵）。成员第 0 轮逐字节复用 council.run_members；仲裁者单会话贯穿（D10）；
成员迭代为成员自身会话延续（D12）；共识/停滞全部由仲裁者 JSON 驱动（D9），无相似度计算。
流式（T35/D15）：prepare_stream 把第 0 轮（成员并发+评论+共识判断）放在开流前完成，
失败语义同 council；第 0 轮即共识返回就绪终局计划，未共识返回带迭代状态的计划，
由网关驱动逐轮执行 + 注释行保活 + 终局 SSE 转发。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from app.adapters.base import AdapterError, BaseAdapter, LlmRequest, LlmUsage
from app.adapters.factory import build_adapter
from app.orm import Provider
from app.strategies.base import (
    AllMembersFailed,
    CallOutcome,
    IterationState,
    RoundResult,
    Strategy,
    StrategyContext,
    StrategyExecutionError,
    StrategyResult,
    StreamPlan,
    register_strategy,
)
from app.strategies.council import (
    DEFAULT_JUDGE_TEMPLATE,
    AdapterFactory,
    _failures_summary,
    build_call_request,
    merge_params,
    render_final_instruction,
    render_judge_prompt,
    run_members,
    sum_usage,
)

# §3.2 参数表：键集合即合法参数白名单
ICE_PARAM_DEFAULTS: dict[str, Any] = {
    "max_rounds": 3,  # 成员生成轮数上限（含第 0 轮）
    "confidence_threshold": 0.8,  # 终局条件：consensus=true 且 confidence ≥ 阈值
    "stagnation": True,  # 连续两轮未共识且 confidence 不升 → 提前终局
    "progress_comments": True,  # 流式迭代期间是否发送 SSE 注释行
}

# §4.2 模板全文（内置常量，逐字照抄；候选答案匿名【回答 N】）

ICE_CRITIQUE_TEMPLATE = """你将看到用户的问题，以及多个 AI 模型分别给出的回答。
请逐个评价每个回答的优劣：指出事实错误、关键信息遗漏、逻辑或表述问题，并给出简短的可信度结论。
随后判断这些回答是否已形成共识。

【用户对话】
{{original_messages}}

【各模型回答】
{{candidate_answers}}

严格按以下 JSON 格式输出，不要输出 JSON 以外的内容：
{"consensus": true或false, "confidence": 0到1的小数, "critique": "评论文本"}
- consensus：剔除表述差异后，各回答的核心结论是否一致；
- confidence：这组回答整体一致与可信的程度；
""" + (
    "- critique：按【回答 1】【回答 2】… 顺序逐条评论，聚焦下一轮改进方向，"
    "不评价模型本身，不复述回答原文。"
)

ICE_ROUND_CRITIQUE_TEMPLATE = """各模型已基于你的上一轮评论改进了回答。最新回答如下：

{{candidate_answers}}

请再次逐条评论最新回答，并判断是否已形成共识。严格按上轮相同的 JSON 格式输出。"""

ICE_REFINE_TEMPLATE = """以上是你此前对用户问题的回答。一位评审专家综合各匿名回答给出了如下评论：

{{critique}}

请批判性地审视该评论与你的回答：识别并吸收评论指出的正确信息与被你忽略的细节，改进你的回答。
直接输出更新后的完整回答，不要提及修改过程。"""


# ---- §4.4 评论 JSON 宽松解析 ---------------------------------------------------------


@dataclass
class Verdict:
    """仲裁者一轮评论的结构化结论。parsed=False 表示完全解析失败（字段为占位值）。"""

    consensus: bool
    confidence: float
    critique: str
    parsed: bool


def _strip_fence(text: str) -> str:
    """剥 Markdown 代码围栏（```json … ```）。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[^\n]*\n", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)
    return stripped


def _extract_json_object(text: str) -> str | None:
    """截取首个平衡的 {…} 片段（字符串内的花括号不计入深度）；无则 None。"""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_verdict(text: str) -> Verdict:
    """宽松解析仲裁评论 JSON；完全失败 → 未共识占位（parsed=False），失败方向固定为多迭代。"""
    candidate = _extract_json_object(_strip_fence(text))
    data: Any = None
    if candidate is not None:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        return Verdict(consensus=False, confidence=0.0, critique=text, parsed=False)
    consensus = data.get("consensus")
    confidence = data.get("confidence")
    critique = data.get("critique")
    return Verdict(
        consensus=consensus if isinstance(consensus, bool) else False,
        confidence=(
            float(confidence)
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            else 0.0
        ),
        critique=critique if isinstance(critique, str) else text,
        parsed=True,
    )


# ---- T33 ICE 策略 --------------------------------------------------------------------


@register_strategy
class IceStrategy(Strategy):
    name = "ice"

    @classmethod
    def validate_params(cls, params: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(params) - set(ICE_PARAM_DEFAULTS))
        if unknown:
            raise ValueError(f"未知参数: {', '.join(unknown)}")
        filled = {**ICE_PARAM_DEFAULTS, **params}
        max_rounds = filled["max_rounds"]
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int):
            raise ValueError("max_rounds 必须是 int")
        if not 1 <= max_rounds <= 5:
            raise ValueError("max_rounds 必须在 1-5 之间")
        threshold = filled["confidence_threshold"]
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("confidence_threshold 必须是数值")
        if not 0 <= threshold <= 1:
            raise ValueError("confidence_threshold 必须在 0-1 之间")
        filled["confidence_threshold"] = float(threshold)
        for flag in ("stagnation", "progress_comments"):
            if not isinstance(filled[flag], bool):
                raise ValueError(f"{flag} 必须是 bool")
        return filled

    # -- 终局条件（§4.3） ------------------------------------------------------------

    @staticmethod
    def _should_finish(
        verdict: Verdict, round_no: int, conf_history: list[float], params: dict[str, Any]
    ) -> bool:
        """终局条件（任一满足）：共识达标 / 轮数耗尽 / 停滞（序列只含解析成功轮）。"""
        if verdict.consensus and verdict.confidence >= params["confidence_threshold"]:
            return True
        if round_no >= params["max_rounds"] - 1:
            return True
        return (
            bool(params["stagnation"])
            and len(conf_history) >= 2
            and conf_history[-1] <= conf_history[-2]
        )

    # -- 仲裁者调用（D10 单会话贯穿；ctx.user_params 作用于全部 R+1 次裁判调用） ------

    def _judge_adapter(self, ctx: StrategyContext) -> BaseAdapter:
        merged = merge_params(ctx.judge_model.default_params, ctx.user_params)
        return build_adapter(
            ctx.judge_provider,
            fernet_key=ctx.fernet_key,
            timeout_seconds=float(merged.get("timeout_seconds", 120)),
            max_retries=int(merged.get("max_retries", 1)),
        )

    async def _run_round_critique(
        self,
        ctx: StrategyContext,
        adapter: BaseAdapter,
        ok: list[CallOutcome],
        session: list[dict[str, str]],
        *,
        round_no: int,
    ) -> CallOutcome:
        """一轮仲裁评论：round 0 含原对话+匿名答案；k≥1 同会话续轮只带最新答案。

        成功才把本轮 user/assistant 追加进仲裁者会话；失败不入会话。
        """
        answers = [(o.upstream_model_id, o.response_content) for o in ok]
        template = ICE_CRITIQUE_TEMPLATE if not session else ICE_ROUND_CRITIQUE_TEMPLATE
        prompt = render_judge_prompt(template, ctx.body["messages"], answers)
        user_message = {"role": "user", "content": prompt}
        merged = merge_params(ctx.judge_model.default_params, ctx.user_params)
        request, payload = build_call_request(
            ctx.judge_model.upstream_model_id, [*session, user_message], merged, stream=True
        )
        critique = CallOutcome(
            role="judge_critique",
            model_id=ctx.judge_model.id,
            upstream_model_id=ctx.judge_model.upstream_model_id,
            provider_name=ctx.judge_provider.name,
            request_payload=payload,
            round_no=round_no,
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
            session.extend((user_message, {"role": "assistant", "content": result.content}))
        return critique

    def _assemble_final(
        self,
        ctx: StrategyContext,
        session: list[dict[str, str]],
        ok: list[CallOutcome],
        critique_text: str,
    ) -> tuple[LlmRequest, dict[str, Any]]:
        """终局裁决请求组装：同会话续轮 + judge_prompt_template 指令（仅作用于本指令轮）。"""
        template = ctx.pipeline.judge_prompt_template or DEFAULT_JUDGE_TEMPLATE
        answers = [(o.upstream_model_id, o.response_content) for o in ok]
        instruction = render_final_instruction(
            template, ctx.body["messages"], answers, critique_text
        )
        merged = merge_params(ctx.judge_model.default_params, ctx.user_params)
        return build_call_request(
            ctx.judge_model.upstream_model_id,
            [*session, {"role": "user", "content": instruction}],
            merged,
            stream=True,
        )

    async def _run_final(
        self,
        ctx: StrategyContext,
        adapter: BaseAdapter,
        session: list[dict[str, str]],
        ok: list[CallOutcome],
        critique_text: str,
    ) -> CallOutcome:
        """终局裁决：执行 _assemble_final 组装的请求并聚合全文。"""
        request, payload = self._assemble_final(ctx, session, ok, critique_text)
        final = CallOutcome(
            role="judge",
            model_id=ctx.judge_model.id,
            upstream_model_id=ctx.judge_model.upstream_model_id,
            provider_name=ctx.judge_provider.name,
            request_payload=payload,
        )
        start = time.perf_counter()
        try:
            result = await adapter.complete(request)
        except AdapterError as exc:
            final.status = "failed"
            final.error_message = str(exc)
            final.duration_ms = int((time.perf_counter() - start) * 1000)
        else:
            final.response_content = result.content
            final.usage = result.usage
            final.duration_ms = result.duration_ms
        return final

    # -- 成员迭代轮（D12：延续成员自身会话；复用 run_members 的限流/容错骨架） ---------

    async def _run_refine_round(
        self,
        ctx: StrategyContext,
        ok: list[CallOutcome],
        critique: str,
        round_no: int,
        *,
        adapter_factory: AdapterFactory | None = None,
    ) -> list[CallOutcome]:
        """第 k≥1 轮成员改进：原始 messages → assistant(上轮答案) → user(改进指令)。

        与 ok 同序，每成员恰好一行；失败不抛错——调用方沿用上轮答案，失败行照落（§5）。
        """
        member_timeout = float(ctx.pipeline.member_timeout_seconds or 120)
        if adapter_factory is None:

            def adapter_factory(provider: Provider, merged: dict[str, Any]) -> BaseAdapter:
                return build_adapter(
                    provider,
                    fernet_key=ctx.fernet_key,
                    timeout_seconds=member_timeout,
                    max_retries=int(merged.get("max_retries", 1)),
                )

        semaphore = asyncio.Semaphore(max(1, int(ctx.pipeline.max_concurrency or 10)))
        specs = {spec.model.id: spec for spec in ctx.members}
        refine_prompt = render_judge_prompt(
            ICE_REFINE_TEMPLATE, ctx.body["messages"], [], critique=critique
        )

        async def run_one(prev: CallOutcome) -> CallOutcome:
            spec = specs[prev.model_id]
            merged = merge_params(spec.model.default_params, spec.member.param_overrides)
            messages = [
                *ctx.body["messages"],
                {"role": "assistant", "content": prev.response_content},
                {"role": "user", "content": refine_prompt},
            ]
            request, payload = build_call_request(
                spec.model.upstream_model_id, messages, merged, stream=True
            )
            outcome = CallOutcome(
                role="member",
                model_id=spec.model.id,
                upstream_model_id=spec.model.upstream_model_id,
                provider_name=spec.provider.name,
                request_payload=payload,
                round_no=round_no,
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

        return list(await asyncio.gather(*(run_one(prev) for prev in ok)))

    # -- 主流程（§4.1 修正版伪代码逐行对应） ---------------------------------------------

    async def run(
        self,
        ctx: StrategyContext,
        *,
        adapter_factory: AdapterFactory | None = None,
        judge_adapter: BaseAdapter | None = None,
    ) -> StrategyResult:
        """非流式执行：第 0 轮成员并发 → 逐轮评论/改进 → 终局裁决。"""
        params = self.validate_params(ctx.pipeline.strategy_params)
        judge_strict = (ctx.pipeline.fault_tolerance or {}).get("judge_failure") == "strict"
        outcomes: list[CallOutcome] = []

        # 第 0 轮：成员原始请求（复用 council 执行器：并发/限流/容错/TTFT 超时；
        # 全部失败或 strict 任一失败由 run_members 抛 AllMembersFailed）
        try:
            round0 = await run_members(ctx, adapter_factory=adapter_factory)
        except AllMembersFailed as exc:
            for outcome in exc.members:
                outcome.round_no = 0
            raise
        for outcome in round0:
            outcome.round_no = 0
        outcomes += round0
        ok = [o for o in round0 if o.status == "success"]  # 失败成员整场退出（同 council skip）
        if not ok:  # pragma: no cover - run_members 已先行抛出
            raise AllMembersFailed(f"全部成员失败: {_failures_summary(round0)}", members=round0)

        adapter = judge_adapter if judge_adapter is not None else self._judge_adapter(ctx)
        session: list[dict[str, str]] = []  # 仲裁者会话（D10）

        critique = await self._run_round_critique(ctx, adapter, ok, session, round_no=0)
        outcomes.append(critique)
        if critique.status != "success":
            # 容错矩阵：第 0 轮评论失败，等同 council 今日语义（AE-18-2/18-3）
            if judge_strict:
                raise StrategyExecutionError(
                    f"裁判调用失败(strict): {critique.error_message}",
                    members=outcomes,
                    critique=critique,
                ) from None
            return StrategyResult(
                final_content=ok[0].response_content,
                usage=sum_usage(outcomes),
                degraded=True,
                calls=outcomes,
            )

        verdict = parse_verdict(critique.response_content)
        # 停滞序列只收解析成功的轮次：解析失败的 confidence=0.0 占位值不进序列（§4.3/§4.4）
        round_no = 0
        conf_history = [verdict.confidence] if verdict.parsed else []
        critique_text = critique.response_content
        degraded = False
        while not self._should_finish(verdict, round_no, conf_history, params):
            round_no += 1
            refined = await self._run_refine_round(
                ctx, ok, verdict.critique, round_no, adapter_factory=adapter_factory
            )
            outcomes += refined
            # 失败成员沿用上轮答案（§5），失败行照落；下一轮该成员继续参与（可恢复）
            ok = [
                new if new.status == "success" else old
                for old, new in zip(ok, refined, strict=True)
            ]
            critique = await self._run_round_critique(ctx, adapter, ok, session, round_no=round_no)
            outcomes.append(critique)
            if critique.status != "success":
                # 迭代期评论失败：跳过剩余轮，直接终局（D11）；strict 非流式 502
                if judge_strict:
                    raise StrategyExecutionError(
                        f"裁判调用失败(strict): {critique.error_message}",
                        members=outcomes,
                        critique=critique,
                    ) from None
                degraded = True
                break
            verdict = parse_verdict(critique.response_content)
            critique_text = critique.response_content
            if verdict.parsed:
                conf_history.append(verdict.confidence)

        final = await self._run_final(ctx, adapter, session, ok, critique_text)
        outcomes.append(final)
        if final.status != "success":
            # 终局失败：降级首个成功成员最新轮答案 / strict 抛错（§5）
            if judge_strict:
                raise StrategyExecutionError(
                    f"裁判调用失败(strict): {final.error_message}",
                    members=[o for o in outcomes if o.role == "member"],
                    judge=final,
                ) from None
            return StrategyResult(
                final_content=ok[0].response_content,
                usage=sum_usage(outcomes),
                degraded=True,
                calls=outcomes,
            )
        return StrategyResult(
            final_content=final.response_content,
            usage=sum_usage(outcomes),
            degraded=degraded,
            calls=outcomes,
        )

    # -- 流式（T35/D15） ----------------------------------------------------------------

    async def _iterate(
        self,
        state: IterationState,
        ctx: StrategyContext,
        adapter: BaseAdapter,
        session: list[dict[str, str]],
        ok: list[CallOutcome],
        verdict: Verdict,
        conf_history: list[float],
        critique_text: str,
        params: dict[str, Any],
        *,
        judge_strict: bool,
        adapter_factory: AdapterFactory | None,
    ) -> AsyncIterator[RoundResult]:
        """逐轮执行迭代（成员改进 → 评论），终局请求组装完成后填充 state.final_plan。

        复用 run 的轮次逻辑与容错矩阵：成员失败沿用上轮答案；迭代期评论失败
        strict → 抛错终止流，否则跳过剩余轮直接终局（degraded，D11）。
        """
        round_no = 0
        while not self._should_finish(verdict, round_no, conf_history, params):
            round_no += 1
            refined = await self._run_refine_round(
                ctx, ok, verdict.critique, round_no, adapter_factory=adapter_factory
            )
            yield RoundResult(round_no=round_no, members=refined)
            # 失败成员沿用上轮答案（§5），失败行照落；下一轮该成员继续参与（可恢复）
            ok = [
                new if new.status == "success" else old
                for old, new in zip(ok, refined, strict=True)
            ]
            critique = await self._run_round_critique(ctx, adapter, ok, session, round_no=round_no)
            yield RoundResult(round_no=round_no, critique=critique)
            if critique.status != "success":
                if judge_strict:
                    raise StrategyExecutionError(
                        f"裁判调用失败(strict): {critique.error_message}",
                        members=refined,
                        critique=critique,
                    ) from None
                break
            verdict = parse_verdict(critique.response_content)
            critique_text = critique.response_content
            if verdict.parsed:
                conf_history.append(verdict.confidence)
        request, payload = self._assemble_final(ctx, session, ok, critique_text)
        state.final_plan = StreamPlan(
            adapter=adapter,
            model_id=ctx.judge_model.id,
            upstream_model_id=ctx.judge_model.upstream_model_id,
            provider_name=ctx.judge_provider.name,
            pre_outcomes=[],
            base_usage=LlmUsage(),
            request=request,
            payload=payload,
        )

    async def prepare_stream(
        self,
        ctx: StrategyContext,
        *,
        adapter_factory: AdapterFactory | None = None,
        judge_adapter: BaseAdapter | None = None,
    ) -> StreamPlan:
        """流式计划：第 0 轮（成员并发+评论+共识判断）开流前完成，失败语义同 council。

        第 0 轮即共识 → 不带迭代状态的就绪终局计划（走现有 event_stream 路径，零新增行为）；
        未共识 → 携带迭代状态，网关走迭代路径（注释行 + 轮次执行 + 终局流式转发）。
        """
        params = self.validate_params(ctx.pipeline.strategy_params)
        judge_strict = (ctx.pipeline.fault_tolerance or {}).get("judge_failure") == "strict"

        try:
            round0 = await run_members(ctx, adapter_factory=adapter_factory)
        except AllMembersFailed as exc:
            for outcome in exc.members:
                outcome.round_no = 0
            raise
        for outcome in round0:
            outcome.round_no = 0
        ok = [o for o in round0 if o.status == "success"]

        adapter = judge_adapter if judge_adapter is not None else self._judge_adapter(ctx)
        session: list[dict[str, str]] = []  # 仲裁者会话（D10）
        critique = await self._run_round_critique(ctx, adapter, ok, session, round_no=0)
        if critique.status != "success":
            # 第 0 轮评论失败发生在开流前：无论容错模式均按失败返回（同 council 流式语义）
            raise StrategyExecutionError(
                f"裁判调用失败(评论阶段): {critique.error_message}",
                members=round0,
                critique=critique,
            ) from None

        pre_outcomes = [*round0, critique]
        base_fields: dict[str, Any] = {
            "adapter": adapter,
            "model_id": ctx.judge_model.id,
            "upstream_model_id": ctx.judge_model.upstream_model_id,
            "provider_name": ctx.judge_provider.name,
            "pre_outcomes": pre_outcomes,
            "base_usage": sum_usage(pre_outcomes),
        }
        verdict = parse_verdict(critique.response_content)
        conf_history = [verdict.confidence] if verdict.parsed else []
        if self._should_finish(verdict, 0, conf_history, params):
            # 快路径：第 0 轮即共识，调用形态与 council 相同（M+2）
            request, payload = self._assemble_final(ctx, session, ok, critique.response_content)
            return StreamPlan(request=request, payload=payload, **base_fields)

        state = IterationState(
            total_rounds=params["max_rounds"],
            completed_rounds=1,
            progress_comments=params["progress_comments"],
            run=lambda: self._iterate(
                state,
                ctx,
                adapter,
                session,
                ok,
                verdict,
                conf_history,
                critique.response_content,
                params,
                judge_strict=judge_strict,
                adapter_factory=adapter_factory,
            ),
        )
        return StreamPlan(iteration=state, **base_fields)
