"""对外网关（/v1/*）：OpenAI 兼容端点。model 解析：Pipeline 名优先 → 真实模型透传（决策 D3）。"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import AdapterError, LlmRequest, LlmUsage, NormalizedMessage
from app.adapters.factory import build_adapter
from app.logging_svc import finish_request, record_call, start_request
from app.orm import LlmModel, Provider
from app.repos import (
    find_enabled_model_by_upstream_id,
    find_enabled_pipeline_by_name,
)
from app.settings import settings

router = APIRouter(tags=["gateway"])

UNSUPPORTED_FIELDS = (
    "tools",
    "tool_choice",
    "functions",
    "function_call",
    "logprobs",
    "logit_bias",
)
VALID_ROLES = ("system", "user", "assistant")


def openai_error(
    status: int,
    message: str,
    err_type: str = "invalid_request_error",
    param: str | None = None,
    code: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "param": param, "code": code}},
    )


def _sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _elapsed_ms(t0: float) -> int:
    """请求级耗时（请求入口 t0 → 现在）；调用行耗时不准用它。"""
    return int((time.perf_counter() - t0) * 1000)


def ice_progress_comment(k: int, total: int) -> str:
    """ICE 迭代期 SSE 注释行（`:` 开头为 SSE 规范注释，OpenAI SDK 忽略，不占 data 序列）。"""
    return f": ice round {k}/{total}\n\n"


@router.get("/v1/models")
async def list_models(request: Request) -> Response:
    from app.orm import LlmModel as M
    from app.orm import Pipeline as P
    from app.repos import Repository

    factory = request.app.state.session_factory
    async with factory() as session:
        pipelines = await Repository(session, P).list()
        models = await Repository(session, M).list()
    data = [
        {"id": p.name, "object": "model", "created": 0, "owned_by": "muti_llm"}
        for p in pipelines
        if p.enabled
    ] + [
        {"id": m.upstream_model_id, "object": "model", "created": 0, "owned_by": "muti_llm"}
        for m in models
        if m.enabled
    ]
    return JSONResponse({"object": "list", "data": data})


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, background: BackgroundTasks) -> Response:
    t0 = time.perf_counter()  # 请求级计时起点（收到外部请求），贯穿所有终态路径
    try:
        body = await request.json()
    except Exception:
        return openai_error(400, "请求体不是合法 JSON")
    client_ip = request.client.host if request.client else ""
    response, _ = await execute_chat(request.app, background, body, client_ip, t0)
    return response


async def execute_chat(
    app: Any, background: BackgroundTasks, body: dict[str, Any], client_ip: str, t0: float
) -> tuple[Response, int | None]:
    """网关核心执行（校验 → Pipeline/透传路由 → 响应）。返回 (response, trace_id)。

    /v1/chat/completions（认证后）与 Playground（管理 API）共用同一执行路径。
    """

    class _AppRequest:
        """给依赖 request 的内部函数提供 app.state 的轻量代理。"""

        def __init__(self, app: Any, client_ip: str) -> None:
            self.app = app
            self.client = type("Client", (), {"host": client_ip})()

    request = _AppRequest(app, client_ip)  # noqa: F841 - 供下方函数签名兼容

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return openai_error(400, "messages 必须为非空数组", param="messages"), None
    for m in messages:
        if (
            not isinstance(m, dict)
            or m.get("role") not in VALID_ROLES
            or not isinstance(m.get("content"), str)
        ):
            return openai_error(
                400,
                "每条 message 需含 role(system|user|assistant) 与字符串 content",
                param="messages",
            ), None
    for unsupported in UNSUPPORTED_FIELDS:
        if body.get(unsupported) is not None:
            return openai_error(400, f"不支持的能力: {unsupported}", param=unsupported), None

    model_field = body.get("model")
    if not isinstance(model_field, str) or not model_field:
        return openai_error(400, "model 必填", param="model"), None

    params: dict[str, Any] = {}
    for key in ("temperature", "max_tokens", "top_p"):
        value = body.get(key)
        if value is not None:
            params[key] = value
    stream = bool(body.get("stream"))
    # 过程流式（T38/T39）：仅 pipeline 流式生效；None=请求体未显式给 → 用 pipeline 默认
    stream_process_raw = body.get("stream_process")

    async with app.state.session_factory() as session:
        pipeline = await find_enabled_pipeline_by_name(session, model_field)
        if pipeline is not None:
            # 请求体显式给了（含 false）就用请求体的值，否则用 pipeline 级默认
            stream_process = (
                bool(stream_process_raw)
                if stream_process_raw is not None
                else bool(pipeline.stream_process)
            )
            return await _run_pipeline(
                app,
                background,
                session,
                body,
                params,
                stream,
                pipeline,
                client_ip,
                t0,
                stream_process=stream_process,
            )

        model_row = await find_enabled_model_by_upstream_id(session, model_field)
        if model_row is None:
            return (
                openai_error(404, f"model '{model_field}' not found", code="model_not_found"),
                None,
            )
        provider = await session.get(Provider, model_row.provider_id)
        if provider is None or not provider.enabled:
            return (
                openai_error(
                    502, f"model '{model_field}' 的 provider 不可用", err_type="server_error"
                ),
                None,
            )

        return await _passthrough(
            app, background, session, body, model_row, provider, params, stream, client_ip, t0
        )


# ---- Pipeline / 策略执行（T16/T17） ------------------------------------------------


async def _build_strategy_context(
    session: AsyncSession,
    body: dict[str, Any],
    params: dict[str, Any],
    pipeline: Any,
    fernet_key: bytes,
) -> Any:
    from sqlalchemy import select

    from app.orm import PipelineMember
    from app.strategies.base import MemberSpec, StrategyContext

    member_rows = (
        await session.execute(
            select(PipelineMember)
            .where(PipelineMember.pipeline_id == pipeline.id)
            .order_by(PipelineMember.sort_order, PipelineMember.id)
        )
    ).scalars()

    specs = []
    for member_row in member_rows:
        model = await session.get(LlmModel, member_row.model_id)
        if model is None or not model.enabled:
            continue
        provider = await session.get(Provider, model.provider_id)
        if provider is None or not provider.enabled:
            continue
        specs.append(MemberSpec(member=member_row, model=model, provider=provider))
    if not specs:
        from app.strategies.base import AllMembersFailed

        raise AllMembersFailed(f"pipeline '{pipeline.name}' 无可用成员（成员/模型/供应商被停用）")

    judge_model = await session.get(LlmModel, pipeline.judge_model_id)
    if judge_model is None or not judge_model.enabled:
        from app.strategies.base import AllMembersFailed

        raise AllMembersFailed(f"pipeline '{pipeline.name}' 裁判模型不可用")
    judge_provider = await session.get(Provider, judge_model.provider_id)
    if judge_provider is None or not judge_provider.enabled:
        from app.strategies.base import AllMembersFailed

        raise AllMembersFailed(f"pipeline '{pipeline.name}' 裁判供应商不可用")

    return StrategyContext(
        body=body,
        user_params=params,
        pipeline=pipeline,
        members=specs,
        judge_model=judge_model,
        judge_provider=judge_provider,
        fernet_key=fernet_key,
    )


async def _record_outcomes(session: AsyncSession, request_id: int, outcomes: list) -> None:
    from app.logging_svc import record_call

    for outcome in outcomes:
        await record_call(session, request_id=request_id, **outcome.to_log_kwargs())


async def _detached_pipeline_persist(
    session_factory: Any,
    trace_id: int,
    work: asyncio.Future[Any],
    t0: float,
) -> None:
    """断连续跑（pipeline 非流式）：等待策略任务收尾，按真实终态落库，绝不留 pending。"""
    from app.strategies.base import StrategyExecutionError

    try:
        result = await work
    except StrategyExecutionError as exc:
        failed_calls = list(exc.members)
        if exc.critique is not None:
            failed_calls.append(exc.critique)
        if exc.judge is not None:
            failed_calls.append(exc.judge)
        async with session_factory() as s:
            await _record_outcomes(s, trace_id, failed_calls)
            await finish_request(
                s, trace_id, status="failed", total_duration_ms=_elapsed_ms(t0)
            )
            await s.commit()
        return
    except Exception:
        # 兜底：非预期异常也必须落终态，避免 trace 永远 pending
        async with session_factory() as s:
            await finish_request(
                s, trace_id, status="failed", total_duration_ms=_elapsed_ms(t0)
            )
            await s.commit()
        return
    async with session_factory() as s:
        await _record_outcomes(s, trace_id, result.calls)
        duration = _elapsed_ms(t0)
        await finish_request(
            s,
            trace_id,
            status="degraded" if result.degraded else "success",
            response_content=result.final_content,
            response_finish_reason=result.final_finish_reason,
            total_duration_ms=duration,
            first_token_ms=duration,
            total_prompt_tokens=result.usage.prompt_tokens,
            total_completion_tokens=result.usage.completion_tokens,
        )
        await s.commit()


async def _detached_passthrough_persist(
    session_factory: Any,
    trace_id: int,
    work: asyncio.Future[Any],
    payload: dict[str, Any],
    model_row: LlmModel,
    provider: Provider,
    start: float,
    t0: float,
) -> None:
    """断连续跑（透传非流式）：等待上游任务收尾，按真实终态落库，绝不留 pending。"""
    try:
        result = await work
    except AdapterError as exc:
        duration = int((time.perf_counter() - start) * 1000)
        async with session_factory() as s:
            await record_call(
                s,
                request_id=trace_id,
                role="passthrough",
                model_id=model_row.id,
                upstream_model_id=model_row.upstream_model_id,
                provider_name=provider.name,
                request_payload=payload,
                status="failed",
                error_message=str(exc),
                duration_ms=duration,
            )
            await finish_request(s, trace_id, status="failed", total_duration_ms=_elapsed_ms(t0))
            await s.commit()
        return
    except Exception:
        async with session_factory() as s:
            await finish_request(
                s, trace_id, status="failed", total_duration_ms=_elapsed_ms(t0)
            )
            await s.commit()
        return
    async with session_factory() as s:
        await record_call(
            s,
            request_id=trace_id,
            role="passthrough",
            model_id=model_row.id,
            upstream_model_id=model_row.upstream_model_id,
            provider_name=provider.name,
            request_payload=payload,
            response_content=result.content,
            duration_ms=result.duration_ms,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
        )
        duration = _elapsed_ms(t0)
        await finish_request(
            s,
            trace_id,
            status="success",
            response_content=result.content,
            response_finish_reason="stop",
            total_duration_ms=duration,
            first_token_ms=duration,
            total_prompt_tokens=result.usage.prompt_tokens,
            total_completion_tokens=result.usage.completion_tokens,
        )
        await s.commit()


async def _run_pipeline(
    app: Any,
    background: BackgroundTasks,
    session: AsyncSession,
    body: dict[str, Any],
    params: dict[str, Any],
    stream: bool,
    pipeline: Any,
    client_ip: str,
    t0: float,
    *,
    stream_process: bool = False,
) -> tuple[Response, int]:
    from app.strategies import get_strategy
    from app.strategies.base import StrategyExecutionError

    trace = await start_request(
        session,
        client_model_field=body["model"],
        pipeline_name=pipeline.name,
        messages=body["messages"],
        params=params,
        client_ip=client_ip,
    )

    try:
        ctx = await _build_strategy_context(session, body, params, pipeline, app.state.fernet_key)
        strategy = get_strategy(pipeline.strategy)()
        if stream:
            if stream_process:
                return await _pipeline_stream_process(
                    app, background, session, trace, ctx, strategy, t0
                )
            return await _pipeline_stream(app, background, session, trace, ctx, strategy, t0)
        await session.commit()  # 落 pending 并固定 ctx 内 ORM 属性：断连后续跑/取消才可写终态
        work = asyncio.ensure_future(strategy.run(ctx))
        try:
            result = await work
        except asyncio.CancelledError:
            if settings.detach_on_disconnect:
                # 断连续跑：响应已无法再交付，上游跑完由后台任务落真实终态
                asyncio.create_task(
                    _detached_pipeline_persist(app.state.session_factory, trace.id, work, t0)
                )
            else:
                work.cancel()
                async with app.state.session_factory() as s:
                    await finish_request(
                        s, trace.id, status="client_cancelled", total_duration_ms=_elapsed_ms(t0)
                    )
                    await s.commit()
            raise
    except StrategyExecutionError as exc:
        failed_calls = list(exc.members)
        if exc.critique is not None:
            failed_calls.append(exc.critique)
        if exc.judge is not None:
            failed_calls.append(exc.judge)
        await _record_outcomes(session, trace.id, failed_calls)
        await finish_request(
            session, trace.id, status="failed", total_duration_ms=_elapsed_ms(t0)
        )
        await session.commit()
        return openai_error(502, f"策略执行失败: {exc}", err_type="upstream_error"), trace.id

    await _record_outcomes(session, trace.id, result.calls)
    status = "degraded" if result.degraded else "success"
    duration = _elapsed_ms(t0)
    await finish_request(
        session,
        trace.id,
        status=status,
        response_content=result.final_content,
        response_finish_reason=result.final_finish_reason,
        total_duration_ms=duration,
        # 非流式一次性响应：首 token 与完成同时到达
        first_token_ms=duration,
        total_prompt_tokens=result.usage.prompt_tokens,
        total_completion_tokens=result.usage.completion_tokens,
    )
    await session.commit()

    response = _completion_response(
        body["model"],
        result.final_content,
        result.usage.prompt_tokens,
        result.usage.completion_tokens,
    )
    if result.degraded:
        response["degraded"] = True
    return JSONResponse(response), trace.id


def make_chunker(response_id: str, created: int, model: str):
    """OpenAI 兼容 SSE chunk 构造（透传与策略流式共用）。"""

    def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
        return {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    return chunk


_END: Any = object()

# 成员/评论原文里可能自带 <think> 标签（思考型模型内联思考），会破坏下游客户端
# （如 Unsloth Studio）朴素的 think 块字符串解析，导致过程内容泄漏进正式回复。
_THINK_TAG_RE = re.compile(r"(</?)\s*(think)(\s[^>]*)?>", re.IGNORECASE)


def _sanitize_reasoning(text: str) -> str:
    """reasoning_content 消毒：用零宽空格拆断 think 标签，视觉不变但不再被解析。"""
    return _THINK_TAG_RE.sub(lambda m: f"{m.group(1)}​think{m.group(3) or ''}>", text)


async def _drain_with_heartbeat(
    queue: asyncio.Queue[Any], interval: float
) -> AsyncIterator[str]:
    """queue.get() 带超时：超时发 ': ping'，收到 _END 结束；interval<=0 时纯透传。"""
    if interval <= 0:
        while (item := await queue.get()) is not _END:
            yield item
        return
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), interval)
        except TimeoutError:
            yield ": ping\n\n"
            continue
        if item is _END:
            return
        yield item


async def _pipeline_stream(
    app: Any,
    background: BackgroundTasks,
    session: AsyncSession,
    trace: Any,
    ctx: Any,
    strategy: Any,
    t0: float,
) -> tuple[Response, int]:
    plan = await strategy.prepare_stream(ctx)

    # 成员阶段已定：先落库
    await _record_outcomes(session, trace.id, plan.pre_outcomes)
    await session.commit()

    if plan.iteration is not None:
        # ICE 迭代路径（D15）：注释行保活 → 逐轮执行 → 终局流式转发
        return await _pipeline_stream_iterative(app, background, trace, ctx, plan, t0)

    session_factory = app.state.session_factory
    response_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    chunk = make_chunker(response_id, int(time.time()), ctx.body["model"])
    outcome = StreamOutcome()
    start = time.perf_counter()  # 流段本地计时：仅供裁判调用行 duration_ms
    trace_id: int = trace.id
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def pump() -> None:
        outcome.status = "client_cancelled"
        try:
            queue.put_nowait(_sse(chunk({"role": "assistant"})))
            async for event in plan.adapter.stream_events_timed(plan.request):
                if event.usage is not None:
                    outcome.usage = event.usage
                if event.text:
                    if outcome.first_token_ms == 0:
                        outcome.first_token_ms = _elapsed_ms(t0)
                    outcome.parts.append(event.text)
                    queue.put_nowait(_sse(chunk({"content": event.text})))
            queue.put_nowait(_sse(chunk({}, finish_reason="stop")))
            queue.put_nowait("data: [DONE]\n\n")
            outcome.status = "success"
        except AdapterError as exc:
            outcome.status = "failed"
            outcome.error = str(exc)
        finally:
            outcome.duration_ms = int((time.perf_counter() - start) * 1000)
            queue.put_nowait(_END)

    pump_task = asyncio.create_task(pump())

    async def event_stream() -> AsyncIterator[str]:
        drained = False
        try:
            async for line in _drain_with_heartbeat(queue, settings.sse_heartbeat_seconds):
                yield line
            drained = True
        finally:
            # detach 关闭时断开即取消上游；开启时仅 ferry 退出，pump 续跑由 persist 收尾
            if not settings.detach_on_disconnect:
                pump_task.cancel()
                if not drained:
                    # 客户端未收到完整流：即使 pump 已跑完，请求级状态仍按取消落库（消除竞态）
                    outcome.status = "client_cancelled"

    async def persist() -> None:
        from app.strategies.base import CallOutcome

        await asyncio.gather(pump_task, return_exceptions=True)
        content = "".join(outcome.parts)
        judge_usage = outcome.usage or LlmUsage()
        judge = CallOutcome(
            role="judge",
            model_id=plan.model_id,
            upstream_model_id=plan.upstream_model_id,
            provider_name=plan.provider_name,
            request_payload=plan.payload,
            response_content=content if outcome.status != "failed" else "",
            status=outcome.status,
            error_message=outcome.error,
            duration_ms=outcome.duration_ms,
            usage=judge_usage,
        )
        async with session_factory() as s:
            await _record_outcomes(s, trace_id, [judge])
            await finish_request(
                s,
                trace_id,
                status=outcome.status,
                response_content=content,
                total_duration_ms=_elapsed_ms(t0),
                first_token_ms=outcome.first_token_ms,
                total_prompt_tokens=plan.base_usage.prompt_tokens + judge_usage.prompt_tokens,
                total_completion_tokens=plan.base_usage.completion_tokens
                + judge_usage.completion_tokens,
            )
            await s.commit()

    background.add_task(persist)
    return StreamingResponse(event_stream(), media_type="text/event-stream"), trace.id


async def _pipeline_stream_iterative(
    app: Any,
    background: BackgroundTasks,
    trace: Any,
    ctx: Any,
    plan: Any,
    t0: float,
) -> tuple[Response, int]:
    """ICE 迭代流式路径（D15）：注释行保活 → 逐轮执行（策略迭代状态）→ 终局裁决 SSE 转发。

    取消传播：detach 关闭时客户端断开经 ferry 取消 pump，CancelledError 传入迭代生成器
    取消在飞轮次任务；detach 开启（默认）时 pump 续跑到底，persist 落真实终态。
    """
    from app.strategies.base import CallOutcome, StrategyExecutionError

    session_factory = app.state.session_factory
    chunk = make_chunker(f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time()), ctx.body["model"])
    outcome = StreamOutcome()
    start = time.perf_counter()  # 本地计时：仅供裁判调用行 duration_ms
    trace_id: int = trace.id
    iteration = plan.iteration
    iter_outcomes: list = []
    final_plan: Any = None
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def pump() -> None:
        nonlocal final_plan
        outcome.status = "client_cancelled"  # 未到终态即结束 → 视为取消
        try:
            # 首条注释行仅在进入第 1 轮迭代时发射（第 0 轮已在开流前完成）
            if iteration.progress_comments:
                queue.put_nowait(
                    ice_progress_comment(iteration.completed_rounds, iteration.total_rounds)
                )
            async for step in iteration.run():
                iter_outcomes.extend(step.members)
                if step.critique is not None:
                    iter_outcomes.append(step.critique)
                if step.members and iteration.progress_comments:
                    queue.put_nowait(
                        ice_progress_comment(step.round_no + 1, iteration.total_rounds)
                    )
            final_plan = iteration.final_plan
            queue.put_nowait(_sse(chunk({"role": "assistant"})))
            async for event in final_plan.adapter.stream_events_timed(final_plan.request):
                if event.usage is not None:
                    outcome.usage = event.usage
                if event.text:
                    if outcome.first_token_ms == 0:
                        outcome.first_token_ms = _elapsed_ms(t0)
                    outcome.parts.append(event.text)
                    queue.put_nowait(_sse(chunk({"content": event.text})))
            queue.put_nowait(_sse(chunk({}, finish_reason="stop")))
            queue.put_nowait("data: [DONE]\n\n")
            outcome.status = "success"
        except (AdapterError, StrategyExecutionError) as exc:
            # 终局流式中途失败 / 迭代期 strict 评论失败 → 终止流，按失败落库
            outcome.status = "failed"
            outcome.error = str(exc)
        finally:
            outcome.duration_ms = int((time.perf_counter() - start) * 1000)
            queue.put_nowait(_END)

    pump_task = asyncio.create_task(pump())

    async def event_stream() -> AsyncIterator[str]:
        drained = False
        try:
            async for line in _drain_with_heartbeat(queue, settings.sse_heartbeat_seconds):
                yield line
            drained = True
        finally:
            if not settings.detach_on_disconnect:
                pump_task.cancel()
                if not drained:
                    outcome.status = "client_cancelled"

    async def persist() -> None:
        await asyncio.gather(pump_task, return_exceptions=True)
        content = "".join(outcome.parts)
        prompt_tokens = plan.base_usage.prompt_tokens
        completion_tokens = plan.base_usage.completion_tokens
        for o in iter_outcomes:
            prompt_tokens += o.usage.prompt_tokens
            completion_tokens += o.usage.completion_tokens
        final_usage = outcome.usage or LlmUsage()
        prompt_tokens += final_usage.prompt_tokens
        completion_tokens += final_usage.completion_tokens
        async with session_factory() as s:
            if iter_outcomes:
                await _record_outcomes(s, trace_id, iter_outcomes)
            if final_plan is not None:  # 终局已开始才落 judge 行（迭代期取消则无此行）
                judge = CallOutcome(
                    role="judge",
                    model_id=final_plan.model_id,
                    upstream_model_id=final_plan.upstream_model_id,
                    provider_name=final_plan.provider_name,
                    request_payload=final_plan.payload or {},
                    response_content=content if outcome.status != "failed" else "",
                    status=outcome.status,
                    error_message=outcome.error,
                    duration_ms=outcome.duration_ms,
                    usage=final_usage,
                )
                await _record_outcomes(s, trace_id, [judge])
            await finish_request(
                s,
                trace_id,
                status=outcome.status,
                response_content=content,
                total_duration_ms=_elapsed_ms(t0),
                first_token_ms=outcome.first_token_ms,
                total_prompt_tokens=prompt_tokens,
                total_completion_tokens=completion_tokens,
            )
            await s.commit()

    background.add_task(persist)
    return StreamingResponse(event_stream(), media_type="text/event-stream"), trace_id


async def _pipeline_stream_process(
    app: Any,
    background: BackgroundTasks,
    session: AsyncSession,
    trace: Any,
    ctx: Any,
    strategy: Any,
    t0: float,
) -> tuple[Response, int]:
    """过程流式路径（T38 stream_process=true）：成员/评论完成即发 reasoning chunk，终局照旧。

    与迭代路径同骨架（pump/ferry/persist + detach/心跳/t0 计时）；区别：响应不等前置阶段
    全部完成——首个成员成功即返回（TTFT 提前到首成员完成）；前置明细不做开流前预写，
    由 persist 从 ProcessEvent 累计落库。首个成功成员前 pump 失败 → 暂存异常重抛，
    走 _run_pipeline 既有 502 JSON 路径（与今日全非流式/流式失败契约一致）。
    """
    from app.strategies.base import FinalPlanReady, StrategyExecutionError

    await session.commit()  # 先落 pending 状态的 request_logs（pre_outcomes 由 persist 补写）

    session_factory = app.state.session_factory
    chunk = make_chunker(f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time()), ctx.body["model"])
    outcome = StreamOutcome()
    start = time.perf_counter()  # 本地计时：仅供裁判调用行 duration_ms
    trace_id: int = trace.id
    queue: asyncio.Queue[Any] = asyncio.Queue()
    pre_outcomes: list[Any] = []
    final_plan: Any = None
    first_success = asyncio.Event()
    stashed: list[Exception] = []
    role_sent = False

    async def pump() -> None:
        nonlocal final_plan, role_sent
        outcome.status = "client_cancelled"  # 未到终态即结束 → 视为取消
        try:
            async for ev in strategy.run_stream_process(ctx):
                if isinstance(ev, FinalPlanReady):
                    final_plan = ev.plan
                    # 终局转发：与常规流式路径同形态（role → content → stop → [DONE]）
                    queue.put_nowait(_sse(chunk({"role": "assistant"})))
                    async for event in final_plan.adapter.stream_events_timed(final_plan.request):
                        if event.usage is not None:
                            outcome.usage = event.usage
                        if event.text:
                            if outcome.first_token_ms == 0:
                                outcome.first_token_ms = _elapsed_ms(t0)
                            outcome.parts.append(event.text)
                            queue.put_nowait(_sse(chunk({"content": event.text})))
                    queue.put_nowait(_sse(chunk({}, finish_reason="stop")))
                    queue.put_nowait("data: [DONE]\n\n")
                    outcome.status = "success"
                    continue
                pre_outcomes.append(ev.outcome)
                header = f"【成员 {ev.label}】\n" if ev.kind == "member" else f"【{ev.label}】\n"
                first_delta: dict[str, Any] = {"reasoning_content": header}
                if not role_sent:
                    # 与 content 路径一致：流的首个 chunk 携带 role
                    first_delta["role"] = "assistant"
                    role_sent = True
                queue.put_nowait(_sse(chunk(first_delta)))
                if ev.outcome.status == "success":
                    content = _sanitize_reasoning(ev.outcome.response_content)
                    for line in content.splitlines(keepends=True):
                        queue.put_nowait(_sse(chunk({"reasoning_content": line})))
                    if ev.kind == "member":
                        first_success.set()
                else:
                    notice = f"（调用失败：{ev.outcome.error_message or ev.outcome.status}）\n"
                    notice = _sanitize_reasoning(notice)
                    queue.put_nowait(_sse(chunk({"reasoning_content": notice})))
        except (AdapterError, StrategyExecutionError) as exc:
            if not first_success.is_set():
                # 开流前失败：暂存并唤醒网关 → 重抛走既有 502 JSON 路径
                stashed.append(exc)
                first_success.set()
            else:
                # 流内失败（评论/终局/strict 成员）：终止流不发 [DONE]，按失败落库
                outcome.status = "failed"
                outcome.error = str(exc)
        finally:
            outcome.duration_ms = int((time.perf_counter() - start) * 1000)
            queue.put_nowait(_END)

    pump_task = asyncio.create_task(pump())

    # 响应起点：首个成员成功（TTFT 提前）或 pump 终止（开流前失败 → 重抛）
    waiter = asyncio.create_task(first_success.wait())
    await asyncio.wait({waiter, pump_task}, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    if stashed:
        await asyncio.gather(pump_task, return_exceptions=True)
        raise stashed[0]
    if pump_task.done() and not first_success.is_set():
        # 非预期异常：同样在开流前抛出（500），不返回空 SSE
        exc = pump_task.exception()
        if exc is not None:
            raise exc

    async def event_stream() -> AsyncIterator[str]:
        drained = False
        try:
            async for line in _drain_with_heartbeat(queue, settings.sse_heartbeat_seconds):
                yield line
            drained = True
        finally:
            # detach 关闭时断开即取消上游；开启时仅 ferry 退出，pump 续跑由 persist 收尾
            if not settings.detach_on_disconnect:
                pump_task.cancel()
                if not drained:
                    # 客户端未收到完整流：即使 pump 已跑完，请求级状态仍按取消落库（消除竞态）
                    outcome.status = "client_cancelled"

    async def persist() -> None:
        from app.strategies.base import CallOutcome

        await asyncio.gather(pump_task, return_exceptions=True)
        content = "".join(outcome.parts)
        prompt_tokens = 0
        completion_tokens = 0
        for o in pre_outcomes:
            prompt_tokens += o.usage.prompt_tokens
            completion_tokens += o.usage.completion_tokens
        final_usage = outcome.usage or LlmUsage()
        prompt_tokens += final_usage.prompt_tokens
        completion_tokens += final_usage.completion_tokens
        async with session_factory() as s:
            if pre_outcomes:
                await _record_outcomes(s, trace_id, pre_outcomes)
            if final_plan is not None:  # 终局已开始才落 judge 行（前置期取消则无此行）
                judge = CallOutcome(
                    role="judge",
                    model_id=final_plan.model_id,
                    upstream_model_id=final_plan.upstream_model_id,
                    provider_name=final_plan.provider_name,
                    request_payload=final_plan.payload or {},
                    response_content=content if outcome.status != "failed" else "",
                    status=outcome.status,
                    error_message=outcome.error,
                    duration_ms=outcome.duration_ms,
                    usage=final_usage,
                )
                await _record_outcomes(s, trace_id, [judge])
            await finish_request(
                s,
                trace_id,
                status=outcome.status,
                response_content=content,
                total_duration_ms=_elapsed_ms(t0),
                first_token_ms=outcome.first_token_ms,
                total_prompt_tokens=prompt_tokens,
                total_completion_tokens=completion_tokens,
            )
            await s.commit()

    background.add_task(persist)
    return StreamingResponse(event_stream(), media_type="text/event-stream"), trace_id


def _merged_params(model_row: LlmModel, params: dict[str, Any]) -> dict[str, Any]:
    """参数合并优先级：模型默认 < 请求参数（FR/tech-plan UT-14-7 同语义）。"""
    merged = dict(model_row.default_params or {})
    merged.update(params)
    return merged


def _llm_request(
    model_row: LlmModel, body: dict[str, Any], merged: dict[str, Any], stream: bool
) -> tuple[LlmRequest, dict[str, Any]]:
    """构造归一化请求与实际发出的 payload（入日志/与快照 hash 一致）。"""
    openai_messages = [{"role": m["role"], "content": m["content"]} for m in body["messages"]]
    payload: dict[str, Any] = {
        "model": model_row.upstream_model_id,
        "messages": openai_messages,
        "stream": stream,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    llm_request = LlmRequest(
        model=model_row.upstream_model_id,
        messages=[NormalizedMessage(role=m["role"], content=m["content"]) for m in openai_messages],
    )
    for key in ("temperature", "max_tokens", "top_p"):
        if merged.get(key) is not None:
            payload[key] = merged[key]
            setattr(llm_request, key, merged[key])
    return llm_request, payload


def _completion_response(
    model: str, content: str, prompt_tokens: int, completion_tokens: int
) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def _passthrough(
    app: Any,
    background: BackgroundTasks,
    session: AsyncSession,
    body: dict[str, Any],
    model_row: LlmModel,
    provider: Provider,
    params: dict[str, Any],
    stream: bool,
    client_ip: str,
    t0: float,
) -> tuple[Response, int]:
    trace = await start_request(
        session,
        client_model_field=body["model"],
        pipeline_name="",
        messages=body["messages"],
        params=params,
        client_ip=client_ip,
    )

    merged = _merged_params(model_row, params)
    # 上游一律流式调用（决策 D8）：stream 只决定客户端拿到 SSE 还是聚合 JSON
    llm_request, payload = _llm_request(model_row, body, merged, stream=True)
    adapter = build_adapter(
        provider,
        fernet_key=app.state.fernet_key,
        timeout_seconds=float(merged.get("timeout_seconds", 120)),
        max_retries=int(merged.get("max_retries", 1)),
    )

    if stream:
        return await _passthrough_stream(
            app, background, session, trace, model_row, provider, payload, llm_request, adapter, t0
        )

    start = time.perf_counter()  # 调用行本地计时：仅供 passthrough 调用行 duration_ms
    await session.commit()  # 落 pending：断连后续跑/取消才能写终态（对齐流式路径）
    work = asyncio.ensure_future(adapter.complete(llm_request))
    try:
        result = await work
    except asyncio.CancelledError:
        if settings.detach_on_disconnect:
            asyncio.create_task(
                _detached_passthrough_persist(
                    app.state.session_factory,
                    trace.id,
                    work,
                    payload,
                    model_row,
                    provider,
                    start,
                    t0,
                )
            )
        else:
            work.cancel()
            async with app.state.session_factory() as s:
                await finish_request(
                    s, trace.id, status="client_cancelled", total_duration_ms=_elapsed_ms(t0)
                )
                await s.commit()
        raise
    except AdapterError as exc:
        duration = int((time.perf_counter() - start) * 1000)
        await record_call(
            session,
            request_id=trace.id,
            role="passthrough",
            model_id=model_row.id,
            upstream_model_id=model_row.upstream_model_id,
            provider_name=provider.name,
            request_payload=payload,
            status="failed",
            error_message=str(exc),
            duration_ms=duration,
        )
        await finish_request(
            session, trace.id, status="failed", total_duration_ms=_elapsed_ms(t0)
        )
        await session.commit()
        return openai_error(502, f"上游调用失败: {exc}", err_type="upstream_error"), trace.id

    await record_call(
        session,
        request_id=trace.id,
        role="passthrough",
        model_id=model_row.id,
        upstream_model_id=model_row.upstream_model_id,
        provider_name=provider.name,
        request_payload=payload,
        response_content=result.content,
        duration_ms=result.duration_ms,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
    )
    duration = _elapsed_ms(t0)
    await finish_request(
        session,
        trace.id,
        status="success",
        response_content=result.content,
        response_finish_reason="stop",
        total_duration_ms=duration,
        first_token_ms=duration,
        total_prompt_tokens=result.usage.prompt_tokens,
        total_completion_tokens=result.usage.completion_tokens,
    )
    await session.commit()
    return JSONResponse(
        _completion_response(
            body["model"],
            result.content,
            result.usage.prompt_tokens,
            result.usage.completion_tokens,
        )
    ), trace.id


@dataclass
class StreamOutcome:
    """流式结果载体：generator 只更新内存，由 background task 持久化（断开安全）。

    usage 为终局流（passthrough 为唯一调用，pipeline 为裁判调用）结束时的汇总事件，
    上游不支持 usage 时保持 None（T41：此前该事件被丢弃，流式 token 全部记 0）。
    """

    status: str = "client_cancelled"
    parts: list[str] = field(default_factory=list)
    error: str = ""
    duration_ms: int = 0
    first_token_ms: int = 0
    usage: LlmUsage | None = None


async def _passthrough_stream(
    app: Any,
    background: BackgroundTasks,
    session: AsyncSession,
    trace: Any,
    model_row: LlmModel,
    provider: Provider,
    payload: dict[str, Any],
    llm_request: LlmRequest,
    adapter: Any,
    t0: float,
) -> tuple[Response, int]:
    await session.commit()  # 先落 pending 状态的 request_logs

    session_factory = app.state.session_factory
    chunk = make_chunker(f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time()), payload["model"])

    outcome = StreamOutcome()
    start = time.perf_counter()  # 流段本地计时：仅供 passthrough 调用行 duration_ms
    trace_id: int = trace.id
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def pump() -> None:
        outcome.status = "client_cancelled"  # 未到终态即结束 → 视为取消
        try:
            queue.put_nowait(_sse(chunk({"role": "assistant"})))
            async for event in adapter.stream_events_timed(llm_request):
                if event.usage is not None:
                    outcome.usage = event.usage
                if event.text:
                    if outcome.first_token_ms == 0:
                        outcome.first_token_ms = _elapsed_ms(t0)
                    outcome.parts.append(event.text)
                    queue.put_nowait(_sse(chunk({"content": event.text})))
            queue.put_nowait(_sse(chunk({}, finish_reason="stop")))
            queue.put_nowait("data: [DONE]\n\n")
            outcome.status = "success"
        except AdapterError as exc:
            outcome.status = "failed"
            outcome.error = str(exc)
        finally:
            outcome.duration_ms = int((time.perf_counter() - start) * 1000)
            queue.put_nowait(_END)

    pump_task = asyncio.create_task(pump())

    async def event_stream() -> AsyncIterator[str]:
        drained = False
        try:
            async for line in _drain_with_heartbeat(queue, settings.sse_heartbeat_seconds):
                yield line
            drained = True
        finally:
            if not settings.detach_on_disconnect:
                pump_task.cancel()
                if not drained:
                    outcome.status = "client_cancelled"

    async def persist() -> None:
        await asyncio.gather(pump_task, return_exceptions=True)
        async with session_factory() as s:
            content = "".join(outcome.parts)
            usage = outcome.usage or LlmUsage()
            await record_call(
                s,
                request_id=trace_id,
                role="passthrough",
                model_id=model_row.id,
                upstream_model_id=model_row.upstream_model_id,
                provider_name=provider.name,
                request_payload=payload,
                response_content=content if outcome.status != "failed" else "",
                status=outcome.status
                if outcome.status != "client_cancelled"
                else "client_cancelled",
                error_message=outcome.error,
                duration_ms=outcome.duration_ms,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cached_tokens=usage.cached_tokens,
                cache_write_tokens=usage.cache_write_tokens,
            )
            await finish_request(
                s,
                trace_id,
                status=outcome.status,
                response_content=content,
                total_duration_ms=_elapsed_ms(t0),
                first_token_ms=outcome.first_token_ms,
                total_prompt_tokens=usage.prompt_tokens,
                total_completion_tokens=usage.completion_tokens,
            )
            await s.commit()

    background.add_task(persist)
    return StreamingResponse(event_stream(), media_type="text/event-stream"), trace_id
