"""FastAPI 依赖：认证、RBAC 权限、数据范围控制。

数据范围约定（与赛题一致）：本人 SELF / 本网格 GRID / 本机构 ORG / 全街道 STREET。
- 超级管理员与街道监管员不受限；
- 机构管理员：按 community_org_id / org_id 过滤；
- 网格员：按 grid_org_id 过滤；
- 护理员：按 nurse_id（本人 staff.id）过滤；
- 老人/家属：按 elder_id（绑定本人的老人）过滤。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, Request
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.enums import DataScope, RoleCode
from app.core.exceptions import Forbidden, Unauthorized
from app.core.security import decode_token
from app.db.session import get_db
from app.models import Elder, Staff, User, WorkOrder


# ---------------- 认证 ----------------
def token_from_request(request: Request) -> str | None:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get("access_token")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = token_from_request(request)
    if not token:
        raise Unauthorized("未登录或登录已过期")
    try:
        payload = decode_token(token)
    except Exception as exc:  # noqa: BLE001
        raise Unauthorized("登录凭证无效") from exc
    user = db.get(User, int(payload.get("sub", 0)))
    if not user or not user.is_active:
        raise Unauthorized("用户不存在或已停用")
    return user


# ---------------- 权限 ----------------
def permission_codes(user: User) -> set[str]:
    if user.role and user.role.code == RoleCode.SUPER:
        return {"*"}
    return {p.code for p in user.role.permissions} if user.role else set()


def has_permission(user: User, *codes: str) -> bool:
    perms = permission_codes(user)
    return "*" in perms or all(c in perms for c in codes)


def require(*codes: str, any_of: bool = False) -> Callable[..., User]:
    """依赖工厂：require("elder:read") 或 require("a:read", "b:read", any_of=True)。"""

    def dependency(user: User = Depends(get_current_user)) -> User:
        perms = permission_codes(user)
        if "*" in perms:
            return user
        ok = any(c in perms for c in codes) if any_of else all(c in perms for c in codes)
        if not ok:
            raise Forbidden(f"缺少权限：{', '.join(codes)}")
        return user

    return dependency


# ---------------- 数据范围 ----------------
def get_staff(db: Session, user: User) -> Staff | None:
    return db.execute(select(Staff).where(Staff.user_id == user.id)).scalar_one_or_none()


def get_elder_of_user(db: Session, user: User) -> Elder | None:
    return db.execute(select(Elder).where(Elder.user_id == user.id, Elder.is_deleted == 0)).scalar_one_or_none()


def effective_scope(user: User) -> str:
    if user.data_scope:
        return user.data_scope
    return user.role.data_scope if user.role else DataScope.SELF


def scope_condition(model: Any, user: User, db: Session):
    """返回该模型上应追加的数据范围过滤条件；None 表示不限制。"""
    role_code = user.role.code if user.role else RoleCode.ELDER
    scope = effective_scope(user)

    if role_code in (RoleCode.SUPER, RoleCode.STREET) or scope == DataScope.STREET:
        return None

    community_col = getattr(model, "community_org_id", None)
    grid_col = getattr(model, "grid_org_id", None)
    org_col = getattr(model, "org_id", None)
    elder_col = getattr(model, "elder_id", None)

    if scope == DataScope.ORG:
        if community_col is not None:
            return community_col == user.org_id
        if org_col is not None:
            return org_col == user.org_id
        return None

    if scope == DataScope.GRID:
        if grid_col is not None:
            return grid_col == user.org_id
        if community_col is not None:
            return community_col == user.org_id
        return None

    # SELF
    if role_code == RoleCode.NURSE:
        staff = get_staff(db, user)
        nurse_col = getattr(model, "nurse_id", None)
        if staff is not None and nurse_col is not None:
            return nurse_col == staff.id
        # 护理员对无 nurse_id 列的实体（老人档案/指标/预约等）只能看到自己服务过的老人
        if staff is not None:
            served_ids = select(WorkOrder.elder_id).where(WorkOrder.nurse_id == staff.id)
            if model is Elder:
                return Elder.id.in_(served_ids)
            if elder_col is not None:
                return elder_col.in_(served_ids)
        return None
    elder = get_elder_of_user(db, user)
    if elder is not None and elder_col is not None:
        return elder_col == elder.id
    if elder is not None and model is Elder:
        return Elder.id == elder.id
    return None


def apply_scope(stmt: Select, model: Any, user: User, db: Session) -> Select:
    cond = scope_condition(model, user, db)
    return stmt.where(cond) if cond is not None else stmt


def visible_elder_ids(db: Session, user: User) -> list[int] | None:
    """返回可访问的老人 id 列表；None 表示不受限。"""
    cond = scope_condition(Elder, user, db)
    if cond is None:
        return None
    return list(db.execute(select(Elder.id).where(Elder.is_deleted == 0, cond)).scalars().all())


def ensure_elder_visible(db: Session, user: User, elder_id: int) -> None:
    ids = visible_elder_ids(db, user)
    if ids is not None and elder_id not in ids:
        raise Forbidden("无权访问该老人档案（数据范围限制）")
