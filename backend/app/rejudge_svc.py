"""Trace 重跑裁判：复用已存成员答案按当前配置重渲染裁判 Prompt，输出作为新版本追加（不回写原始记录）。"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import AdapterError, BaseAdapter, LlmRequest
from app.adapters.factory import build_adapter
from app.logging_svc import record_call
from app.orm import LlmModel, ModelCallLog, Provider, RequestLog
from app.repos import find_pipeline_by_name, list_model_calls
from app.strategies.council import (
    DEFAULT_JUDGE_TEMPLATE,
    build_call_request,
    merge_params,
    render_judge_prompt,
)


class RejudgeRejected(Exception):
    """重跑前置校验不通过（status 为 HTTP 状态码，路由层转 HTTPException）。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class PreparedRejudge:
    """通过校验后的重跑调用计划（适配器已按新裁判模型绑定超时/重试参数）。"""

    trace: RequestLog
    judge_model: LlmModel
    judge_provider: Provider
    adapter: BaseAdapter
    request: LlmRequest
    payload: dict  # 实际发出的完整入参（入日志/与快照 hash 一致）


async def prepare_rejudge(
    session: AsyncSession, *, trace: RequestLog, judge_model_id: int, fernet_key: bytes
) -> PreparedRejudge:
    """校验并构建重跑调用计划：已存成员答案 + Pipeline 当前模板 → 裁判请求与适配器。"""
    if trace.status == "pending":
        raise RejudgeRejected(409, "trace 尚未完成，不能重跑裁判")

    calls = await list_model_calls(session, trace.id)
    answers = [
        (c.upstream_model_id, c.response_content)
        for c in calls
        if c.role == "member" and c.status == "success"
    ]
    if not trace.pipeline_name or not answers:
        raise RejudgeRejected(409, "该 trace 无可聚合的成员答案（透传请求或成员全部失败）")

    judge_model = await session.get(LlmModel, judge_model_id)
    if judge_model is None or not judge_model.enabled:
        raise RejudgeRejected(422, f"裁判模型不可用: id={judge_model_id}")
    judge_provider = await session.get(Provider, judge_model.provider_id)
    if judge_provider is None or not judge_provider.enabled:
        raise RejudgeRejected(422, f"裁判模型所属 provider 不可用: {judge_model.display_name}")

    pipeline = await find_pipeline_by_name(session, trace.pipeline_name)
    template = (pipeline.judge_prompt_template if pipeline else "") or DEFAULT_JUDGE_TEMPLATE
    prompt = render_judge_prompt(template, trace.request_messages, answers)
    merged = merge_params(judge_model.default_params, trace.request_params)
    request, payload = build_call_request(
        judge_model.upstream_model_id,
        [{"role": "user", "content": prompt}],
        merged,
        stream=True,
    )
    adapter = build_adapter(
        judge_provider,
        fernet_key=fernet_key,
        timeout_seconds=float(merged.get("timeout_seconds", 120)),
        max_retries=int(merged.get("max_retries", 1)),
    )
    return PreparedRejudge(
        trace=trace,
        judge_model=judge_model,
        judge_provider=judge_provider,
        adapter=adapter,
        request=request,
        payload=payload,
    )


async def rejudge_trace(
    session: AsyncSession, *, trace: RequestLog, judge_model_id: int, fernet_key: bytes
) -> ModelCallLog:
    """同步重跑：complete() 聚合后一次性落库（脚本/非流式调用方使用；UI 走流式端点）。

    裁判 Prompt 按 Pipeline 当前模板重新渲染（Pipeline 已删则用默认模板）；
    只重调裁判，不重调成员。无论成败都追加一行 role="judge_rerun" 的调用记录，
    原始 RequestLog 的最终响应/状态/token 概不回写。
    """
    prepared = await prepare_rejudge(
        session, trace=trace, judge_model_id=judge_model_id, fernet_key=fernet_key
    )

    start = time.perf_counter()
    status, error_message, content = "success", "", ""
    prompt_tokens = completion_tokens = 0
    try:
        result = await prepared.adapter.complete(prepared.request)
    except AdapterError as exc:
        status, error_message = "failed", str(exc)
        duration_ms = int((time.perf_counter() - start) * 1000)
    else:
        content = result.content
        duration_ms = result.duration_ms
        prompt_tokens = result.usage.prompt_tokens
        completion_tokens = result.usage.completion_tokens

    return await record_call(
        session,
        request_id=trace.id,
        role="judge_rerun",
        model_id=prepared.judge_model.id,
        upstream_model_id=prepared.judge_model.upstream_model_id,
        provider_name=prepared.judge_provider.name,
        request_payload=prepared.payload,
        response_content=content,
        status=status,
        error_message=error_message,
        duration_ms=duration_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
