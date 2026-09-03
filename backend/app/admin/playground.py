"""Playground：Web UI 试运行——复用网关 execute_chat 同一执行路径，返回响应与 trace_id。

UI 拿 trace_id 再查 Trace 详情，展示成员答案/裁判 Prompt/最终答案的完整链路。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, Field

from app.gateways.llm_gateway import execute_chat

router = APIRouter(prefix="/playground", tags=["admin-playground"])


class PlaygroundRun(BaseModel):
    pipeline_name: str = Field(min_length=1)
    message: str = Field(min_length=1)
    temperature: float | None = None


@router.post("/run")
async def playground_run(
    body: PlaygroundRun, request: Request, background: BackgroundTasks
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": body.pipeline_name,
        "messages": [{"role": "user", "content": body.message}],
    }
    if body.temperature is not None:
        payload["temperature"] = body.temperature
    response, trace_id = await execute_chat(request.app, background, payload, "playground")

    data: dict[str, Any] | None = None
    if isinstance(response.body, (bytes, bytearray)):
        try:
            data = json.loads(response.body)
        except (ValueError, TypeError):
            data = None
    return {
        "status_code": response.status_code,
        "response": data,
        "trace_id": trace_id,
    }
