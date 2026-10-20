"""认证领域服务（多用户 T46）：数据面 Key 身份解析与会话令牌。

三平面共用 Authorization: Bearer 但令牌空间互不通用（D26）：
- 数据面 /v1/*：用户 Key（sk-mu-）或兼容 Key（旧服务 Key → 内置 admin，零查询短路，D28）。
- 管理面 /api/admin/*：登录换取的会话令牌（只存哈希、滑动续期），要求 admin 角色。
- 门户面 /api/portal/*（T52）：同一会话体系，任何有登录凭据的启用用户可自助查询。

身份以 Caller 显式穿参进入执行路径（D25）；归属落库由调用方携带 caller 的字段（D27）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request, Response
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.orm import AdminSession, User, UserApiKey, utcnow
from app.repos import (
    create_admin_session,
    find_user_by_username,
    find_user_key_by_hash,
    find_valid_session,
    revoke_user_sessions,
)
from app.security import (
    generate_password_salt,
    generate_session_token,
    hash_password,
    hash_token,
    verify_password,
    verify_service_key,
)
from app.settings import settings

SESSION_TTL_HOURS = 24.0
SESSION_COOKIE_MAX_AGE = int(SESSION_TTL_HOURS * 3600)
ADMIN_SESSION_COOKIE = "muti.admin_session"
PORTAL_SESSION_COOKIE = "muti.portal_session"
KEY_USAGE_THROTTLE_SECONDS = 60.0

# last_used_at 节流记账（D31）：进程内 {key_id: monotonic}；重启清零只导致多写一次
_last_usage_write: dict[int, float] = {}

# 登录失败锁定（T51）：同用户名连续失败 LOCK_THRESHOLD 次锁定 LOCK_SECONDS 秒（进程内，重启清零）
LOCK_THRESHOLD = 5
LOCK_SECONDS = 300.0
_lock_state: dict[str, list[float]] = {}  # username → [失败时间戳(monotonic), ...]


def _lockout_remaining(username: str, now: float) -> float:
    """剩余锁定秒数（<=0 未锁定）；窗口过期清空计数。"""
    failures = [t for t in _lock_state.get(username, []) if now - t < LOCK_SECONDS]
    _lock_state[username] = failures
    if len(failures) >= LOCK_THRESHOLD:
        return LOCK_SECONDS - (now - failures[0])
    return 0.0


def _record_login_failure(username: str, now: float) -> None:
    failures = [t for t in _lock_state.get(username, []) if now - t < LOCK_SECONDS]
    failures.append(now)
    _lock_state[username] = failures


def reset_login_failures(username: str) -> None:
    _lock_state.pop(username, None)


@dataclass(frozen=True)
class Caller:
    """请求身份：贯穿数据面执行路径与管理端点的显式穿参对象。"""

    user_id: int
    username: str
    role: str  # admin | user
    key_id: int | None  # None = 兼容 Key / 管理会话 / Playground
    via: str  # key | legacy | session | playground


@dataclass(frozen=True)
class AuthError:
    """认证/授权失败（中间件按平面转换响应结构：数据面 OpenAI error，管理面 {"detail"}）。"""

    status: int  # 401 | 403
    message: str
    code: str  # invalid_api_key | user_disabled | unauthorized | forbidden


# ---- 数据面 ------------------------------------------------------------------------


async def resolve_data_plane_identity(app: Any, token: str) -> Caller | AuthError:
    """Bearer → Caller。顺序（D28）：空 → 401；兼容 Key 哈希命中 → admin（零查询）；
    否则查 user_api_keys（不存在/停用 → 401；用户停用 → 403 user_disabled）。"""
    if not token:
        return AuthError(401, "Invalid API key", "invalid_api_key")
    legacy_hash = getattr(app.state, "service_api_key_hash", "")
    if legacy_hash and verify_service_key(token, legacy_hash):
        return Caller(
            user_id=int(app.state.admin_user_id),
            username="admin",
            role="admin",
            key_id=None,
            via="legacy",
        )
    async with app.state.session_factory() as session:
        key_row = await find_user_key_by_hash(session, hash_token(token))
        if key_row is None or not key_row.enabled:
            return AuthError(401, "Invalid API key", "invalid_api_key")
        user = await session.get(User, key_row.user_id)
        if user is None:
            return AuthError(401, "Invalid API key", "invalid_api_key")
        if not user.enabled:
            return AuthError(403, "该账号已停用，请联系管理员", "user_disabled")
        await _touch_key_usage(session, key_row)
        await session.commit()
        return Caller(user.id, user.username, user.role, key_row.id, "key")


async def _touch_key_usage(session: AsyncSession, key_row: UserApiKey) -> None:
    """last_used_at 每 Key 每 60s 至多一次 UPDATE（D31）。"""
    now = time.monotonic()
    last = _last_usage_write.get(key_row.id)
    if last is not None and now - last < KEY_USAGE_THROTTLE_SECONDS:
        return
    _last_usage_write[key_row.id] = now
    key_row.last_used_at = utcnow()
    await session.flush()


# ---- 管理面会话 --------------------------------------------------------------------


def _cookie_name(plane: str) -> str:
    return ADMIN_SESSION_COOKIE if plane == "admin" else PORTAL_SESSION_COOKIE


def set_session_cookie(response: Response, token: str, *, plane: str) -> None:
    """会话 Cookie 下发/续期（T54 持久登录）：HttpOnly 防 XSS 窃取、SameSite=Lax 防 CSRF，
    路径限定对应平面 API（管理/门户 Cookie 互不可见）。max_age 与会话滑动窗口一致。"""
    response.set_cookie(
        key=_cookie_name(plane),
        value=token,
        max_age=SESSION_COOKIE_MAX_AGE,
        path=f"/api/{plane}",
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, *, plane: str) -> None:
    response.delete_cookie(
        key=_cookie_name(plane),
        path=f"/api/{plane}",
        httponly=True,
        samesite="lax",
    )


def request_session_token(request: Request, *, plane: str) -> str:
    """会话令牌解析：Bearer 头优先（API 客户端/既有前端），缺失回退 Cookie（浏览器持久登录）。"""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.cookies.get(_cookie_name(plane), "")


def user_payload(user: User, expires_at: datetime | None = None) -> dict[str, Any]:
    """登录/me 响应的用户身份体（管理面与门户面同构，T52 抽取共用）。"""
    data: dict[str, Any] = {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
    }
    if expires_at is not None:
        data["expires_at"] = expires_at.astimezone(UTC).isoformat()
    return data


async def verify_session(
    session: AsyncSession, token: str, *, require_role: str | None = "admin"
) -> Caller | AuthError:
    """会话令牌 → Caller；顺带滑动续期（expires_at/last_seen_at 前移）。

    require_role="admin"（管理面）时角色非 admin → 403；require_role=None
    （门户面 T52）时任何有登录凭据的启用用户均放行。停用用户/过期 → 401。
    """
    if not token:
        return AuthError(401, "未登录或会话已过期", "unauthorized")
    found = await find_valid_session(session, token)
    if found is None:
        return AuthError(401, "未登录或会话已过期", "unauthorized")
    admin_session, user = found
    if require_role is not None and user.role != require_role:
        return AuthError(403, "需要管理员权限", "forbidden")
    admin_session.expires_at = utcnow() + timedelta(hours=SESSION_TTL_HOURS)
    admin_session.last_seen_at = utcnow()
    await session.flush()
    return Caller(user.id, user.username, user.role, None, "session")


async def login(
    session: AsyncSession, username: str, password: str
) -> tuple[str, datetime, User] | AuthError:
    """口令登录 → (明文令牌, 过期时间, 用户)。

    失败统一 401 文案（防用户名枚举，NFR-8）；连续失败触发临时锁定（T51）。
    """
    now = time.monotonic()
    remaining = _lockout_remaining(username, now)
    if remaining > 0:
        return AuthError(
            401, f"失败次数过多，账号已锁定，请 {int(remaining) // 60 + 1} 分钟后再试", "locked"
        )
    user = await find_user_by_username(session, username)
    if (
        user is None
        or not user.enabled
        or not user.password_hash
        or not verify_password(
            password, user.password_salt, user.password_hash, settings.pbkdf2_iterations
        )
    ):
        _record_login_failure(username, now)
        return AuthError(401, "用户名或密码错误", "invalid_credentials")
    reset_login_failures(username)
    token = generate_session_token()
    row = await create_admin_session(session, user.id, token, ttl_hours=SESSION_TTL_HOURS)
    return token, row.expires_at, user


async def revoke_session(session: AsyncSession, token: str) -> None:
    """登出：删除令牌对应会话行（幂等，白名单端点，无效令牌静默）。"""
    if not token:
        return
    await session.execute(delete(AdminSession).where(AdminSession.token_hash == hash_token(token)))
    await session.flush()


async def change_password(
    session: AsyncSession,
    user_id: int,
    *,
    old_password: str = "",
    new_password: str,
    admin_reset: bool = False,
) -> None:
    """改密（admin_reset=True 为主管管理员重置，免验旧口令）；成功后轮换盐并吊销旧会话外置处理。

    校验失败抛 ValueError（路由层转 400/422）。
    """
    if len(new_password) < 8:
        raise ValueError("密码至少 8 位")
    user = await session.get(User, user_id)
    if user is None:
        raise ValueError("用户不存在")
    if not admin_reset:
        if not user.password_hash or not verify_password(
            old_password, user.password_salt, user.password_hash, settings.pbkdf2_iterations
        ):
            raise ValueError("旧密码不正确")
    salt = generate_password_salt()
    user.password_hash = hash_password(new_password, salt, settings.pbkdf2_iterations)
    user.password_salt = salt
    await session.flush()


async def revoke_other_sessions(session: AsyncSession, user_id: int, *, keep_token: str) -> None:
    """吊销用户除当前会话外的全部会话（改密后调用，见 repos.revoke_user_sessions）。"""
    await revoke_user_sessions(session, user_id, keep_token=keep_token)


async def write_audit(
    session: AsyncSession,
    *,
    actor: Caller | None,
    action: str,
    target_type: str = "",
    target_id: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    """管理动作审计流水（T51）：操作人/动作/目标/时间，只增不改。"""
    from app.orm import AdminAuditLog

    session.add(
        AdminAuditLog(
            actor_user_id=actor.user_id if actor else 0,
            actor_username=actor.username if actor else "",
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            detail=detail or {},
        )
    )
    await session.flush()


async def purge_expired_sessions(session: AsyncSession, keep_days: int = 7) -> int:
    """清理过期超过 keep_days 的会话行（启动维护调用；过期但未超窗的行保留以便诊断）。"""
    cutoff = utcnow() - timedelta(days=keep_days)
    result = await session.execute(delete(AdminSession).where(AdminSession.expires_at < cutoff))
    return int(result.rowcount or 0)
