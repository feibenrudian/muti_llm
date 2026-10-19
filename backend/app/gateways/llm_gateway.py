"""对外网关（/v1/*）：OpenAI 兼容端点。model 解析：Pipeline 名优先 → 真实模型透传（决策 D3）。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import (
    AdapterError,
    LlmRequest,
    LlmUsage,
    NormalizedMessage,
    merge_tool_call_deltas,
)
from app.adapters.factory import build_adapter
from app.auth_svc import Caller
from app.authz import AccessScope, filter_models, filter_pipelines, load_scope
from app.bootstrap import KEY_EXPOSE_ROUTED_MODELS, KEY_EXPOSE_VIRTUAL_MODELS
from app.logging_svc import finish_request, record_call, start_request
from app.orm import LlmModel, Provider
from app.repos import (
    find_enabled_model_by_upstream_id,
    find_enabled_pipeline_by_name,
    get_setting,
)
from app.settings import settings

router = APIRouter(tags=["gateway"])

logger = logging.getLogger("muti_llm")

VALID_ROLES = ("system", "user", "assistant")


# 上游 AdapterError.kind → 对客户端的 HTTP 状态（T53b：限流不再伪装成 502）
_ADAPTER_ERROR_STATUS: dict[str, int] = {
    "timeout": 504,
    "connection": 502,
    "rate_limit": 429,
    "auth": 502,  # 上游鉴权失败：客户端凭据没错，不宜回 401 误导
    "bad_request": 400,
    "upstream_error": 502,
}


def adapter_error_response(exc: AdapterError) -> JSONResponse:
    """上游 AdapterError → OpenAI 风格错误响应（非流式；流式的错误事件与此同构）。"""
    status = _ADAPTER_ERROR_STATUS.get(exc.kind, 502)
    return openai_error(status, f"上游调用失败: {exc}", err_type="upstream_error", code=exc.kind)


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


async def model_exposure(session: AsyncSession) -> tuple[bool, bool]:
    """两类模型的对外接入开关（settings 页可配）：(虚拟模型, 路由模型)，缺省均开。

    关闭即整体下线该类接入：/v1/models 不列出、/v1/chat/completions 不再按该类解析。
    """
    virtual = await get_setting(session, KEY_EXPOSE_VIRTUAL_MODELS)
    routed = await get_setting(session, KEY_EXPOSE_ROUTED_MODELS)
    return virtual != "0", routed != "0"


def _is_off_state(value: Any) -> bool:
    """客户端默认携带的"关闭态"值（None/False/空容器/"auto"/"none"）视为未启用该能力。"""
    if value is None or value is False:
        return True
    if isinstance(value, (list, dict)) and not value:
        return True
    if isinstance(value, str) and value.lower() in ("auto", "none"):
        return True
    return False


def _error_reason(error: JSONResponse) -> str:
    """从 openai_error 响应体提取 message（校验拒绝落库用）。"""
    try:
        return str(json.loads(error.body)["error"]["message"])  # type: ignore[arg-type]
    except Exception:
        return "invalid_request"


async def record_rejected_request(
    app: Any, body: Any, client_ip: str, caller: Any, reason: str
) -> None:
    """400 校验拒绝落一条 failed trace（T53）：保留原始 messages 便于排查；
    落库失败静默（不掩盖 400 响应）。"""
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        messages = []
    try:
        async with app.state.session_factory() as session:
            trace = await start_request(
                session,
                client_model_field=str(body.get("model", "")) if isinstance(body, dict) else "",
                pipeline_name="",
                messages=messages,
                params={},
                client_ip=client_ip,
                user_id=getattr(caller, "user_id", 0),
                user_username=getattr(caller, "username", ""),
                error_message=reason,
            )
            await finish_request(session, trace.id, status="failed")
            await session.commit()
    except Exception:
        logger.warning("rejected request 落库失败: %s", reason, exc_info=True)


def _valid_tool_calls(tool_calls: Any) -> bool:
    """assistant.tool_calls 合法性（T53）：非空数组，每项含非空 id 与
    function.name/arguments 字符串。"""
    if not isinstance(tool_calls, list) or not tool_calls:
        return False
    for call in tool_calls:
        if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]:
            return False
        fn = call.get("function")
        if (
            not isinstance(fn, dict)
            or not isinstance(fn.get("name"), str)
            or not fn["name"]
            or not isinstance(fn.get("arguments"), str)
        ):
            return False
    return True


def validate_chat_body(
    body: dict[str, Any],
) -> tuple[JSONResponse | None, list[Any], str, dict[str, Any], bool, bool, bool]:
    """OpenAI 兼容请求体校验与提参（三入口共用，错误契约一致）。

    基础校验为 OpenAI 工具协议超集（T53）：role=tool（须带非空 tool_call_id）与
    assistant.tool_calls（content 可空）放行——透传路径原样转发上游；聚合路径是否拒绝
    由调用方按 tool_history 分支处理（council 渲染消息进裁判 prompt，不支持回放）。
    tools 空值/关闭态字段静默忽略（Unsloth Studio 等客户端默认携带空 tools，T43）；
    非空 tools 不在此拒绝——由调用方按路径分流：透传放行、聚合（pipeline）400。
    返回 (error, messages, model, params, stream, has_tools, tool_history)。
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return (
            openai_error(400, "messages 必须为非空数组", param="messages"),
            [],
            "",
            {},
            False,
            False,
            False,
        )

    def _reject(
        message: str,
    ) -> tuple[JSONResponse | None, list[Any], str, dict[str, Any], bool, bool, bool]:
        return (openai_error(400, message, param="messages"), [], "", {}, False, False, False)

    tool_history = False
    for m in messages:
        if not isinstance(m, dict):
            return _reject("每条 message 需含 role(system|user|assistant) 与字符串 content")
        role = m.get("role")
        content = m.get("content")
        if role == "tool":
            # 工具结果回放（T53）：content 字符串 + 非空 tool_call_id
            if not isinstance(content, str) or not m.get("tool_call_id"):
                return _reject("tool 消息需含字符串 content 与非空 tool_call_id")
            tool_history = True
            continue
        if role == "assistant" and content is None:
            # assistant 声明工具调用：content 可空，但 tool_calls 必须合法
            if not _valid_tool_calls(m.get("tool_calls")):
                return _reject("assistant 消息 content=null 时需携带合法 tool_calls 数组")
            tool_history = True
            continue
        if role not in VALID_ROLES or not isinstance(content, str):
            return _reject("每条 message 需含 role(system|user|assistant) 与字符串 content")
        if role == "assistant" and _valid_tool_calls(m.get("tool_calls")):
            tool_history = True

    tools = body.get("tools")
    has_tools = isinstance(tools, list) and len(tools) > 0
    for unsupported in ("tool_choice", "functions", "function_call", "logprobs", "logit_bias"):
        value = body.get(unsupported)
        if has_tools and unsupported == "tool_choice":
            continue  # 随 tools 一并透传上游（含 "required"/对象形态）
        if not _is_off_state(value):
            return (
                openai_error(400, f"不支持的能力: {unsupported}", param=unsupported),
                [],
                "",
                {},
                False,
                False,
                False,
            )

    model_field = body.get("model")
    if not isinstance(model_field, str) or not model_field:
        return openai_error(400, "model 必填", param="model"), [], "", {}, False, False, False

    params: dict[str, Any] = {}
    for key in ("temperature", "max_tokens", "top_p"):
        value = body.get(key)
        if value is not None:
            params[key] = value
    return None, messages, model_field, params, bool(body.get("stream")), has_tools, tool_history


