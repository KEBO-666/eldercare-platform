"""系统管理与审计 API：用户、角色权限、组织、审计、配置、备份与恢复。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.deps import require
from app.core.exceptions import RuleViolation
from app.core.response import ok, paged
from app.db.session import get_db
from app.models import User
from app.services import admin_service

router = APIRouter(prefix="/admin", tags=["系统管理与审计"])


class UserCreateRequest(BaseModel):
    username: str
    password: str
    real_name: str | None = None
    role_id: int | None = None
    role: str | None = None
    role_code: str | None = None
    org_id: int | None = None
    data_scope: str | None = None
    phone: str | None = None
    is_active: bool = True


class UserUpdateRequest(BaseModel):
    real_name: str | None = None
    role_id: int | None = None
    role: str | None = None
    role_code: str | None = None
    org_id: int | None = None
    data_scope: str | None = None
    phone: str | None = None
    is_active: bool | None = None


class ResetPasswordRequest(BaseModel):
    password: str | None = None
    new_password: str | None = None


class OrgCreateRequest(BaseModel):
    code: str
    name: str
    level: str | None = None
    parent_id: int | None = None
    sort: int = 0
    remark: str | None = None
    is_active: bool = True


def _confirm_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# ==========================================================================
# 用户管理
# ==========================================================================
@router.get("/users", summary="用户列表")
def list_users(
    page: int = 1,
    size: int = 20,
    keyword: str | None = None,
    role: str | None = None,
    role_code: str | None = None,
    org_id: int | None = None,
    is_active: bool | None = None,
    user: User = Depends(require("admin:user")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = admin_service.list_users(
        db,
        user,
        page=page,
        size=size,
        keyword=keyword,
        role=role or role_code,
        org_id=org_id,
        is_active=is_active,
    )
    return paged(items, total, page, size)


@router.post("/users", summary="新建用户")
def create_user(
    payload: UserCreateRequest,
    request: Request,
    user: User = Depends(require("admin:user")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(admin_service.create_user(db, user, payload.model_dump(exclude_unset=True), request))


@router.put("/users/{user_id}", summary="编辑用户")
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    request: Request,
    user: User = Depends(require("admin:user")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(admin_service.update_user(db, user, user_id, payload.model_dump(exclude_unset=True), request))


@router.post("/users/{user_id}/reset-password", summary="重置用户密码")
def reset_password(
    user_id: int,
    request: Request,
    payload: ResetPasswordRequest | None = Body(default=None),
    user: User = Depends(require("admin:user")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = payload.model_dump(exclude_unset=True) if payload is not None else {}
    return ok(admin_service.reset_password(db, user, user_id, data, request))


# ==========================================================================
# 角色与权限
# ==========================================================================
@router.get("/roles", summary="角色列表（含权限点数量）")
def list_roles(
    _user: User = Depends(require("admin:user")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(admin_service.list_roles(db))


@router.get("/permissions", summary="权限点列表（含数量统计）")
def list_permissions(
    _user: User = Depends(require("admin:user")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(admin_service.list_permissions(db))


# ==========================================================================
# 组织
# ==========================================================================
@router.get("/orgs", summary="组织树")
def list_orgs(
    level: str | None = None,
    _user: User = Depends(require("admin:org")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tree = admin_service.list_orgs(db, level=level)
    return ok(tree)


@router.post("/orgs", summary="新建组织")
def create_org(
    payload: OrgCreateRequest,
    request: Request,
    user: User = Depends(require("admin:org")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(admin_service.create_org(db, user, payload.model_dump(exclude_unset=True), request))


# ==========================================================================
# 审计
# ==========================================================================
@router.get("/audit-logs", summary="审计日志查询")
def list_audit_logs(
    page: int = 1,
    size: int = 20,
    action: str | None = None,
    entity_type: str | None = None,
    entity: str | None = None,
    user_id: int | None = None,
    username: str | None = None,
    user: str | None = None,
    start: str | None = None,
    end: str | None = None,
    start_time: str | None = Query(default=None),
    end_time: str | None = Query(default=None),
    _user: User = Depends(require("admin:audit")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if user_id is None and user and user.strip().isdigit():
        user_id = int(user.strip())
    if username is None and user and not user.strip().isdigit():
        username = user
    items, total = admin_service.list_audit_logs(
        db,
        page=page,
        size=size,
        action=action,
        entity_type=entity_type or entity,
        user_id=user_id,
        username=username,
        start=start or start_time,
        end=end or end_time,
    )
    return paged(items, total, page, size)


@router.get("/audit/verify", summary="审计哈希链校验")
def verify_audit(
    limit: int = 5000,
    _user: User = Depends(require("admin:audit")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(admin_service.verify_audit_chain(db, limit=limit))


# ==========================================================================
# 运行期配置
# ==========================================================================
@router.get("/config", summary="运行期配置（含默认派单权重）")
def get_config(
    _user: User = Depends(require("admin:config")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(admin_service.list_configs(db))


@router.put("/config", summary="更新运行期配置（upsert）")
def update_config(
    request: Request,
    payload: dict[str, Any] = Body(...),
    user: User = Depends(require("admin:config")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(admin_service.set_config(db, user, payload, request))


# ==========================================================================
# 备份与恢复
# ==========================================================================
@router.post("/backup", summary="手动备份")
def create_backup(
    request: Request,
    user: User = Depends(require("admin:backup")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(admin_service.create_backup(db, user, request, backup_type="manual"))


@router.get("/backups", summary="备份列表")
def list_backups(
    page: int = 1,
    size: int = 20,
    backup_type: str | None = None,
    status: str | None = None,
    _user: User = Depends(require("admin:backup")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = admin_service.list_backups(
        db, page=page, size=size, backup_type=backup_type, status=status
    )
    return paged(items, total, page, size)


@router.post("/restore", summary="按时间点恢复（需 confirm=true）")
def restore(
    request: Request,
    payload: dict[str, Any] | None = Body(default=None),
    confirm: bool | None = Query(default=None, description="二次确认"),
    backup_id: int | None = Query(default=None),
    user: User = Depends(require("admin:backup")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data: dict[str, Any] = payload or {}
    raw_confirm = data.get("confirm", False)
    if not _confirm_true(raw_confirm) and not _confirm_true(confirm):
        raise RuleViolation("恢复操作需二次确认（confirm=true）")
    bid = data.get("backup_id", data.get("id", backup_id))
    file_path = (
        data.get("file_path")
        or data.get("backup_file")
        or data.get("file")
        or data.get("path")
    )
    report = admin_service.restore_backup(
        db,
        backup_id=bid,
        file_path=file_path,
        operator=user,
        request=request,
    )
    return ok(report)
