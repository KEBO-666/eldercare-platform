"""系统管理服务：用户、角色权限、组织、审计、运行期配置、备份与恢复。"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import string
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.audit import verify_chain, write_audit
from app.core.config import settings
from app.core.deps import apply_scope, effective_scope, scope_condition
from app.core.enums import AuditAction, DataScope, OrgLevel, RoleCode
from app.core.exceptions import BadRequest, Conflict, Forbidden, NotFound, RuleViolation
from app.core.security import decrypt_field, encrypt_field, hash_password, mask_phone
from app.models import AuditLog, BackupRecord, ConfigItem, Org, Permission, Role, User
from app.tasks.backup_daily import run_daily_backup, table_counts

logger = logging.getLogger("eldercare.admin")

_DATA_SCOPES = {scope.value for scope in DataScope}
_ORG_LEVELS = {level.value for level in OrgLevel}
_ORG_LEVEL_RANK = {OrgLevel.STREET.value: 1, OrgLevel.COMMUNITY.value: 2, OrgLevel.GRID.value: 3}


# ==========================================================================
# 公共小工具
# ==========================================================================
def _as_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"{field} 必须是整数") from exc


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("Z", "")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise BadRequest(f"时间格式不正确：{value}") from exc


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _jsonable(value: Any) -> Any:
    """把快照转成可写入审计 JSON 字段的普通结构（时间转字符串）。"""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


# ==========================================================================
# 用户管理
# ==========================================================================
def user_to_dict(user: User) -> dict[str, Any]:
    role = user.role
    org = user.org
    phone = mask_phone(decrypt_field(user.phone_enc)) if user.phone_enc else ""
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
        "phone": phone,
        "is_active": bool(user.is_active),
        "last_login_at": user.last_login_at,
        "created_at": user.created_at,
    }


def _resolve_role(db: Session, role_id: Any = None, role_code: Any = None) -> Role:
    if role_id not in (None, ""):
        role = db.get(Role, _as_int(role_id, "role_id"))
        if role is None:
            raise RuleViolation("角色不存在")
        return role
    if role_code:
        role = db.execute(select(Role).where(Role.code == str(role_code))).scalar_one_or_none()
        if role is None:
            raise RuleViolation("角色不存在")
        return role
    role = db.execute(select(Role).where(Role.code == RoleCode.ELDER.value)).scalar_one_or_none()
    if role is None:
        raise RuleViolation("必须指定角色")
    return role


def _operator_is_admin(operator: User) -> bool:
    return bool(operator.role and operator.role.code == RoleCode.SUPER.value)


def _ensure_can_assign_role(operator: User, role: Role) -> None:
    if role.code == RoleCode.SUPER.value and not _operator_is_admin(operator):
        raise Forbidden("无权分配超级管理员角色")


def _ensure_org_in_scope(operator: User, org_id: Any) -> None:
    if org_id in (None, ""):
        return
    if _operator_is_admin(operator):
        return
    if operator.role and operator.role.code == RoleCode.STREET.value:
        return
    scope = effective_scope(operator)
    if scope == DataScope.STREET.value:
        return
    if operator.org_id is not None and _as_int(org_id, "org_id") == int(operator.org_id):
        return
    raise Forbidden("只能管理本机构/本网格范围内的用户")


def _validate_org(db: Session, org_id: Any) -> int | None:
    if org_id in (None, ""):
        return None
    value = _as_int(org_id, "org_id")
    if db.get(Org, value) is None:
        raise RuleViolation("组织不存在")
    return value


def _validate_scope(data_scope: Any) -> str:
    value = str(data_scope or "")
    if value not in _DATA_SCOPES:
        raise RuleViolation("数据范围不合法，可选：self/grid/org/street")
    return value


def _ensure_user_manageable(db: Session, operator: User, target: User) -> None:
    cond = scope_condition(User, operator, db)
    if cond is not None:
        visible = db.execute(select(User.id).where(User.id == target.id, cond)).scalar_one_or_none()
        if visible is None:
            raise Forbidden("无权管理该用户（数据范围限制）")
    if target.role and target.role.code == RoleCode.SUPER.value and not _operator_is_admin(operator):
        raise Forbidden("无权管理超级管理员")


def list_users(
    db: Session,
    operator: User,
    *,
    page: int = 1,
    size: int = 20,
    keyword: str | None = None,
    role: str | None = None,
    org_id: int | None = None,
    is_active: bool | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """分页查询用户，受数据范围约束。"""
    page = max(int(page or 1), 1)
    size = min(max(int(size or 20), 1), 200)
    stmt = apply_scope(select(User), User, operator, db)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(User.username.like(like), User.real_name.like(like)))
    if role:
        stmt = stmt.where(User.role_id.in_(select(Role.id).where(Role.code == str(role))))
    if org_id is not None:
        stmt = stmt.where(User.org_id == _as_int(org_id, "org_id"))
    if is_active is not None:
        stmt = stmt.where(User.is_active == bool(is_active))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        db.execute(stmt.order_by(User.id.asc()).offset((page - 1) * size).limit(size))
        .scalars()
        .unique()
        .all()
    )
    return [user_to_dict(item) for item in rows], total


def create_user(
    db: Session,
    operator: User,
    payload: dict[str, Any],
    request: Request | None = None,
) -> dict[str, Any]:
    """新建用户：用户名唯一、密码强度 >= 8、角色/组织/数据范围校验并审计。"""
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username:
        raise RuleViolation("用户名不能为空")
    if len(password) < 8:
        raise RuleViolation("密码长度至少 8 位")
    exists = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if exists is not None:
        raise Conflict("用户名已存在")

    role = _resolve_role(db, payload.get("role_id"), payload.get("role") or payload.get("role_code"))
    _ensure_can_assign_role(operator, role)
    org_id = _validate_org(db, payload.get("org_id"))
    _ensure_org_in_scope(operator, org_id)
    data_scope = _validate_scope(payload.get("data_scope") or role.data_scope)
    real_name = str(payload.get("real_name") or "").strip() or username
    phone = payload.get("phone")

    user = User(
        username=username,
        password_hash=hash_password(password),
        real_name=real_name,
        phone_enc=encrypt_field(str(phone)) if phone else None,
        role_id=role.id,
        org_id=org_id,
        data_scope=data_scope,
        is_active=bool(payload.get("is_active", True)),
    )
    db.add(user)
    db.flush()
    after = user_to_dict(user)
    write_audit(
        db,
        user=operator,
        action=AuditAction.CREATE,
        entity_type="user",
        entity_id=user.id,
        after=_jsonable(after),
        detail=f"新建用户：{username}",
        request=request,
    )
    db.commit()
    return after


def update_user(
    db: Session,
    operator: User,
    user_id: int,
    payload: dict[str, Any],
    request: Request | None = None,
) -> dict[str, Any]:
    """编辑用户：角色/组织/数据范围/启停用等。"""
    target = db.get(User, _as_int(user_id, "user_id"))
    if target is None:
        raise NotFound("用户不存在")
    _ensure_user_manageable(db, operator, target)
    before = user_to_dict(target)

    if payload.get("real_name"):
        target.real_name = str(payload["real_name"]).strip()
    if payload.get("role_id") is not None or payload.get("role") or payload.get("role_code"):
        role = _resolve_role(db, payload.get("role_id"), payload.get("role") or payload.get("role_code"))
        _ensure_can_assign_role(operator, role)
        target.role_id = role.id
    if "org_id" in payload and payload["org_id"] != "":
        org_id = _validate_org(db, payload["org_id"])
        _ensure_org_in_scope(operator, org_id)
        target.org_id = org_id
    if payload.get("data_scope"):
        target.data_scope = _validate_scope(payload["data_scope"])
    if payload.get("is_active") is not None:
        is_active = bool(payload["is_active"])
        if not is_active and target.id == operator.id:
            raise RuleViolation("不能停用自己的账号")
        target.is_active = is_active
    if "phone" in payload:
        phone = payload.get("phone")
        target.phone_enc = encrypt_field(str(phone)) if phone else None

    db.flush()
    after = user_to_dict(target)
    write_audit(
        db,
        user=operator,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=target.id,
        before=_jsonable(before),
        after=_jsonable(after),
        detail=f"编辑用户：{target.username}",
        request=request,
    )
    db.commit()
    return after


def reset_password(
    db: Session,
    operator: User,
    user_id: int,
    payload: dict[str, Any] | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """重置密码；未提供新密码时随机生成一个 10 位强密码返回给管理员。"""
    payload = payload or {}
    target = db.get(User, _as_int(user_id, "user_id"))
    if target is None:
        raise NotFound("用户不存在")
    _ensure_user_manageable(db, operator, target)

    password = payload.get("password") or payload.get("new_password")
    generated = False
    if not password:
        alphabet = string.ascii_letters + string.digits
        password = "Aa1" + "".join(secrets.choice(alphabet) for _ in range(7))
        generated = True
    password = str(password)
    if len(password) < 8:
        raise RuleViolation("密码长度至少 8 位")

    target.password_hash = hash_password(password)
    write_audit(
        db,
        user=operator,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=target.id,
        before={"password": "***"},
        after={"password": "***"},
        detail=f"重置密码：{target.username}",
        request=request,
    )
    db.commit()
    return {
        "user_id": target.id,
        "username": target.username,
        "password": password,
        "generated": generated,
    }


# ==========================================================================
# 角色与权限
# ==========================================================================
def list_roles(db: Session) -> list[dict[str, Any]]:
    roles = db.execute(select(Role).order_by(Role.id.asc())).scalars().unique().all()
    items: list[dict[str, Any]] = []
    for role in roles:
        codes = sorted(perm.code for perm in role.permissions)
        items.append(
            {
                "id": role.id,
                "code": role.code,
                "name": role.name,
                "description": role.description,
                "data_scope": role.data_scope,
                "permission_count": len(codes),
                "permissions": codes,
            }
        )
    return items


def list_permissions(db: Session) -> dict[str, Any]:
    rows = db.execute(select(Permission).order_by(Permission.id.asc())).scalars().unique().all()
    items = [
        {"id": perm.id, "code": perm.code, "name": perm.name, "module": perm.module}
        for perm in rows
    ]
    module_counts: dict[str, int] = {}
    for item in items:
        module_counts[item["module"]] = module_counts.get(item["module"], 0) + 1
    return {"items": items, "total": len(items), "module_counts": module_counts}


# ==========================================================================
# 组织
# ==========================================================================
def org_to_dict(org: Org) -> dict[str, Any]:
    return {
        "id": org.id,
        "code": org.code,
        "name": org.name,
        "level": org.level,
        "parent_id": org.parent_id,
        "sort": org.sort,
        "is_active": bool(org.is_active),
        "remark": org.remark,
        "children": [],
    }


def list_orgs(db: Session, level: str | None = None) -> list[dict[str, Any]]:
    """返回组织树；指定 level 时以该层级为根（保留其下级子树）。"""
    if level and level not in _ORG_LEVELS:
        raise RuleViolation("组织层级不合法，可选：street/community/grid")
    orgs = db.execute(select(Org).order_by(Org.sort.asc(), Org.id.asc())).scalars().all()
    nodes = {org.id: org_to_dict(org) for org in orgs}

    if level:
        root_ids = {org.id for org in orgs if org.level == level}
    else:
        root_ids = {org.id for org in orgs if org.parent_id is None or org.parent_id not in nodes}

    included: set[int] = set()
    ordered = sorted(orgs, key=lambda item: _ORG_LEVEL_RANK.get(item.level, 9))
    for org in ordered:
        if org.id in root_ids or (org.parent_id in included):
            included.add(org.id)

    roots: list[dict[str, Any]] = []
    for org in orgs:
        if org.id not in included:
            continue
        node = nodes[org.id]
        parent = nodes.get(org.parent_id) if org.parent_id else None
        if parent is not None and org.parent_id in included:
            parent["children"].append(node)
        else:
            roots.append(node)
    return roots


def create_org(
    db: Session,
    operator: User,
    payload: dict[str, Any],
    request: Request | None = None,
) -> dict[str, Any]:
    """新建组织，校验 street → community → grid 的层级关系。"""
    code = str(payload.get("code") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not code or not name:
        raise RuleViolation("组织编码与名称不能为空")
    exists = db.execute(select(Org).where(Org.code == code)).scalar_one_or_none()
    if exists is not None:
        raise Conflict("组织编码已存在")

    level = payload.get("level")
    parent_id = payload.get("parent_id")
    parent: Org | None = None
    if parent_id not in (None, ""):
        parent = db.get(Org, _as_int(parent_id, "parent_id"))
        if parent is None:
            raise RuleViolation("上级组织不存在")

    if not level:
        # 未显式指定层级时按上级推断
        if parent is None:
            level = OrgLevel.STREET.value
        elif parent.level == OrgLevel.STREET.value:
            level = OrgLevel.COMMUNITY.value
        else:
            level = OrgLevel.GRID.value
    level = str(level)
    if level not in _ORG_LEVELS:
        raise RuleViolation("组织层级不合法，可选：street/community/grid")

    if level == OrgLevel.STREET.value:
        if parent is not None:
            raise RuleViolation("街道为顶级组织，不能指定上级")
    elif level == OrgLevel.COMMUNITY.value:
        if parent is None or parent.level != OrgLevel.STREET.value:
            raise RuleViolation("社区的上级组织必须是街道")
    else:
        if parent is None or parent.level != OrgLevel.COMMUNITY.value:
            raise RuleViolation("网格的上级组织必须是社区")

    org = Org(
        code=code,
        name=name,
        level=level,
        parent_id=parent.id if parent else None,
        sort=int(payload.get("sort") or 0),
        is_active=bool(payload.get("is_active", True)),
        remark=payload.get("remark"),
    )
    db.add(org)
    db.flush()
    after = org_to_dict(org)
    write_audit(
        db,
        user=operator,
        action=AuditAction.CREATE,
        entity_type="org",
        entity_id=org.id,
        after=_jsonable(after),
        detail=f"新建组织：{code} {name}",
        request=request,
    )
    db.commit()
    return after


# ==========================================================================
# 审计日志
# ==========================================================================
def audit_to_dict(log: AuditLog) -> dict[str, Any]:
    return {
        "id": log.id,
        "user_id": log.user_id,
        "username": log.username,
        "action": log.action,
        "entity_type": log.entity_type,
        "entity_id": log.entity_id,
        "before": log.before_json,
        "after": log.after_json,
        "detail": log.detail,
        "ip": log.ip,
        "ua": log.ua,
        "prev_hash": log.prev_hash,
        "hash": log.hash,
        "created_at": log.created_at,
    }


def list_audit_logs(
    db: Session,
    *,
    page: int = 1,
    size: int = 20,
    action: str | None = None,
    entity_type: str | None = None,
    user_id: int | None = None,
    username: str | None = None,
    start: Any = None,
    end: Any = None,
) -> tuple[list[dict[str, Any]], int]:
    page = max(int(page or 1), 1)
    size = min(max(int(size or 20), 1), 500)
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == str(action))
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == str(entity_type))
    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == _as_int(user_id, "user_id"))
    if username:
        stmt = stmt.where(AuditLog.username.like(f"%{username.strip()}%"))
    start_dt = _parse_dt(start)
    if start_dt is not None:
        stmt = stmt.where(AuditLog.created_at >= start_dt)
    end_dt = _parse_dt(end)
    if end_dt is not None:
        # 只传日期时按“当天结束”处理
        if isinstance(end, str) and len(end.strip()) == 10:
            end_dt = end_dt + timedelta(days=1)
            stmt = stmt.where(AuditLog.created_at < end_dt)
        else:
            stmt = stmt.where(AuditLog.created_at <= end_dt)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        db.execute(stmt.order_by(AuditLog.id.desc()).offset((page - 1) * size).limit(size))
        .scalars()
        .all()
    )
    return [audit_to_dict(row) for row in rows], total


def verify_audit_chain(db: Session, limit: int = 5000) -> dict[str, Any]:
    result = verify_chain(db, limit=limit)
    result["checked_at"] = datetime.now()
    return result


# ==========================================================================
# 运行期配置
# ==========================================================================
DEFAULT_CONFIGS: dict[str, str] = {
    "dispatch_weights": settings.dispatch_weights,
    "dispatch_top_n": str(settings.dispatch_top_n),
    "order_daily_limit": str(settings.order_daily_limit),
    "service_radius_km": str(settings.service_radius_km),
    "nurse_rating_default": str(settings.nurse_rating_default),
    "accept_timeout_minutes": str(settings.accept_timeout_minutes),
    "cancel_free_hours": str(settings.cancel_free_hours),
    "checkin_distance_threshold_m": str(settings.checkin_distance_threshold_m),
    "duration_anomaly_minutes": str(settings.duration_anomaly_minutes),
    "alert_silence_hours": str(settings.alert_silence_hours),
    "alert_escalate_minutes": str(settings.alert_escalate_minutes),
    "diff_ratio_threshold": str(settings.diff_ratio_threshold),
}

#: 默认值的业务类型（用于把库里的字符串配置转成 int/float 等）
DEFAULT_CONFIG_TYPES: dict[str, Any] = {
    "dispatch_weights": settings.dispatch_weights,
    "dispatch_top_n": settings.dispatch_top_n,
    "order_daily_limit": settings.order_daily_limit,
    "service_radius_km": settings.service_radius_km,
    "nurse_rating_default": settings.nurse_rating_default,
    "accept_timeout_minutes": settings.accept_timeout_minutes,
    "cancel_free_hours": settings.cancel_free_hours,
    "checkin_distance_threshold_m": settings.checkin_distance_threshold_m,
    "duration_anomaly_minutes": settings.duration_anomaly_minutes,
    "alert_silence_hours": settings.alert_silence_hours,
    "alert_escalate_minutes": settings.alert_escalate_minutes,
    "diff_ratio_threshold": settings.diff_ratio_threshold,
}


def _config_default(key: str) -> Any:
    return DEFAULT_CONFIG_TYPES.get(key)


def _coerce_config(value: str, default: Any) -> Any:
    """按默认值类型把字符串配置转换成业务可用的 Python 值。"""
    if default is None:
        return value
    text = str(value).strip()
    if isinstance(default, bool):
        return text.lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(float(text))
        except ValueError:
            return value
    if isinstance(default, float):
        try:
            return float(text)
        except ValueError:
            return value
    if isinstance(default, (list, tuple)):
        if text.startswith("["):
            try:
                return json.loads(text)
            except ValueError:
                return value
        try:
            return [float(part) for part in text.replace("，", ",").split(",") if part.strip()]
        except ValueError:
            return value
    if isinstance(default, dict):
        try:
            return json.loads(text)
        except ValueError:
            return value
    return value


def _serialize_config(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def get_config(db: Session, key: str, default: Any = None) -> Any:
    """读取运行期配置（他模块调用入口）：优先 ConfigItem，其次默认值。"""
    item = db.execute(select(ConfigItem).where(ConfigItem.key == key)).scalar_one_or_none()
    fallback = default if default is not None else _config_default(key)
    if item is None:
        return fallback if fallback is not None else DEFAULT_CONFIGS.get(key)
    return _coerce_config(item.value, fallback)


def get_config_map(db: Session) -> dict[str, Any]:
    """返回“默认值 + 数据库覆盖值”的生效配置字典。"""
    result: dict[str, Any] = dict(DEFAULT_CONFIG_TYPES)
    for item in db.execute(select(ConfigItem)).scalars().all():
        result[item.key] = _coerce_config(item.value, _config_default(item.key))
    return result


def config_to_dict(item: ConfigItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "key": item.key,
        "value": item.value,
        "description": item.description,
        "updated_by": item.updated_by,
        "updated_at": item.updated_at,
    }


def list_configs(db: Session) -> dict[str, Any]:
    rows = db.execute(select(ConfigItem).order_by(ConfigItem.key.asc())).scalars().all()
    return {
        "items": [config_to_dict(row) for row in rows],
        "total": len(rows),
        "defaults": dict(DEFAULT_CONFIGS),
        "dispatch_weights": list(settings.weights_tuple),
        "effective": get_config_map(db),
    }


def _normalize_config_payload(payload: Any) -> list[tuple[str, Any]]:
    if not isinstance(payload, dict) or not payload:
        raise RuleViolation("配置内容不能为空")
    raw_items = payload.get("items")
    if isinstance(raw_items, list):
        pairs: list[tuple[str, Any]] = []
        for row in raw_items:
            if not isinstance(row, dict) or not row.get("key"):
                raise RuleViolation("配置项格式不正确（缺少 key）")
            pairs.append((str(row["key"]).strip(), row.get("value")))
        return pairs
    if payload.get("key"):
        return [(str(payload["key"]).strip(), payload.get("value"))]
    pairs = [
        (str(key).strip(), value)
        for key, value in payload.items()
        if key not in {"confirm", "note", "remark"} and value is not None
    ]
    if not pairs:
        raise RuleViolation("配置内容不能为空")
    return pairs


def _normalize_weights(value: Any) -> str:
    try:
        if isinstance(value, (list, tuple)):
            parts = [float(part) for part in value]
        else:
            parts = [float(part) for part in str(value).replace("，", ",").split(",") if part.strip()]
    except (TypeError, ValueError) as exc:
        raise RuleViolation("派单权重必须是 4 个数字，如 0.4,0.3,0.2,0.1") from exc
    if len(parts) != 4:
        raise RuleViolation("派单权重必须是 4 个数字，如 0.4,0.3,0.2,0.1")
    if sum(parts) <= 0:
        raise RuleViolation("派单权重之和必须大于 0")
    return ",".join(f"{part:g}" for part in parts)


def set_config(
    db: Session,
    operator: User,
    payload: Any,
    request: Request | None = None,
) -> dict[str, Any]:
    """更新/新增运行期配置（支持单项、批量 items、键值映射三种写法）。"""
    pairs = _normalize_config_payload(payload)
    changed: dict[str, Any] = {}
    for key, value in pairs:
        if not key:
            raise RuleViolation("配置 key 不能为空")
        if value is None:
            raise RuleViolation(f"配置 {key} 的值不能为空")
        if key == "dispatch_weights":
            value = _normalize_weights(value)
        text = _serialize_config(value)
        if len(text) > 512:
            raise RuleViolation(f"配置 {key} 的值过长（最多 512 字符）")
        item = db.execute(select(ConfigItem).where(ConfigItem.key == key)).scalar_one_or_none()
        if item is None:
            item = ConfigItem(key=key, value=text, description=f"运行期配置 {key}", updated_by=operator.id)
            db.add(item)
        else:
            item.value = text
            item.updated_by = operator.id
        changed[key] = value

    write_audit(
        db,
        user=operator,
        action=AuditAction.UPDATE,
        entity_type="config",
        after={"changed": {key: _serialize_config(value) for key, value in changed.items()}},
        detail=f"更新配置：{', '.join(changed)}",
        request=request,
    )
    db.commit()
    return {
        "changed": sorted(changed),
        "values": {
            key: get_config(db, key, _config_default(key) or DEFAULT_CONFIGS.get(key))
            for key in changed
        },
        "items": [config_to_dict(row) for row in db.execute(select(ConfigItem)).scalars().all()],
    }


# ==========================================================================
# 备份与恢复
# ==========================================================================
def _session_factory_for(db: Session) -> sessionmaker:
    """基于当前请求会话的绑定引擎，创建独立 Session（不依赖请求上下文）。"""
    bind = db.get_bind()
    engine = getattr(bind, "engine", bind)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def backup_to_dict(record: BackupRecord) -> dict[str, Any]:
    file_path = record.file_path or ""
    return {
        "id": record.id,
        "file_path": file_path,
        "file_name": Path(file_path).name if file_path else "",
        "file_size": record.file_size,
        "backup_type": record.backup_type,
        "status": record.status,
        "tables_snapshot": record.tables_snapshot,
        "tables_count": len(record.tables_snapshot or {}),
        "note": record.note,
        "created_by": record.created_by,
        "created_at": record.created_at,
        "exists": bool(file_path) and Path(file_path).is_file(),
    }


def create_backup(
    db: Session,
    operator: User | None = None,
    request: Request | None = None,
    backup_type: str = "manual",
) -> dict[str, Any]:
    """手动备份：调用独立 Session 的备份任务，并写 BACKUP 审计。"""
    db.commit()  # 释放连接，避免与备份会话争用同一 SQLite 连接
    result = run_daily_backup(
        backup_type=backup_type,
        created_by=getattr(operator, "id", None),
        session_factory=_session_factory_for(db),
    )
    if not result.get("ok"):
        raise RuleViolation(f"备份失败：{result.get('message') or '未知错误'}")
    try:
        write_audit(
            db,
            user=operator,
            action=AuditAction.BACKUP,
            entity_type="backup",
            entity_id=result.get("backup_id"),
            after={
                "file": result.get("file"),
                "file_size": result.get("file_size"),
                "tables": result.get("tables"),
            },
            detail=f"手动备份：{result.get('file_name')}",
            request=request,
        )
        db.commit()
    except Exception:  # noqa: BLE001 - 审计失败不影响备份结果
        db.rollback()
        logger.exception("备份审计写入失败")
    return result


def list_backups(
    db: Session,
    *,
    page: int = 1,
    size: int = 20,
    backup_type: str | None = None,
    status: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    page = max(int(page or 1), 1)
    size = min(max(int(size or 20), 1), 200)
    stmt = select(BackupRecord)
    if backup_type:
        stmt = stmt.where(BackupRecord.backup_type == str(backup_type))
    if status:
        stmt = stmt.where(BackupRecord.status == str(status))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        db.execute(
            stmt.order_by(BackupRecord.id.desc()).offset((page - 1) * size).limit(size)
        )
        .scalars()
        .all()
    )
    return [backup_to_dict(row) for row in rows], total


def _check_sqlite_integrity(path: Path) -> dict[str, Any]:
    """恢复前校验：文件可读、PRAGMA integrity_check 通过、含业务表。"""
    connection = None
    try:
        connection = sqlite3.connect(str(path))
        row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity_ok = bool(row and str(row[0]).lower() == "ok")
        tables = {
            item[0]
            for item in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    except sqlite3.Error as exc:
        raise RuleViolation(f"备份文件无法读取，可能已损坏：{exc}") from exc
    finally:
        if connection is not None:
            connection.close()
    if not integrity_ok:
        raise RuleViolation("备份文件完整性校验未通过（PRAGMA integrity_check）")
    if not tables & {"user", "org", "role", "permission", "elder"}:
        raise RuleViolation("备份文件不是有效的业务数据库（缺少业务表）")
    return {"integrity": "ok", "file_size": path.stat().st_size, "tables": len(tables)}


def _restore_sqlite(db: Session, backup_file: Path) -> None:
    """用 SQLite 在线备份 API 把备份文件内容写回当前数据库连接。"""
    bind = db.get_bind()
    engine = getattr(bind, "engine", bind)
    if engine.dialect.name != "sqlite":
        raise RuleViolation("当前数据库类型不支持文件级恢复（仅支持 SQLite）")
    db.commit()
    raw = engine.raw_connection()
    source = None
    try:
        target = raw.driver_connection
        source = sqlite3.connect(str(backup_file))
        source.backup(target)
        target.commit()
    except sqlite3.Error as exc:
        raise RuleViolation(f"恢复失败：{exc}") from exc
    finally:
        if source is not None:
            source.close()
        raw.close()


def _restore_uploads(backup_file: Path) -> int:
    """如存在同名附件 zip，则解包回上传目录（仅覆盖同名文件，不做删除）。"""
    zip_path = backup_file.with_suffix(".zip")
    if not zip_path.is_file():
        return 0
    upload_dir = settings.upload_path
    upload_dir.mkdir(parents=True, exist_ok=True)
    root = upload_dir.resolve()
    restored = 0
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                target = (upload_dir / info.filename).resolve()
                if root not in target.parents and target != root:
                    logger.warning("跳过越界附件：%s", info.filename)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as dest:
                    dest.write(source.read())
                restored += 1
    except (zipfile.BadZipFile, OSError):
        logger.exception("附件包恢复失败（不影响数据库恢复）：%s", zip_path)
        return 0
    return restored


def restore_backup(
    db: Session,
    *,
    backup_id: int | None = None,
    file_path: str | None = None,
    operator: User | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """按备份时间点恢复数据库：完整性校验 → 记录恢复前行数 → 恢复 → 核对报告 → 审计。"""
    record: BackupRecord | None = None
    if backup_id not in (None, ""):
        record = db.get(BackupRecord, _as_int(backup_id, "backup_id"))
        if record is None:
            raise NotFound("备份记录不存在")
        path = Path(record.file_path or "")
    elif file_path:
        candidate = Path(str(file_path))
        path = candidate if candidate.is_absolute() else settings.abs_path(str(file_path))
    else:
        raise RuleViolation("请指定要恢复的备份（backup_id 或 file_path）")

    if not path.is_file():
        raise RuleViolation("备份文件不存在或已被清理")

    live_engine = getattr(db.get_bind(), "engine", db.get_bind())
    live_url = getattr(getattr(live_engine, "url", None), "database", "") or ""
    if live_url and live_url == str(path):
        raise RuleViolation("不能从当前正在使用的数据库文件恢复")

    integrity = _check_sqlite_integrity(path)

    record_snapshot = backup_to_dict(record) if record is not None else None
    operator_id = getattr(operator, "id", None)
    operator_name = getattr(operator, "username", None)

    before = table_counts(db)
    db.commit()
    _restore_sqlite(db, path)
    db.expunge_all()
    after = table_counts(db)
    uploads_restored = _restore_uploads(path)

    # 备份文件生成于备份记录写入之前，恢复后该记录可能“回到过去”，这里回填，保留备份目录可追溯
    if record_snapshot is not None and db.get(BackupRecord, record_snapshot["id"]) is None:
        db.add(
            BackupRecord(
                file_path=record_snapshot["file_path"],
                file_size=record_snapshot["file_size"],
                backup_type=record_snapshot["backup_type"] or "manual",
                status="success",
                tables_snapshot=record_snapshot["tables_snapshot"],
                note="恢复后回填的备份记录",
                created_by=record_snapshot["created_by"],
            )
        )
        db.flush()

    audit_user: Any = db.get(User, operator_id) if operator_id is not None else None
    if audit_user is None and operator_id is not None:
        audit_user = SimpleNamespace(id=operator_id, username=operator_name)
    write_audit(
        db,
        user=audit_user,
        action=AuditAction.RESTORE,
        entity_type="backup",
        entity_id=record_snapshot["id"] if record_snapshot else None,
        before=before,
        after=after,
        detail=f"从备份恢复：{path.name}",
        request=request,
    )
    db.commit()

    # 系统表（审计/备份目录）本身会随恢复动作变化，不计入业务数据差异
    system_tables = {"audit_log", "backup_record"}
    diff = {
        name: {
            "before": before.get(name, 0),
            "after": after.get(name, 0),
            "delta": after.get(name, 0) - before.get(name, 0),
        }
        for name in sorted(set(before) | set(after))
        if name not in system_tables and before.get(name, 0) != after.get(name, 0)
    }
    system_diff = {
        name: {"before": before.get(name, 0), "after": after.get(name, 0)}
        for name in sorted(system_tables)
        if before.get(name, 0) != after.get(name, 0)
    }
    return {
        "backup_id": record_snapshot["id"] if record_snapshot else None,
        "file_path": str(path),
        "file_name": path.name,
        "integrity": integrity,
        "before": before,
        "after": after,
        "diff": diff,
        "changed_tables": sorted(diff),
        "system_tables": system_diff,
        "unchanged": not diff,
        "uploads_restored": uploads_restored,
        "restored_at": datetime.now(),
    }
