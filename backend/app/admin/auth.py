"""管理端认证 API（多用户 T46）：登录 / 登出 / 当前身份 / 修改自己的密码。

登录与登出在中间件白名单（main.ADMIN_AUTH_WHITELIST）；其余 /api/admin/* 需管理员会话。
一般用户的自助入口在 /api/portal/*（T52）：管理台登录成功后角色非 admin 一律拒绝。
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_svc import (
    AuthError,
    change_password,
    clear_session_cookie,
    login,
    request_session_token,
    revoke_other_sessions,
    revoke_session,
    set_session_cookie,
    user_payload,
    write_audit,
)
from app.deps import get_session
from app.orm import User

router = APIRouter(prefix="/auth", tags=["admin-auth"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=200)


class PasswordBody(BaseModel):
    old_password: str = Field(min_length=0, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


def _auth_error(exc: AuthError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


@router.post("/login")
async def auth_login(
    body: LoginBody, response: Response, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    result = await login(session, body.username, body.password)
    if isinstance(result, AuthError):
        await write_audit(
            session,
            actor=None,
            action="auth.login_failed",
            target_type="user",
            target_id=body.username,
        )
        await session.commit()
        raise _auth_error(result)
    token, expires_at, user = result
    if user.role != "admin":
        # 管理台只发管理员会话（T52）：一般用户请走门户登录；刚签发的令牌立即吊销
        await revoke_session(session, token)
        await write_audit(
            session,
            actor=None,
            action="auth.login_rejected_role",
            target_type="user",
            target_id=str(user.id),
            detail={"username": user.username},
        )
        await session.commit()
        raise HTTPException(status_code=403, detail="需要管理员权限，请使用门户登录")
    await write_audit(
        session,
        actor=None,
        action="auth.login",
        target_type="user",
        target_id=str(user.id),
        detail={"username": user.username},
    )
    set_session_cookie(response, token, plane="admin")
    await session.commit()
    return {
        "token": token,
        "expires_at": expires_at.astimezone(UTC).isoformat(),
        "user": user_payload(user),
    }


@router.post("/logout")
async def auth_logout(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
) -> dict[str, bool]:
    """白名单端点：无效令牌也幂等 200；Cookie 登录的浏览器无 Bearer，从 Cookie 解析令牌。"""
    token = request_session_token(request, plane="admin")
    await revoke_session(session, token)
    clear_session_cookie(response, plane="admin")
    await session.commit()
    return {"ok": True}


@router.get("/me")
async def auth_me(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    user = await session.get(User, request.state.caller.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user_payload(user)


@router.post("/password")
async def auth_change_password(
    body: PasswordBody, request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, bool]:
    caller = request.state.caller
    token = request_session_token(request, plane="admin")
    try:
        await change_password(
            session,
            caller.user_id,
            old_password=body.old_password,
            new_password=body.new_password,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    # 改密后吊销本人其他会话（当前会话保留，FR-21）
    await revoke_other_sessions(session, caller.user_id, keep_token=token)
    await session.commit()
    return {"ok": True}
