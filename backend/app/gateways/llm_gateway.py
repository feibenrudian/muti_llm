"""对外网关（/v1/*）：OpenAI 兼容端点。model 解析：Pipeline 名优先 → 真实模型透传（决策 D3）。"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import AdapterError, LlmRequest, NormalizedMessage
from app.adapters.factory import build_adapter
from app.logging_svc import finish_request, record_call, start_request
from app.orm import LlmModel, Provider
from app.repos import (
    find_enabled_model_by_upstream_id,
    find_enabled_pipeline_by_name,
)

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
    try:
        body = await request.json()
    except Exception:
        return openai_error(400, "请求体不是合法 JSON")
    client_ip = request.client.host if request.client else ""
    response, _ = await execute_chat(request.app, background, body, client_ip)
    return response


async def execute_chat(
    app: Any, background: BackgroundTasks, body: dict[str, Any], client_ip: str
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

    async with app.state.session_factory() as session:
        pipeline = await find_enabled_pipeline_by_name(session, model_field)
        if pipeline is not None:
            return await _run_pipeline(
                app, background, session, body, params, stream, pipeline, client_ip
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
            app, background, session, body, model_row, provider, params, stream, client_ip
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


async def _run_pipeline(
    app: Any,
    background: BackgroundTasks,
    session: AsyncSession,
    body: dict[str, Any],
    params: dict[str, Any],
    stream: bool,
    pipeline: Any,
    client_ip: str,
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
            return await _pipeline_stream(app, background, session, trace, ctx, strategy)
        result = await strategy.run(ctx)
    except StrategyExecutionError as exc:
        failed_calls = list(exc.members)
        if exc.critique is not None:
            failed_calls.append(exc.critique)
        if exc.judge is not None:
            failed_calls.append(exc.judge)
        await _record_outcomes(session, trace.id, failed_calls)
        await finish_request(session, trace.id, status="failed")
        await session.commit()
        return openai_error(502, f"策略执行失败: {exc}", err_type="upstream_error"), trace.id

    start = time.perf_counter()
    await _record_outcomes(session, trace.id, result.calls)
    status = "degraded" if result.degraded else "success"
    await finish_request(
        session,
        trace.id,
        status=status,
        response_content=result.final_content,
        response_finish_reason=result.final_finish_reason,
        total_duration_ms=int((time.perf_counter() - start) * 1000),
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


async def _pipeline_stream(
    app: Any,
    background: BackgroundTasks,
    session: AsyncSession,
    trace: Any,
    ctx: Any,
    strategy: Any,
) -> tuple[Response, int]:
    plan = await strategy.prepare_stream(ctx)

    # 成员阶段已定：先落库
    await _record_outcomes(session, trace.id, plan.pre_outcomes)
    await session.commit()

    if plan.iteration is not None:
        # ICE 迭代路径（D15）：注释行保活 → 逐轮执行 → 终局流式转发
        return await _pipeline_stream_iterative(app, background, trace, ctx, plan)

    session_factory = app.state.session_factory
    response_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    chunk = make_chunker(response_id, int(time.time()), ctx.body["model"])
    outcome = StreamOutcome()
    start = time.perf_counter()

    async def event_stream():
        outcome.status = "client_cancelled"
        try:
            yield _sse(chunk({"role": "assistant"}))
            async for delta in plan.adapter.stream(plan.request):
                outcome.parts.append(delta)
                yield _sse(chunk({"content": delta}))
            yield _sse(chunk({}, finish_reason="stop"))
            yield "data: [DONE]\n\n"
            outcome.status = "success"
        except AdapterError as exc:
            outcome.status = "failed"
            outcome.error = str(exc)
        finally:
            outcome.duration_ms = int((time.perf_counter() - start) * 1000)

    async def persist() -> None:
        from app.strategies.base import CallOutcome

        content = "".join(outcome.parts)
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
        )
        async with session_factory() as s:
            await _record_outcomes(s, trace.id, [judge])
            await finish_request(
                s,
                trace.id,
                status=outcome.status,
                response_content=content,
                total_duration_ms=outcome.duration_ms,
                total_prompt_tokens=plan.base_usage.prompt_tokens,
                total_completion_tokens=plan.base_usage.completion_tokens,
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
) -> tuple[Response, int]:
    """ICE 迭代流式路径（D15）：注释行保活 → 逐轮执行（策略迭代状态）→ 终局裁决 SSE 转发。

    取消传播：客户端断开时 CancelledError 传入迭代生成器取消在飞轮次任务；
    已完成轮次行由 background persist 照常落库，trace 落 client_cancelled。
    """
    from app.strategies.base import CallOutcome, StrategyExecutionError

    session_factory = app.state.session_factory
    chunk = make_chunker(f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time()), ctx.body["model"])
    outcome = StreamOutcome()
    start = time.perf_counter()
    iteration = plan.iteration
    iter_outcomes: list = []
    final_plan: Any = None

    async def event_stream():
        nonlocal final_plan
        outcome.status = "client_cancelled"  # 未到终态即结束 → 视为取消
        try:
            # 首条注释行仅在进入第 1 轮迭代时发射（第 0 轮已在开流前完成）
            if iteration.progress_comments:
                yield ice_progress_comment(iteration.completed_rounds, iteration.total_rounds)
            async for step in iteration.run():
                iter_outcomes.extend(step.members)
                if step.critique is not None:
                    iter_outcomes.append(step.critique)
                if step.members and iteration.progress_comments:
                    yield ice_progress_comment(step.round_no + 1, iteration.total_rounds)
            final_plan = iteration.final_plan
            yield _sse(chunk({"role": "assistant"}))
            async for delta in final_plan.adapter.stream(final_plan.request):
                outcome.parts.append(delta)
                yield _sse(chunk({"content": delta}))
            yield _sse(chunk({}, finish_reason="stop"))
            yield "data: [DONE]\n\n"
            outcome.status = "success"
        except (AdapterError, StrategyExecutionError) as exc:
            # 终局流式中途失败 / 迭代期 strict 评论失败 → 终止流，按失败落库
            outcome.status = "failed"
            outcome.error = str(exc)
        finally:
            outcome.duration_ms = int((time.perf_counter() - start) * 1000)

    async def persist() -> None:
        content = "".join(outcome.parts)
        prompt_tokens = plan.base_usage.prompt_tokens
        completion_tokens = plan.base_usage.completion_tokens
        for o in iter_outcomes:
            prompt_tokens += o.usage.prompt_tokens
            completion_tokens += o.usage.completion_tokens
        async with session_factory() as s:
            if iter_outcomes:
                await _record_outcomes(s, trace.id, iter_outcomes)
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
                )
                await _record_outcomes(s, trace.id, [judge])
            await finish_request(
                s,
                trace.id,
                status=outcome.status,
                response_content=content,
                total_duration_ms=outcome.duration_ms,
                total_prompt_tokens=prompt_tokens,
                total_completion_tokens=completion_tokens,
            )
            await s.commit()

    background.add_task(persist)
    return StreamingResponse(event_stream(), media_type="text/event-stream"), trace.id


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
            app, background, session, trace, model_row, provider, payload, llm_request, adapter
        )

    start = time.perf_counter()
    try:
        result = await adapter.complete(llm_request)
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
        await finish_request(session, trace.id, status="failed", total_duration_ms=duration)
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
    duration = int((time.perf_counter() - start) * 1000)
    await finish_request(
        session,
        trace.id,
        status="success",
        response_content=result.content,
        response_finish_reason="stop",
        total_duration_ms=duration,
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
    """流式结果载体：generator 只更新内存，由 background task 持久化（断开安全）。"""

    status: str = "client_cancelled"
    parts: list[str] = field(default_factory=list)
    error: str = ""
    duration_ms: int = 0


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
) -> tuple[Response, int]:
    await session.commit()  # 先落 pending 状态的 request_logs

    session_factory = app.state.session_factory
    chunk = make_chunker(f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time()), payload["model"])

    outcome = StreamOutcome()
    start = time.perf_counter()

    async def event_stream():
        outcome.status = "client_cancelled"  # 未到终态即结束 → 视为取消
        try:
            yield _sse(chunk({"role": "assistant"}))
            async for delta in adapter.stream(llm_request):
                outcome.parts.append(delta)
                yield _sse(chunk({"content": delta}))
            yield _sse(chunk({}, finish_reason="stop"))
            yield "data: [DONE]\n\n"
            outcome.status = "success"
        except AdapterError as exc:
            outcome.status = "failed"
            outcome.error = str(exc)
        finally:
            outcome.duration_ms = int((time.perf_counter() - start) * 1000)

    async def persist() -> None:
        async with session_factory() as s:
            content = "".join(outcome.parts)
            await record_call(
                s,
                request_id=trace.id,
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
            )
            await finish_request(
                s,
                trace.id,
                status=outcome.status,
                response_content=content,
                total_duration_ms=outcome.duration_ms,
            )
            await s.commit()

    background.add_task(persist)
    return StreamingResponse(event_stream(), media_type="text/event-stream"), trace.id
