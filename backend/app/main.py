"""FastAPI 入口。lifespan：建表、首次初始化、启动清理；/v1/* 与 /api/* 需认证；托管前端产物。"""

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
from app.gateways.namespaces import router as namespaces_router
from app.portal import router as portal_router
from app.repos import get_setting
from app.settings import settings

logger = logging.getLogger("muti_llm")
# uvicorn 只给自己的 logger 配 handler，根日志器保持默认 WARNING——不在这里放开 INFO，
# bootstrap 的「管理员初始密码」「启动清理」等一次性信息会被静默丢弃（真实事故：初始密码无法找回）
logging.basicConfig(level=logging.INFO)

# main.py 位于 <root>/backend/app/ → 前端产物在 <root>/frontend/dist（Dockerfile 保持同构布局）
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

# 无需管理会话的端点（多用户 T46）：登录自不必说；登出容忍无效令牌（幂等 200）
ADMIN_AUTH_WHITELIST = ("/api/admin/auth/login", "/api/admin/auth/logout")
# 门户面白名单（T52）：语义同上
PORTAL_AUTH_WHITELIST = ("/api/portal/auth/login", "/api/portal/auth/logout")


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.startswith("Bearer ") else ""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = create_db_engine(settings.database_path)
    result = await bootstrap(engine)
    if result.service_api_key:
        logger.info("服务 API Key 首次生成（仅存哈希）；如需明文请到 Web UI 设置页重置")
    if result.initial_admin_password:
        logger.info(
            "管理员初始密码：%s（仅首次启动打印一次，请登录后修改）", result.initial_admin_password
        )
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.fernet_key = result.fernet_key
    app.state.service_api_key = result.service_api_key  # 首次明文，仅内存
    app.state.service_api_key_hash = result.service_api_key_hash
    app.state.admin_user_id = result.admin_user_id
    app.state.initial_admin_password = result.initial_admin_password  # 首次明文，仅内存
    app.state.started_at = datetime.now(UTC)

    # 启动时按保留期清理旧日志与过期会话
    from app.auth_svc import purge_expired_sessions
    from app.logging_svc import cleanup_old_logs

    async with app.state.session_factory() as session:
        retention = await get_setting(session, KEY_LOG_RETENTION_DAYS)
        days = int(retention) if retention else settings.log_retention_days
        deleted = await cleanup_old_logs(session, days)
        purged = await purge_expired_sessions(session)
        await session.commit()
    if deleted:
        logger.info("启动清理：%d 条过期日志（保留 %d 天）", deleted, days)
    if purged:
        logger.info("启动清理：%d 条过期管理会话", purged)

    yield
    await engine.dispose()


app = FastAPI(title="muti_llm", version="0.1.0", lifespan=lifespan)
app.include_router(admin_router)
app.include_router(portal_router)
app.include_router(gateway_router)
app.include_router(namespaces_router)


@app.middleware("http")
async def authenticate(request: Request, call_next):  # type: ignore[no-untyped-def]
    """三平面认证（多用户 T46/T52）：按路径前缀分流，令牌空间互不通用。

    - /v1/*：用户 Key 或兼容 Key（旧服务 Key → 内置 admin）；错误返回 OpenAI error 结构。
    - /api/admin/*：管理台会话令牌（login/logout 白名单），要求 admin 角色；错误返回 {"detail"}。
    - /api/portal/*（T52）：同一会话体系，任何有登录凭据的启用用户；错误返回 {"detail"} 结构。
    管理/门户两平面的令牌解析 Bearer 头优先、缺失回退 HttpOnly 会话 Cookie（T54 持久登录）；
    已认证响应顺带续期 Cookie（与会话滑动续期同窗）。身份写入 request.state.caller，
    供网关执行路径显式穿参（D25）。
    """
    from app import auth_svc

    path = request.url.path
    cookie_plane = ""
    token = ""
    if path.startswith("/v1/"):
        token = _bearer_token(request)
        result = await auth_svc.resolve_data_plane_identity(request.app, token)
        if isinstance(result, auth_svc.AuthError):
            return openai_error(result.status, result.message, code=result.code)
        request.state.caller = result
    elif path.startswith("/api/admin/") and path not in ADMIN_AUTH_WHITELIST:
        token = auth_svc.request_session_token(request, plane="admin")
        async with request.app.state.session_factory() as session:
            result = await auth_svc.verify_session(session, token)
            if isinstance(result, auth_svc.AuthError):
                await session.rollback()
                return JSONResponse({"detail": result.message}, status_code=result.status)
            await session.commit()  # 滑动续期落库
        request.state.caller = result
        cookie_plane = "admin"
    elif path.startswith("/api/portal/") and path not in PORTAL_AUTH_WHITELIST:
        token = auth_svc.request_session_token(request, plane="portal")
        async with request.app.state.session_factory() as session:
            result = await auth_svc.verify_session(session, token, require_role=None)
            if isinstance(result, auth_svc.AuthError):
                await session.rollback()
                return JSONResponse({"detail": result.message}, status_code=result.status)
            await session.commit()  # 滑动续期落库
        request.state.caller = result
        cookie_plane = "portal"
    response = await call_next(request)
    if cookie_plane:
        auth_svc.set_session_cookie(response, token, plane=cookie_plane)
    return response


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