@router.get("/v1/models")
async def list_models(request: Request) -> Response:
    from app.orm import LlmModel as M
    from app.orm import Pipeline as P
    from app.repos import Repository

    factory = request.app.state.session_factory
    async with factory() as session:
        expose_virtual, expose_routed = await model_exposure(session)
        pipelines = await Repository(session, P).list()
        models = await Repository(session, M).list()
        scope: AccessScope = await load_scope(session, request.state.caller)
    data = []
    if expose_virtual:
        data += [
            {"id": p.name, "object": "model", "created": 0, "owned_by": "muti_llm"}
            for p in filter_pipelines(scope, pipelines)
            if p.enabled
        ]
    if expose_routed:
        data += [
            {"id": m.upstream_model_id, "object": "model", "created": 0, "owned_by": "muti_llm"}
            for m in filter_models(scope, models)
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
    caller: Caller = request.state.caller
    response, _ = await execute_chat(request.app, background, body, client_ip, t0, caller)
    return response


async def execute_chat(
    app: Any,
    background: BackgroundTasks,
    body: dict[str, Any],
    client_ip: str,
    t0: float,
    caller: Caller,
) -> tuple[Response, int | None]:
    """网关核心执行（校验 → Pipeline/透传路由 → 响应）。返回 (response, trace_id)。

    /v1/chat/completions（认证后）与 Playground（管理 API）共用同一执行路径；
    caller 为认证身份，贯穿归属落库（D25/D27）。
    """

    class _AppRequest:
        """给依赖 request 的内部函数提供 app.state 的轻量代理。"""

        def __init__(self, app: Any, client_ip: str) -> None:
            self.app = app
            self.client = type("Client", (), {"host": client_ip})()

    request = _AppRequest(app, client_ip)  # noqa: F841 - 供下方函数签名兼容

    error, messages, model_field, params, stream, has_tools, tool_history = validate_chat_body(body)
    if error is not None:
        await record_rejected_request(app, body, client_ip, caller, _error_reason(error))
        return error, None
    # 过程流式（T38/T39）：仅 pipeline 流式生效；None=请求体未显式给 → 用 pipeline 默认
    stream_process_raw = body.get("stream_process")

    async with app.state.session_factory() as session:
        expose_virtual, expose_routed = await model_exposure(session)
        # 虚拟模型接入关闭时不按 Pipeline 解析（同名真实模型若存在则照常透传）
        pipeline = (
            await find_enabled_pipeline_by_name(session, model_field) if expose_virtual else None
        )
        scope = await load_scope(session, caller)
        if pipeline is not None:
            if not scope.allows_pipeline(pipeline.id):
                return (
                    openai_error(
                        403,
                        f"model '{model_field}' 未授权：请联系管理员开通",
                        code="model_not_authorized",
                    ),
                    None,
                )
            if has_tools and (not pipeline.tool_aggregation or pipeline.strategy != "council"):
                # 工具聚合仅 council 实现：开关关闭或其余策略（ICE）→ 明确拒绝（不静默忽略 tools）
                return (
                    openai_error(400, "聚合模式（Pipeline）暂不支持工具调用: tools", param="tools"),
                    None,
                )
            if tool_history:
                # 工具协议历史回放（T53）仅透传路径放行；聚合（含工具聚合）把消息渲染进
                # 裁判 prompt，从未接受过此类消息（旧校验直接拒绝），语义不变
                return (
                    openai_error(
                        400,
                        "聚合模式（Pipeline）暂不支持工具协议消息回放: messages",
                        param="messages",
                    ),
                    None,
                )
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
                caller,
                stream_process=stream_process,
            )

        model_row = await find_enabled_model_by_upstream_id(session, model_field)
        if model_row is None or not expose_routed:
            return (
                openai_error(404, f"model '{model_field}' not found", code="model_not_found"),
                None,
            )
        if not scope.allows_model_row(model_row):
            return (
                openai_error(
                    403,
                    f"model '{model_field}' 未授权：请联系管理员开通",
                    code="model_not_authorized",
                ),
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
            app,
            background,
            session,
            body,
            model_row,
            provider,
            params,
            stream,
            client_ip,
            t0,
            caller,
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


async def _record_outcomes(
    session: AsyncSession, request_id: int, outcomes: list, caller: Caller | None = None
) -> None:
    from app.logging_svc import record_call

    for outcome in outcomes:
        await record_call(
            session,
            request_id=request_id,
            user_id=caller.user_id if caller else 0,
            user_username=caller.username if caller else "",
            **outcome.to_log_kwargs(),
        )


async def _detached_pipeline_persist(
    session_factory: Any,
    trace_id: int,
    work: asyncio.Future[Any],
    t0: float,
    caller: Caller | None = None,
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
            await _record_outcomes(s, trace_id, failed_calls, caller)
            await finish_request(s, trace_id, status="failed", total_duration_ms=_elapsed_ms(t0))
            await s.commit()
        return
    except Exception:
        # 兜底：非预期异常也必须落终态，避免 trace 永远 pending
        async with session_factory() as s:
            await finish_request(s, trace_id, status="failed", total_duration_ms=_elapsed_ms(t0))
            await s.commit()
        return
    async with session_factory() as s:
        await _record_outcomes(s, trace_id, result.calls, caller)
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
    caller: Caller | None = None,
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
                user_id=caller.user_id if caller else 0,
                user_username=caller.username if caller else "",
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
            await finish_request(s, trace_id, status="failed", total_duration_ms=_elapsed_ms(t0))
            await s.commit()
        return
    async with session_factory() as s:
        await record_call(
            s,
            request_id=trace_id,
            user_id=caller.user_id,
            user_username=caller.username,
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
    caller: Caller,
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
        user_id=caller.user_id,
        user_username=caller.username,
    )

    try:
        ctx = await _build_strategy_context(session, body, params, pipeline, app.state.fernet_key)
        strategy = get_strategy(pipeline.strategy)()
        if stream:
            if stream_process:
                return await _pipeline_stream_process(
                    app, background, session, trace, ctx, strategy, t0, caller
                )
            return await _pipeline_stream(
                app, background, session, trace, ctx, strategy, t0, caller
            )
        await session.commit()  # 落 pending 并固定 ctx 内 ORM 属性：断连后续跑/取消才可写终态
        work = asyncio.ensure_future(strategy.run(ctx))
        try:
            result = await work
        except asyncio.CancelledError:
            if settings.detach_on_disconnect:
                # 断连续跑：响应已无法再交付，上游跑完由后台任务落真实终态
                asyncio.create_task(
                    _detached_pipeline_persist(
                        app.state.session_factory, trace.id, work, t0, caller
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
    except StrategyExecutionError as exc:
        failed_calls = list(exc.members)
        if exc.critique is not None:
            failed_calls.append(exc.critique)
        if exc.judge is not None:
            failed_calls.append(exc.judge)
        await _record_outcomes(session, trace.id, failed_calls, caller)
        await finish_request(session, trace.id, status="failed", total_duration_ms=_elapsed_ms(t0))
        await session.commit()
        return openai_error(502, f"策略执行失败: {exc}", err_type="upstream_error"), trace.id

    await _record_outcomes(session, trace.id, result.calls, caller)
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
        tool_calls=result.tool_calls,
        finish_reason=result.final_finish_reason,
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


async def _drain_with_heartbeat(queue: asyncio.Queue[Any], interval: float) -> AsyncIterator[str]:
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
    caller: Caller,
) -> tuple[Response, int]:
    plan = await strategy.prepare_stream(ctx)

    # 成员阶段已定：先落库
    await _record_outcomes(session, trace.id, plan.pre_outcomes, caller)
    await session.commit()

    if plan.iteration is not None:
        # ICE 迭代路径（D15）：注释行保活 → 逐轮执行 → 终局流式转发
        return await _pipeline_stream_iterative(app, background, trace, ctx, plan, t0, caller)

    session_factory = app.state.session_factory
    response_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    chunk = make_chunker(response_id, int(time.time()), ctx.body["model"])
    outcome = StreamOutcome()
    start = time.perf_counter()  # 流段本地计时：仅供裁判调用行 duration_ms
    trace_id: int = trace.id
    queue: asyncio.Queue[Any] = asyncio.Queue()

    async def pump() -> None:
        outcome.status = "client_cancelled"  # 未到终态即结束 → 视为取消
        try:
            queue.put_nowait(_sse(chunk({"role": "assistant"})))
            async for event in plan.adapter.stream_events_timed(plan.request):
                if event.usage is not None:
                    outcome.usage = event.usage
                if event.finish_reason:
                    outcome.finish_reason = event.finish_reason
                if event.tool_calls:
                    if outcome.first_token_ms == 0:
                        outcome.first_token_ms = _elapsed_ms(t0)
                    outcome.tool_call_fragments.extend(event.tool_calls)
                    queue.put_nowait(_sse(chunk({"tool_calls": event.tool_calls})))
                if event.text:
                    if outcome.first_token_ms == 0:
                        outcome.first_token_ms = _elapsed_ms(t0)
                    outcome.parts.append(event.text)
                    queue.put_nowait(_sse(chunk({"content": event.text})))
            queue.put_nowait(_sse(chunk({}, finish_reason=outcome.finish_reason)))
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
        merged = merge_tool_call_deltas(outcome.tool_call_fragments)
        tool_calls = [merged[i] for i in sorted(merged)] or None
        judge_usage = outcome.usage or LlmUsage()
        judge = CallOutcome(
            role="judge",
            model_id=plan.model_id,
            upstream_model_id=plan.upstream_model_id,
            provider_name=plan.provider_name,
            request_payload=plan.payload,
            response_content=content if outcome.status != "failed" else "",
            response_tool_calls=tool_calls,
            status=outcome.status,
            error_message=outcome.error,
            duration_ms=outcome.duration_ms,
            usage=judge_usage,
        )
        async with session_factory() as s:
            await _record_outcomes(s, trace_id, [judge], caller)
            await finish_request(
                s,
                trace_id,
                status=outcome.status,
                response_content=content,
                response_finish_reason=outcome.finish_reason,
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
    caller: Caller,
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
                await _record_outcomes(s, trace_id, iter_outcomes, caller)
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
                await _record_outcomes(s, trace_id, [judge], caller)
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
    caller: Caller,
) -> tuple[Response, int]:
    """过程流式路径（T38 stream_process=true）：成员/评论完成即发 reasoning chunk，终局照旧。

    与迭代路径同骨架（pump/ferry/persist + detach/心跳/t0 计时）；区别：响应不等前置阶段
    全部完成——首个成员成功即返回（TTFT 提前到首成员完成）；前置明细不做开流前预写，
    由 persist 从 ProcessEvent 累计落库。首个成功成员前 pump 失败 → 暂存异常重抛，
    走 _run_pipeline 既有 502 JSON 路径（与今日全非流式/流式失败契约一致）。
    终局两形态（T44）：FinalPlanReady 逐字转发裁判流；FinalResultReady（工具聚合）为
    策略内已校验/降级的完整结果，按 tool_calls/文字一次性下发。
    """
    from app.strategies.base import FinalPlanReady, FinalResultReady, StrategyExecutionError

    await session.commit()  # 先落 pending 状态的 request_logs（pre_outcomes 由 persist 补写）

    session_factory = app.state.session_factory
    chunk = make_chunker(f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time()), ctx.body["model"])
    outcome = StreamOutcome()
    start = time.perf_counter()  # 本地计时：仅供裁判调用行 duration_ms
    trace_id: int = trace.id
    queue: asyncio.Queue[Any] = asyncio.Queue()
    pre_outcomes: list[Any] = []
    final_plan: Any = None
    final_result: Any = None
    first_success = asyncio.Event()
    stashed: list[Exception] = []
    role_sent = False

    async def pump() -> None:
        nonlocal final_plan, final_result, role_sent
        outcome.status = "client_cancelled"  # 未到终态即结束 → 视为取消
        try:
            async for ev in strategy.run_stream_process(ctx):
                if isinstance(ev, FinalResultReady):
                    # 工具聚合终局（T44）：策略内已校验/降级，按结果形态一次性下发
                    final_result = ev
                    queue.put_nowait(_sse(chunk({"role": "assistant"})))
                    if outcome.first_token_ms == 0:
                        outcome.first_token_ms = _elapsed_ms(t0)
                    if ev.tool_calls:
                        for i, call in enumerate(ev.tool_calls):
                            queue.put_nowait(_sse(chunk({"tool_calls": [{**call, "index": i}]})))
                    elif ev.content:
                        queue.put_nowait(_sse(chunk({"content": ev.content})))
                    queue.put_nowait(_sse(chunk({}, finish_reason=ev.finish_reason)))
                    queue.put_nowait("data: [DONE]\n\n")
                    outcome.status = "success"
                    continue
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
        final_judge: CallOutcome | None = None
        response_content = content
        response_finish = ""
        if final_plan is not None:
            # 逐字转发形态：judge 明细从流式 outcome 还原
            final_usage = outcome.usage or LlmUsage()
            prompt_tokens += final_usage.prompt_tokens
            completion_tokens += final_usage.completion_tokens
            final_judge = CallOutcome(
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
        elif final_result is not None:
            # 工具聚合形态（T44）：终局在策略内完成，明细随 FinalResultReady 携带
            final_judge = final_result.judge
            prompt_tokens += final_result.judge.usage.prompt_tokens
            completion_tokens += final_result.judge.usage.completion_tokens
            response_content = final_result.content
            response_finish = final_result.finish_reason
        status = outcome.status
        if final_result is not None and final_result.degraded and status == "success":
            status = "degraded"
        async with session_factory() as s:
            if pre_outcomes:
                await _record_outcomes(s, trace_id, pre_outcomes, caller)
            if final_judge is not None:  # 终局已开始才落 judge 行（前置期取消则无此行）
                await _record_outcomes(s, trace_id, [final_judge], caller)
            await finish_request(
                s,
                trace_id,
                status=status,
                response_content=response_content,
                response_finish_reason=response_finish,
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
    """构造归一化请求与实际发出的 payload（入日志/与快照 hash 一致）。

    消息按字段白名单透传（T53）：role/content 恒在；tool_calls/tool_call_id/name
    仅在携带时出现——普通请求 payload 与既往逐字节一致（快照 hash 不受影响）。
    """
    openai_messages: list[dict[str, Any]] = []
    normalized: list[NormalizedMessage] = []
    for m in body["messages"]:
        entry: dict[str, Any] = {"role": m["role"], "content": m.get("content")}
        for key in ("tool_calls", "tool_call_id", "name", "reasoning_content"):
            if m.get(key) is not None:
                entry[key] = m[key]
        openai_messages.append(entry)
        normalized.append(
            NormalizedMessage(
                role=m["role"],
                content=m.get("content"),
                tool_calls=m.get("tool_calls"),
                tool_call_id=m.get("tool_call_id") or "",
                name=m.get("name") or "",
                reasoning_content=m.get("reasoning_content") or "",
            )
        )
    payload: dict[str, Any] = {
        "model": model_row.upstream_model_id,
        "messages": openai_messages,
        "stream": stream,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    llm_request = LlmRequest(model=model_row.upstream_model_id, messages=normalized)
    for key in ("temperature", "max_tokens", "top_p"):
        if merged.get(key) is not None:
            payload[key] = merged[key]
            setattr(llm_request, key, merged[key])
    # 工具透传（T43）：仅透传路径到达此处（pipeline 分支已 400）； anthropic 供应商由适配器拒绝
    if isinstance(body.get("tools"), list) and body["tools"]:
        payload["tools"] = body["tools"]
        llm_request.tools = body["tools"]
        if body.get("tool_choice") is not None:
            payload["tool_choice"] = body["tool_choice"]
            llm_request.tool_choice = body["tool_choice"]
    return llm_request, payload


def _completion_response(
    model: str,
    content: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        # 纯工具调用时上游 content 为 null（OpenAI 兼容形态），仅在有 tool_calls 时允许
        "content": content if (content or not tool_calls) else None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
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
    caller: Caller,
) -> tuple[Response, int]:
    trace = await start_request(
        session,
        client_model_field=body["model"],
        pipeline_name="",
        messages=body["messages"],
        params=params,
        client_ip=client_ip,
        user_id=caller.user_id,
        user_username=caller.username,
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
            app,
            background,
            session,
            trace,
            model_row,
            provider,
            payload,
            llm_request,
            adapter,
            t0,
            caller,
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
                    caller,
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
            user_id=caller.user_id,
            user_username=caller.username,
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
            session,
            trace.id,
            status="failed",
            total_duration_ms=_elapsed_ms(t0),
            error_message=str(exc),
        )
        await session.commit()
        return adapter_error_response(exc), trace.id

    await record_call(
        session,
        request_id=trace.id,
        user_id=caller.user_id,
        user_username=caller.username,
        role="passthrough",
        model_id=model_row.id,
        upstream_model_id=model_row.upstream_model_id,
        provider_name=provider.name,
        request_payload=payload,
        response_content=result.content,
        response_tool_calls=result.tool_calls,
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
        response_finish_reason=result.finish_reason,
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
            tool_calls=result.tool_calls,
            finish_reason=result.finish_reason,
        )
    ), trace.id


@dataclass
class StreamOutcome:
    """流式结果载体：generator 只更新内存，由 background task 持久化（断开安全）。

    usage 为终局流（passthrough 为唯一调用，pipeline 为裁判调用）结束时的汇总事件，
    上游不支持 usage 时保持 None（T41：此前该事件被丢弃，流式 token 全部记 0）。
    tool_call_fragments 为流内原样分片（T43），persist 时按 index 聚合落库。
    """

    status: str = "client_cancelled"
    parts: list[str] = field(default_factory=list)
    error: str = ""
    duration_ms: int = 0
    first_token_ms: int = 0
    usage: LlmUsage | None = None
    finish_reason: str = "stop"
    tool_call_fragments: list[dict[str, Any]] = field(default_factory=list)


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
    caller: Caller,
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
                if event.finish_reason:
                    outcome.finish_reason = event.finish_reason
                if event.tool_calls:
                    if outcome.first_token_ms == 0:
                        outcome.first_token_ms = _elapsed_ms(t0)
                    outcome.tool_call_fragments.extend(event.tool_calls)
                    queue.put_nowait(_sse(chunk({"tool_calls": event.tool_calls})))
                if event.text:
                    if outcome.first_token_ms == 0:
                        outcome.first_token_ms = _elapsed_ms(t0)
                    outcome.parts.append(event.text)
                    queue.put_nowait(_sse(chunk({"content": event.text})))
            queue.put_nowait(_sse(chunk({}, finish_reason=outcome.finish_reason)))
            queue.put_nowait("data: [DONE]\n\n")
            outcome.status = "success"
        except AdapterError as exc:
            outcome.status = "failed"
            outcome.error = str(exc)
            # 上游失败可见性（T53b）：HTTP 200 已随首块发出，补发 OpenAI 兼容 SSE 错误事件
            # 再正常收尾——客户端据此刻画真实原因（如上游 429 余额不足），而非误判为空响应
            queue.put_nowait(
                _sse(
                    {
                        "error": {
                            "message": f"上游调用失败: {exc}",
                            "type": "upstream_error",
                            "param": "",
                            "code": exc.kind,
                        }
                    }
                )
            )
            queue.put_nowait("data: [DONE]\n\n")
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
            merged = merge_tool_call_deltas(outcome.tool_call_fragments)
            await record_call(
                s,
                request_id=trace_id,
                user_id=caller.user_id if caller else 0,
                user_username=caller.username if caller else "",
                role="passthrough",
                model_id=model_row.id,
                upstream_model_id=model_row.upstream_model_id,
                provider_name=provider.name,
                request_payload=payload,
                response_content=content if outcome.status != "failed" else "",
                response_tool_calls=[merged[i] for i in sorted(merged)] or None,
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
                response_finish_reason=outcome.finish_reason,
                error_message=outcome.error,
                total_duration_ms=_elapsed_ms(t0),
                first_token_ms=outcome.first_token_ms,
                total_prompt_tokens=usage.prompt_tokens,
                total_completion_tokens=usage.completion_tokens,
            )
            await s.commit()

    background.add_task(persist)
    return StreamingResponse(event_stream(), media_type="text/event-stream"), trace_id
