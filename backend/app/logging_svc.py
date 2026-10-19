"""调用日志：request_logs / model_call_logs 的写入、终态更新、读取辅助与保留期清理（Trace 链路）。

status 生命周期：pending → success | degraded | failed | client_cancelled。
归属（多用户 T48）：写入方必须显式携带 user_id/user_username（来自 Caller，D27）——
新增任何落库路径都要传，漏传即出现"无归属"行。
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.orm import ModelCallLog, RequestLog, utcnow


def row_to_dict(row: Any) -> dict[str, Any]:
    """日志行 → API dict（datetime 转 ISO；request/response 大字段由端点自行裁剪）。"""
    data = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    for key, value in data.items():
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


def parse_start(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def parse_end_exclusive(value: str) -> datetime:
    """end 视为日期时含当天全天（次日 0 点为界）。"""
    dt = datetime.fromisoformat(value)
    if len(value) <= 10:
        dt += timedelta(days=1)
    return dt.astimezone(UTC)


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
    user_id: int = 0,
    user_username: str = "",
    error_message: str = "",
) -> RequestLog:
    log = RequestLog(
        pipeline_name=pipeline_name,
        client_model_field=client_model_field,
        request_messages=messages,
        request_params=params,
        client_ip=client_ip,
        user_id=user_id,
        user_username=user_username,
        error_message=error_message,
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
    response_tool_calls: list[dict[str, Any]] | None = None,
    status: str = "success",
    error_message: str = "",
    duration_ms: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    round_no: int | None = None,
    user_id: int = 0,
    user_username: str = "",
) -> ModelCallLog:
    log = ModelCallLog(
        request_id=request_id,
        role=role,
        model_id=model_id,
        upstream_model_id=upstream_model_id,
        provider_name=provider_name,
        request_payload=request_payload,
        response_content=response_content,
        response_tool_calls=response_tool_calls,
        status=status,
        error_message=error_message,
        duration_ms=duration_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        round=round_no,
        user_id=user_id,
        user_username=user_username,
    )
    session.add(log)
    await session.flush()
    return log


async def finish_request(
    session: AsyncSession,
    request_id: int,
    *,
    status: str,
    response_content: str = "",
    response_finish_reason: str = "",
    total_duration_ms: int = 0,
    first_token_ms: int = 0,
    total_prompt_tokens: int = 0,
    total_completion_tokens: int = 0,
    error_message: str = "",
) -> None:
    row = await session.get(RequestLog, request_id)
    if row is None:
        return
    row.status = status
    row.response_content = response_content
    row.response_finish_reason = response_finish_reason
    row.total_duration_ms = total_duration_ms
    row.first_token_ms = first_token_ms
    row.total_prompt_tokens = total_prompt_tokens
    row.total_completion_tokens = total_completion_tokens
    if error_message:
        row.error_message = error_message
    await session.flush()
