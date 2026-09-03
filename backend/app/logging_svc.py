"""调用日志：request_logs / model_call_logs 的写入、终态更新与保留期清理（Trace 链路）。

status 生命周期：pending → success | degraded | failed | client_cancelled。
"""

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.orm import ModelCallLog, RequestLog, utcnow


async def cleanup_old_logs(session: AsyncSession, retention_days: int) -> int:
    """删除超过保留期的请求及其调用明细（先删子记录），返回删除的请求数。"""
    cutoff = utcnow() - timedelta(days=retention_days)
    old_ids = select(RequestLog.id).where(RequestLog.created_at < cutoff)
    await session.execute(delete(ModelCallLog).where(ModelCallLog.request_id.in_(old_ids)))
    result = await session.execute(delete(RequestLog).where(RequestLog.id.in_(old_ids)))
    return int(result.rowcount or 0)


async def start_request(
    session: AsyncSession,
    *,
    client_model_field: str,
    pipeline_name: str,
    messages: list[dict[str, Any]],
    params: dict[str, Any],
    client_ip: str = "",
) -> RequestLog:
    log = RequestLog(
        pipeline_name=pipeline_name,
        client_model_field=client_model_field,
        request_messages=messages,
        request_params=params,
        client_ip=client_ip,
        status="pending",
    )
    session.add(log)
    await session.flush()
    await session.refresh(log)
    return log


async def record_call(
    session: AsyncSession,
    *,
    request_id: int,
    role: str,
    model_id: int,
    upstream_model_id: str,
    provider_name: str,
    request_payload: dict[str, Any],
    response_content: str = "",
    status: str = "success",
    error_message: str = "",
    duration_ms: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    session.add(
        ModelCallLog(
            request_id=request_id,
            role=role,
            model_id=model_id,
            upstream_model_id=upstream_model_id,
            provider_name=provider_name,
            request_payload=request_payload,
            response_content=response_content,
            status=status,
            error_message=error_message,
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    )
    await session.flush()


async def finish_request(
    session: AsyncSession,
    request_id: int,
    *,
    status: str,
    response_content: str = "",
    response_finish_reason: str = "",
    total_duration_ms: int = 0,
    total_prompt_tokens: int = 0,
    total_completion_tokens: int = 0,
) -> None:
    row = await session.get(RequestLog, request_id)
    if row is None:
        return
    row.status = status
    row.response_content = response_content
    row.response_finish_reason = response_finish_reason
    row.total_duration_ms = total_duration_ms
    row.total_prompt_tokens = total_prompt_tokens
    row.total_completion_tokens = total_completion_tokens
    await session.flush()
