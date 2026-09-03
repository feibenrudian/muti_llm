"""FastAPI 入口。lifespan：建表、首次初始化、启动清理；/v1/* 需 Bearer 认证；托管前端静态资源。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.admin import router as admin_router
from app.bootstrap import KEY_LOG_RETENTION_DAYS, bootstrap
from app.db import create_db_engine, create_session_factory
from app.gateways.llm_gateway import openai_error
from app.gateways.llm_gateway import router as gateway_router
from app.repos import get_setting
from app.security import verify_service_key
from app.settings import settings

logger = logging.getLogger("muti_llm")

# main.py 位于 <root>/backend/app/ → 前端产物在 <root>/frontend/dist（Dockerfile 保持同构布局）
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = create_db_engine(settings.database_path)
    result = await bootstrap(engine)
    if result.service_api_key:
        logger.info("服务 API Key 首次生成（仅存哈希）；如需明文请到 Web UI 设置页重置")
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.fernet_key = result.fernet_key
    app.state.service_api_key = result.service_api_key  # 首次明文，仅内存
    app.state.service_api_key_hash = result.service_api_key_hash
    app.state.started_at = datetime.now(UTC)

    # 启动时按保留期清理旧日志
    from app.logging_svc import cleanup_old_logs

    async with app.state.session_factory() as session:
        retention = await get_setting(session, KEY_LOG_RETENTION_DAYS)
        days = int(retention) if retention else settings.log_retention_days
        deleted = await cleanup_old_logs(session, days)
        await session.commit()
    if deleted:
        logger.info("启动清理：%d 条过期日志（保留 %d 天）", deleted, days)

    yield
    await engine.dispose()


app = FastAPI(title="muti_llm", version="0.1.0", lifespan=lifespan)
app.include_router(admin_router)
app.include_router(gateway_router)


@app.middleware("http")
async def require_service_key(request: Request, call_next):  # type: ignore[no-untyped-def]
    if request.url.path.startswith("/v1/"):
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        stored = getattr(request.app.state, "service_api_key_hash", "")
        if not stored or not token or not verify_service_key(token, stored):
            return openai_error(401, "Invalid API key", code="invalid_api_key")
    return await call_next(request)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request) -> dict[str, bool]:
    """就绪检查：lifespan 已完成初始化（engine/session_factory 就位）。"""
    return {"ready": hasattr(request.app.state, "engine")}


def _mount_spa() -> None:
    """存在 frontend/dist 时托管 SPA（单进程交付，tech-plan 决策/T29）。"""
    if not (FRONTEND_DIST / "index.html").exists():
        return
    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):  # type: ignore[no-untyped-def]
        reserved = ("api/", "v1/", "health", "ready", "docs", "openapi", "redoc")
        if full_path.startswith(reserved):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if full_path:  # 根路径 / 直接返回 index.html
            candidate = FRONTEND_DIST / full_path
            if candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")


_mount_spa()


def main() -> None:
    """`python -m app.main` 直接启动（等价 uvicorn app.main:app）。"""
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
