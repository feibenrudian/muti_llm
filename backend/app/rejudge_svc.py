"""Trace 重跑裁判（两段式）：复用已存成员答案重跑"评论 + 最终答案"两次调用，输出作为新版本追加（不回写原始记录）。"""

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
    DEFAULT_CRITIQUE_TEMPLATE,
    DEFAULT_JUDGE_TEMPLATE,
    build_call_request,
    build_final_messages,
    merge_params,
    render_final_instruction,
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
    """通过校验后的重跑调用计划（两段：评论 → 最终答案，适配器已绑定新裁判模型）。"""

    trace: RequestLog
    judge_model: LlmModel
    judge_provider: Provider
    adapter: BaseAdapter
    merged: dict
    answers: list[tuple[str, str]]
    final_template: str
    critique_request: LlmRequest
    critique_payload: dict

    def final_request(self, critique: str) -> tuple[LlmRequest, dict]:
        """第二段请求：与第一次同会话——第一次输入 → 评论(assistant) → 最终指令。"""
        instruction = render_final_instruction(
            self.final_template, self.trace.request_messages, self.answers, critique
        )
        messages = build_final_messages(self.critique_payload["messages"], critique, instruction)
        return build_call_request(
            self.judge_model.upstream_model_id,
            messages,
            self.merged,
            stream=True,
        )


async def prepare_rejudge(
    session: AsyncSession, *, trace: RequestLog, judge_model_id: int, fernet_key: bytes
) -> PreparedRejudge:
    """校验并构建两段式重跑计划：已存成员答案 + 当前模板 → 评论请求/最终模板与适配器。"""
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
    final_template = (pipeline.judge_prompt_template if pipeline else "") or DEFAULT_JUDGE_TEMPLATE
    merged = merge_params(judge_model.default_params, trace.request_params)

    critique_prompt = render_judge_prompt(
        DEFAULT_CRITIQUE_TEMPLATE, trace.request_messages, answers
    )
    critique_request, critique_payload = build_call_request(
        judge_model.upstream_model_id,
        [{"role": "user", "content": critique_prompt}],
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
        merged=merged,
        answers=answers,
        final_template=final_template,
        critique_request=critique_request,
        critique_payload=critique_payload,
    )


async def rejudge_trace(
    session: AsyncSession, *, trace: RequestLog, judge_model_id: int, fernet_key: bytes
) -> ModelCallLog:
    """同步两段式重跑（脚本/非流式调用方使用；UI 走流式端点）。

    只重调裁判（评论 + 最终两次），不重调成员。评论失败只落一行失败的 judge_critique；
    成功则落评论 + judge_rerun 两行并返回最终行。原始 RequestLog 概不回写。
    """
    prepared = await prepare_rejudge(
        session, trace=trace, judge_model_id=judge_model_id, fernet_key=fernet_key
    )
    common = dict(
        request_id=trace.id,
        model_id=prepared.judge_model.id,
        upstream_model_id=prepared.judge_model.upstream_model_id,
        provider_name=prepared.judge_provider.name,
    )

    # 第一段：评论
    start = time.perf_counter()
    try:
        result = await prepared.adapter.complete(prepared.critique_request)
    except AdapterError as exc:
        return await record_call(
            session,
            **common,
            role="judge_critique",
            request_payload=prepared.critique_payload,
            status="failed",
            error_message=str(exc),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
    await record_call(
        session,
        **common,
        role="judge_critique",
        request_payload=prepared.critique_payload,
        response_content=result.content,
        duration_ms=result.duration_ms,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
    )

    # 第二段：最终答案
    request, payload = prepared.final_request(result.content)
    start = time.perf_counter()
    try:
        final = await prepared.adapter.complete(request)
    except AdapterError as exc:
        return await record_call(
            session,
            **common,
            role="judge_rerun",
            request_payload=payload,
            status="failed",
            error_message=str(exc),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
    return await record_call(
        session,
        **common,
        role="judge_rerun",
        request_payload=payload,
        response_content=final.content,
        duration_ms=final.duration_ms,
        prompt_tokens=final.usage.prompt_tokens,
        completion_tokens=final.usage.completion_tokens,
    )
