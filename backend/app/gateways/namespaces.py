"""命名空间网关：/v1/pipeline（虚拟模型）与 /v1/route/{ident}（按供应商透传）的分离接入视图。

与老入口 /v1/* 共用执行路径（_run_pipeline/_passthrough），区别只在 model 解析范围：
pipeline 命名空间只认 Pipeline 名；route 命名空间只认该供应商的模型短名（供应商可用
slug 或数字 id 定位）。接入开关（settings 页）对命名空间同样生效：关闭 → 整体 404。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.factory import build_adapter
from app.gateways.llm_gateway import (
    _passthrough,
    _run_pipeline,
    model_exposure,
    openai_error,
    validate_chat_body,
)
from app.orm import LlmModel, Pipeline, Provider
from app.repos import find_enabled_pipeline_by_name

router = APIRouter(tags=["gateway-namespaces"])

# 上游模型列表缓存（provider_id → (取回时刻, 列表)）：TTL 内不打上游；
# 供应商更新/删除/手动测试时主动失效（invalidate_route_list）。测试可调 TTL=0 立即过期。
ROUTE_LIST_TTL_SECONDS = 30.0
_list_cache: dict[int, tuple[float, list[str]]] = {}


def invalidate_route_list(provider_id: int) -> None:
    _list_cache.pop(provider_id, None)


def clear_route_list_cache() -> None:
    _list_cache.clear()


async def _resolve_provider(session: AsyncSession, ident: str) -> Provider | None:
    """数字 id 优先，其次 slug（全数字 slug 与 id 撞车时 id 赢）。"""
    if ident.isdigit():
        row = await session.get(Provider, int(ident))
        if row is not None:
            return row
    result = await session.execute(select(Provider).where(Provider.slug == ident))
    return result.scalars().first()


def _model_entry(model_id: str) -> dict[str, Any]:
    return {"id": model_id, "object": "model", "created": 0, "owned_by": "muti_llm"}


# ---- pipeline 命名空间：只含 Pipeline 虚拟模型 --------------------------------------


@router.get("/v1/pipeline")
async def pipeline_namespace_info(request: Request) -> Response:
    factory = request.app.state.session_factory
    async with factory() as session:
        expose_virtual, _ = await model_exposure(session)
    if not expose_virtual:
        return openai_error(404, "namespace 'pipeline' not found", code="model_not_found")
    return JSONResponse(
        {
            "namespace": "pipeline",
            "description": "虚拟模型（Pipeline）视图：model 填 Pipeline 名，多模型并行 + 裁判聚合",
            "models": "/v1/pipeline/models",
            "chat_completions": "/v1/pipeline/chat/completions",
        }
    )


@router.get("/v1/pipeline/models")
async def pipeline_models(request: Request) -> Response:
    factory = request.app.state.session_factory
    async with factory() as session:
        expose_virtual, _ = await model_exposure(session)
        if not expose_virtual:
            return openai_error(404, "namespace 'pipeline' not found", code="model_not_found")
        pipelines: Sequence[Pipeline] = (
            (await session.execute(select(Pipeline).where(Pipeline.enabled.is_(True))))
            .scalars()
            .all()
        )
    return JSONResponse({"object": "list", "data": [_model_entry(p.name) for p in pipelines]})


@router.post("/v1/pipeline/chat/completions")
async def pipeline_chat(request: Request, background: BackgroundTasks) -> Response:
    t0 = time.perf_counter()
    try:
        body = await request.json()
    except Exception:
        return openai_error(400, "请求体不是合法 JSON")
    client_ip = request.client.host if request.client else ""

    error, _messages, model_field, params, stream, has_tools = validate_chat_body(body)
    if error is not None:
        return error

    app = request.app
    async with app.state.session_factory() as session:
        expose_virtual, _ = await model_exposure(session)
        pipeline = (
            await find_enabled_pipeline_by_name(session, model_field) if expose_virtual else None
        )
        if pipeline is None:
            return openai_error(404, f"model '{model_field}' not found", code="model_not_found")
        if has_tools and (not pipeline.tool_aggregation or pipeline.strategy != "council"):
            # 工具聚合仅 council 实现：开关关闭或其余策略（ICE）→ 明确拒绝（不静默忽略 tools）
            return openai_error(
                400, "聚合模式（Pipeline）暂不支持工具调用: tools", param="tools"
            )
        stream_process_raw = body.get("stream_process")
        stream_process = (
            bool(stream_process_raw)
            if stream_process_raw is not None
            else bool(pipeline.stream_process)
        )
        response, _ = await _run_pipeline(
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
        return response


# ---- route 命名空间：按供应商透传真实模型 -------------------------------------------


@router.get("/v1/route")
async def route_namespace_info(request: Request) -> Response:
    factory = request.app.state.session_factory
    async with factory() as session:
        _, expose_routed = await model_exposure(session)
        if not expose_routed:
            return openai_error(404, "namespace 'route' not found", code="model_not_found")
        providers: Sequence[Provider] = (
            (await session.execute(select(Provider).where(Provider.enabled.is_(True))))
            .scalars()
            .all()
        )
    return JSONResponse(
        {
            "routes": [
                {"id": p.id, "slug": p.slug, "name": p.name}
                for p in sorted(providers, key=lambda r: r.id)
            ]
        }
    )


async def _upstream_model_ids(app: Any, provider: Provider) -> list[str]:
    """上游实时模型列表（TTL 缓存）。上游不可达时抛 AdapterError，由调用方回落。"""
    cached = _list_cache.get(provider.id)
    now = time.monotonic()
    if cached is not None and now - cached[0] < ROUTE_LIST_TTL_SECONDS:
        return cached[1]
    adapter = build_adapter(
        provider, fernet_key=app.state.fernet_key, timeout_seconds=15, max_retries=0
    )
    ids = await adapter.probe()
    _list_cache[provider.id] = (now, list(ids))
    return ids


async def _local_model_map(session: AsyncSession, provider_id: int) -> dict[str, bool]:
    """该供应商的本地模型：upstream_model_id → enabled（上游新模型不在表内 → 默认可工作）。"""
    rows = (
        (await session.execute(select(LlmModel).where(LlmModel.provider_id == provider_id)))
        .scalars()
        .all()
    )
    return {row.upstream_model_id: row.enabled for row in rows}


@router.get("/v1/route/{ident}/models")
async def route_models(ident: str, request: Request) -> Response:
    factory = request.app.state.session_factory
    async with factory() as session:
        _, expose_routed = await model_exposure(session)
        if not expose_routed:
            return openai_error(404, "namespace 'route' not found", code="model_not_found")
        provider = await _resolve_provider(session, ident)
        if provider is None or not provider.enabled:
            return openai_error(
                404, f"route '{ident}' not found", code="model_not_found"
            )
        local = await _local_model_map(session, provider.id)

    try:
        ids = await _upstream_model_ids(request.app, provider)
    except Exception:  # 上游不可达/不支持列表：回落库内已同步的启用模型
        ids = [model_id for model_id, enabled in local.items() if enabled]

    visible = [model_id for model_id in ids if local.get(model_id, True)]
    return JSONResponse({"object": "list", "data": [_model_entry(m) for m in visible]})


@router.post("/v1/route/{ident}/chat/completions")
async def route_chat(ident: str, request: Request, background: BackgroundTasks) -> Response:
    t0 = time.perf_counter()
    try:
        body = await request.json()
    except Exception:
        return openai_error(400, "请求体不是合法 JSON")
    client_ip = request.client.host if request.client else ""

    error, _messages, model_field, params, stream, _has_tools = validate_chat_body(body)
    if error is not None:
        return error

    app = request.app
    async with app.state.session_factory() as session:
        _, expose_routed = await model_exposure(session)
        if not expose_routed:
            return openai_error(
                404, f"model '{model_field}' not found", code="model_not_found"
            )
        provider = await _resolve_provider(session, ident)
        if provider is None or not provider.enabled:
            return openai_error(
                404, f"route '{ident}' not found", code="model_not_found"
            )

        rows = (
            (
                await session.execute(
                    select(LlmModel).where(
                        LlmModel.provider_id == provider.id,
                        LlmModel.upstream_model_id == model_field,
                    )
                )
            )
            .scalars()
            .all()
        )
        enabled_row = next((r for r in rows if r.enabled), None)
        if enabled_row is not None:
            model_row = enabled_row
        elif rows:
            # 本地明确停用（手动停用或同步下线）→ 拒绝；与列表过滤口径一致
            return openai_error(404, f"model '{model_field}' not found", code="model_not_found")
        else:
            # 上游新模型本地无记录 → 动态透传（默认参数；model_id=0 仅入日志，不落配置行）
            model_row = LlmModel(
                id=0,
                provider_id=provider.id,
                display_name=model_field,
                upstream_model_id=model_field,
                default_params={},
                enabled=True,
            )

        response, _ = await _passthrough(
            app, background, session, body, model_row, provider, params, stream, client_ip, t0
        )
        return response
