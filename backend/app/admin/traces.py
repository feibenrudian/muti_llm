"""Trace 查询 API：列表（筛选/分页/关键字）+ 详情（完整时间线）+ 手动清空 + 换裁判重跑。"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.adapters.base import AdapterError, LlmUsage
from app.deps import get_session
from app.logging_svc import record_call
from app.orm import ModelCallLog, RequestLog
from app.rejudge_svc import PreparedRejudge, RejudgeRejected, prepare_rejudge, rejudge_trace
from app.repos import list_model_calls

router = APIRouter(prefix="/traces", tags=["admin-traces"])


def row_to_dict(row: Any) -> dict[str, Any]:
    data = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    for key, value in data.items():
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


def _parse_start(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _parse_end_exclusive(value: str) -> datetime:
    """end 视为日期时含当天全天（次日 0 点为界）。"""
    dt = datetime.fromisoformat(value)
    if len(value) <= 10:
        dt += timedelta(days=1)
    return dt.astimezone(UTC)


@router.get("")
async def list_traces(
    session: AsyncSession = Depends(get_session),
    start: str | None = None,
    end: str | None = None,
    pipeline: str | None = None,
    model: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    conditions = []
    if pipeline:
        conditions.append(
            or_(
                RequestLog.pipeline_name == pipeline,
                RequestLog.client_model_field == pipeline,
            )
        )
    if status:
        conditions.append(RequestLog.status == status)
    if start:
        conditions.append(RequestLog.created_at >= _parse_start(start))
    if end:
        conditions.append(RequestLog.created_at < _parse_end_exclusive(end))
    if keyword:
        conditions.append(RequestLog.request_messages.like(f"%{keyword}%"))
    if model:
        conditions.append(
            RequestLog.id.in_(
                select(ModelCallLog.request_id).where(ModelCallLog.upstream_model_id == model)
            )
        )

    base = select(RequestLog)
    count_query = select(func.count()).select_from(RequestLog)
    for condition in conditions:
        base = base.where(condition)
        count_query = count_query.where(condition)

    total = (await session.execute(count_query)).scalar_one()
    rows = (
        (
            await session.execute(
                base.order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    items = []
    for row in rows:
        item = row_to_dict(row)
        item["content_preview"] = (row.response_content or "")[:120]
        item.pop("request_messages", None)
        item.pop("response_content", None)
        items.append(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/{trace_id}")
async def get_trace(trace_id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    row = await session.get(RequestLog, trace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="trace not found")
    calls = await list_model_calls(session, trace_id)
    detail = row_to_dict(row)
    if isinstance(detail.get("request_messages"), str):
        detail["request_messages"] = json.loads(detail["request_messages"])
    return {
        "request": detail,
        "calls": [row_to_dict(call) for call in calls],
    }


@router.post("/clear")
async def clear_traces(session: AsyncSession = Depends(get_session)) -> dict[str, bool]:
    await session.execute(delete(ModelCallLog))
    await session.execute(delete(RequestLog))
    await session.commit()
    return {"cleared": True}


class RejudgeBody(BaseModel):
    judge_model_id: int


@router.post("/{trace_id}/rejudge")
async def rejudge(
    trace_id: int,
    body: RejudgeBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """换裁判模型重跑聚合：复用已存成员答案，输出作为新版本追加（原始记录不动）。"""
    trace = await session.get(RequestLog, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    try:
        call = await rejudge_trace(
            session,
            trace=trace,
            judge_model_id=body.judge_model_id,
            fernet_key=request.app.state.fernet_key,
        )
    except RejudgeRejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None
    await session.commit()
    return {"call": row_to_dict(call)}


def _rejudge_sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@dataclass
class _RerunOutcome:
    """两段式流式重跑的内存载体：SSE generator 只更新内存，落库由后台任务完成（断开安全）。"""

    critique_parts: list[str] = field(default_factory=list)
    final_parts: list[str] = field(default_factory=list)
    final_payload: dict | None = None  # 评论完成后才能构建第二段入参
    critique_usage: LlmUsage | None = None
    final_usage: LlmUsage | None = None
    critique_status: str = "client_cancelled"  # 评论段终态
    status: str = "client_cancelled"  # 最终段终态（未到终态即断开 → 取消）
    critique_duration_ms: int = 0
    error: str = ""
    duration_ms: int = 0


@router.post("/{trace_id}/rejudge/stream")
async def rejudge_stream(
    trace_id: int,
    body: RejudgeBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """换裁判两段式重跑（流式）：SSE 依次发 meta/delta(phase=critique) → meta/delta(phase=final) → done。"""
    trace = await session.get(RequestLog, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    try:
        prepared = await prepare_rejudge(
            session,
            trace=trace,
            judge_model_id=body.judge_model_id,
            fernet_key=request.app.state.fernet_key,
        )
    except RejudgeRejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None

    session_factory = request.app.state.session_factory
    outcome = _RerunOutcome()
    start = time.perf_counter()

    async def gen() -> AsyncIterator[str]:
        phase = "critique"
        try:
            yield _rejudge_sse(
                {"type": "meta", "phase": "critique", "payload": prepared.critique_payload}
            )
            async for event in prepared.adapter.stream_events_timed(prepared.critique_request):
                if event.usage is not None:
                    outcome.critique_usage = event.usage
                if event.text:
                    outcome.critique_parts.append(event.text)
                    yield _rejudge_sse({"type": "delta", "phase": "critique", "text": event.text})
            outcome.critique_status = "success"
            outcome.critique_duration_ms = int((time.perf_counter() - start) * 1000)

            final_request, final_payload = prepared.final_request("".join(outcome.critique_parts))
            outcome.final_payload = final_payload
            yield _rejudge_sse({"type": "meta", "phase": "final", "payload": final_payload})
            phase = "final"
            async for event in prepared.adapter.stream_events_timed(final_request):
                if event.usage is not None:
                    outcome.final_usage = event.usage
                if event.text:
                    outcome.final_parts.append(event.text)
                    yield _rejudge_sse({"type": "delta", "phase": "final", "text": event.text})
            outcome.status = "success"
        except AdapterError as exc:
            outcome.status, outcome.error = "failed", str(exc)
            if phase == "critique":
                outcome.critique_status = "failed"
                outcome.critique_duration_ms = int((time.perf_counter() - start) * 1000)
        finally:
            outcome.duration_ms = int((time.perf_counter() - start) * 1000)
        # 客户端断开时生成器被取消，不会走到这里；正常结束/失败都发终态事件
        critique_usage = outcome.critique_usage or LlmUsage()
        final_usage = outcome.final_usage or LlmUsage()
        yield _rejudge_sse(
            {
                "type": "done",
                "status": outcome.status,
                "critique": "".join(outcome.critique_parts)
                if outcome.critique_status == "success"
                else "",
                "content": "".join(outcome.final_parts) if outcome.status != "failed" else "",
                "error": outcome.error,
                "duration_ms": outcome.duration_ms,
                "prompt_tokens": critique_usage.prompt_tokens + final_usage.prompt_tokens,
                "completion_tokens": critique_usage.completion_tokens + final_usage.completion_tokens,
                "critique_duration_ms": outcome.critique_duration_ms,
                "critique_prompt_tokens": critique_usage.prompt_tokens,
                "critique_completion_tokens": critique_usage.completion_tokens,
            }
        )

    async def persist() -> None:
        critique_usage = outcome.critique_usage or LlmUsage()
        final_usage = outcome.final_usage or LlmUsage()
        common = dict(
            request_id=trace.id,
            model_id=prepared.judge_model.id,
            upstream_model_id=prepared.judge_model.upstream_model_id,
            provider_name=prepared.judge_provider.name,
        )
        async with session_factory() as s:
            await record_call(
                s,
                **common,
                role="judge_critique",
                request_payload=prepared.critique_payload,
                response_content="".join(outcome.critique_parts)
                if outcome.critique_status != "failed"
                else "",
                status=outcome.critique_status,
                error_message=outcome.error if outcome.critique_status == "failed" else "",
                duration_ms=outcome.critique_duration_ms or outcome.duration_ms,
                prompt_tokens=critique_usage.prompt_tokens,
                completion_tokens=critique_usage.completion_tokens,
            )
            if outcome.final_payload is not None:
                await record_call(
                    s,
                    **common,
                    role="judge_rerun",
                    request_payload=outcome.final_payload,
                    response_content="".join(outcome.final_parts)
                    if outcome.status != "failed"
                    else "",
                    status=outcome.status,
                    error_message=outcome.error if outcome.status == "failed" else "",
                    duration_ms=outcome.duration_ms,
                    prompt_tokens=final_usage.prompt_tokens,
                    completion_tokens=final_usage.completion_tokens,
                )
            await s.commit()

    return StreamingResponse(
        gen(), media_type="text/event-stream", background=BackgroundTask(persist)
    )
