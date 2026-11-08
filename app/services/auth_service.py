"""认证服务：登录校验、JWT 签发、当前用户装配、登录审计。

- 密码使用 bcrypt 校验（`app.core.security.verify_password`）；
- 登录成功/失败均写审计（LOGIN / LOGIN_FAIL），失败提示统一为「用户名或密码错误」；
- 登录成功更新 `user.last_login_at` 并签发带 role/username 附加声明的 JWT。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.deps import effective_scope, permission_codes
from app.core.enums import AuditAction
from app.core.exceptions import Forbidden, Unauthorized
from app.core.security import create_access_token, verify_password
from app.models import User

#: 密码错误/用户不存在统一提示，避免账号枚举
LOGIN_FAIL_MESSAGE = "用户名或密码错误"


def build_user_payload(user: User) -> dict[str, Any]:
    """装配登录/当前用户返回体（含角色、数据范围与权限点）。"""
    perms = permission_codes(user)
    permissions = ["*"] if "*" in perms else sorted(perms)
    role = user.role
    org = user.org
    return {
        "id": user.id,
        "username": user.username,
        "real_name": user.real_name,
        "role_id": user.role_id,
        "role": role.code if role else None,
        "role_name": role.name if role else None,
        "org_id": user.org_id,
        "org_name": org.name if org else None,
        "data_scope": effective_scope(user),
        "permissions": permissions,
        "is_active": bool(user.is_active),
        "last_login_at": user.last_login_at,
    }


def _write_login_fail(
    db: Session,
    user: User | None,
    username: str,
    reason: str,
    request: Request | None,
) -> None:
    write_audit(
        db,
        user=user,
        action=AuditAction.LOGIN_FAIL,
        entity_type="user",
        entity_id=getattr(user, "id", None),
        detail=f"{reason}：{username}",
        request=request,
    )
    db.commit()


def login(
    db: Session,
    username: str,
    password: str,
    request: Request | None = None,
) -> dict[str, Any]:
    """校验账号密码并签发令牌；失败抛 Unauthorized / Forbidden 并落审计。"""
    username = (username or "").strip()
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user is None:
        _write_login_fail(db, None, username, "用户名不存在", request)
        raise Unauthorized(LOGIN_FAIL_MESSAGE)
    if not verify_password(password or "", user.password_hash):
        _write_login_fail(db, user, username, "密码错误", request)
        raise Unauthorized(LOGIN_FAIL_MESSAGE)
    if not user.is_active:
        _write_login_fail(db, user, username, "账号已停用", request)
        raise Forbidden("账号已停用，请联系管理员")

    user.last_login_at = datetime.now()
    role_code = user.role.code if user.role else None
    token = create_access_token(user.id, {"role": role_code, "username": user.username})
    write_audit(
        db,
        user=user,
        action=AuditAction.LOGIN,
        entity_type="user",
        entity_id=user.id,
        detail=f"登录成功：{username}",
        request=request,
    )
    db.commit()
    return {"token": token, "token_type": "Bearer", "user": build_user_payload(user)}
