"""老人档案服务层（A2）。

职责：
- 档案 CRUD：敏感字段 AES-GCM 加密 + HMAC 摘要检索列 + 列表脱敏；
- 变更留痕：ArchiveChangeLog（before/after/changed_fields）+ 审计哈希链；
- 二次授权：AccessGrant（10 分钟有效、本人、字段覆盖校验、used_at 留痕）；
- 标签 / 慢病 / 用药 / 能力评估（评估后同步 ability_level）；
- 批量导入（逐条提交，单条失败不影响其他）与 CSV 导出（UTF-8 BOM、脱敏）。
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.deps import apply_scope, ensure_elder_visible
from app.core.enums import (
    ABILITY_NAMES,
    DIFFICULTY_NAMES,
    ELDER_TAG_NAMES,
    AuditAction,
)
from app.core.exceptions import BizError, Conflict, Forbidden, NotFound, RuleViolation
from app.core.security import (
    decrypt_field,
    encrypt_field,
    field_digest,
    mask_address,
    mask_id_card,
    mask_name,
    mask_phone,
)
from app.models import (
    AbilityAssessment,
    AccessGrant,
    ArchiveChangeLog,
    Elder,
    ElderDisease,
    ElderMedication,
    ElderTag,
    Org,
    User,
)
from app.schemas.elder import ElderCreate, ElderUpdate, GrantCreate

# ---------------- 常量 ----------------
GRANT_TTL_MINUTES = 10                       # 二次授权有效期
SENSITIVE_ENC_ATTR: dict[str, str] = {       # 明文字段 -> ORM 密文列
    "id_card": "id_card_enc",
    "phone": "phone_enc",
    "address": "address_enc",
    "emergency_phone": "emergency_phone_enc",
}
# 变更日志快照覆盖的字段（顺序即前端展示顺序）
TRACKED_FIELDS: tuple[str, ...] = (
    "code",
    "name",
    "gender",
    "birth_date",
    "id_card",
    "phone",
    "address",
    "community_org_id",
    "grid_org_id",
    "ability_level",
    "difficulty_type",
    "insurance_type",
    "ltc_type",
    "emergency_name",
    "emergency_relation",
    "emergency_phone",
    "allergy",
    "notes",
    "lat",
    "lng",
)

GENDER_TEXT = {"male": "男", "female": "女", "unknown": "未知"}
ABILITY_TEXT = {item.value: name for item, name in ABILITY_NAMES.items()}
DIFFICULTY_TEXT = {item.value: name for item, name in DIFFICULTY_NAMES.items()}
TAG_TEXT = {item.value: name for item, name in ELDER_TAG_NAMES.items()}


# ---------------- 通用小工具 ----------------
def _iso(value: Any) -> Any:
    """把 date/datetime 转成 JSON 可序列化的 ISO 字符串。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _plain(elder: Elder, field: str) -> str | None:
    return decrypt_field(getattr(elder, SENSITIVE_ENC_ATTR[field]))


def _write_sensitive(elder: Elder, field: str, raw: str | None) -> None:
    """加密写入敏感字段，并维护可检索摘要 / 快速展示脱敏列。"""
    text = _clean_str(raw)
    if field == "id_card":
        elder.id_card_enc = encrypt_field(text)
        elder.id_card_hash = field_digest(text)
    elif field == "phone":
        elder.phone_enc = encrypt_field(text)
        elder.phone_hash = field_digest(text)
    elif field == "address":
        elder.address_enc = encrypt_field(text)
        elder.address_masked = mask_address(text) if text else None
    elif field == "emergency_phone":
        elder.emergency_phone_enc = encrypt_field(text)
    else:  # pragma: no cover - 防御式
        raise RuleViolation(f"不支持的敏感字段：{field}")


def _snapshot(elder: Elder) -> dict[str, Any]:
    """变更日志快照：敏感字段一律脱敏，绝不明文/密文落日志。"""
    return {
        "code": elder.code,
        "name": elder.name,
        "gender": elder.gender,
        "birth_date": _iso(elder.birth_date),
        "id_card": mask_id_card(_plain(elder, "id_card")),
        "phone": mask_phone(_plain(elder, "phone")),
        "address": elder.address_masked or mask_address(_plain(elder, "address")),
        "community_org_id": elder.community_org_id,
        "grid_org_id": elder.grid_org_id,
        "ability_level": elder.ability_level,
        "difficulty_type": elder.difficulty_type,
        "insurance_type": elder.insurance_type,
        "ltc_type": elder.ltc_type,
        "emergency_name": elder.emergency_name,
        "emergency_relation": elder.emergency_relation,
        "emergency_phone": mask_phone(_plain(elder, "emergency_phone")),
        "allergy": elder.allergy,
        "notes": elder.notes,
        "lat": elder.lat,
        "lng": elder.lng,
    }


def to_masked_dict(elder: Elder, tags: list[str] | None = None) -> dict[str, Any]:
    """对外脱敏视图：绝不返回 *_enc 密文。"""
    id_card_masked = mask_id_card(_plain(elder, "id_card"))
    phone_masked = mask_phone(_plain(elder, "phone"))
    address_masked = elder.address_masked or mask_address(_plain(elder, "address"))
    return {
        "id": elder.id,
        "code": elder.code,
        "name": mask_name(elder.name),
        "name_masked": mask_name(elder.name),
        "gender": elder.gender,
        "birth_date": _iso(elder.birth_date),
        "id_card": id_card_masked,
        "id_card_masked": id_card_masked,
        "phone": phone_masked,
        "phone_masked": phone_masked,
        "address": address_masked,
        "address_masked": address_masked,
        "community_org_id": elder.community_org_id,
        "grid_org_id": elder.grid_org_id,
        "ability_level": elder.ability_level,
        "ability_level_name": ABILITY_TEXT.get(elder.ability_level, elder.ability_level),
        "difficulty_type": elder.difficulty_type,
        "difficulty_type_name": DIFFICULTY_TEXT.get(elder.difficulty_type, elder.difficulty_type),
        "insurance_type": elder.insurance_type,
        "ltc_type": elder.ltc_type,
        "emergency_name": elder.emergency_name,
        "emergency_relation": elder.emergency_relation,
        "emergency_phone": mask_phone(_plain(elder, "emergency_phone")),
        "allergy": elder.allergy,
        "notes": elder.notes,
        "lat": elder.lat,
        "lng": elder.lng,
        "tags": list(tags or []),
        "created_at": _iso(elder.created_at),
        "updated_at": _iso(elder.updated_at),
    }


def _tags_map(db: Session, elder_ids: list[int]) -> dict[int, list[str]]:
    if not elder_ids:
        return {}
    rows = db.execute(
        select(ElderTag.elder_id, ElderTag.tag)
        .where(ElderTag.elder_id.in_(elder_ids))
        .order_by(ElderTag.id.asc())
    ).all()
    mapping: dict[int, list[str]] = {}
    for elder_id, tag in rows:
        mapping.setdefault(elder_id, []).append(tag)
    return mapping


# ---------------- 查询 ----------------
def get_elder(db: Session, user: User, elder_id: int) -> Elder:
    """取未删除档案并做数据范围校验：不存在 404，越权 403。"""
    elder = db.execute(
        select(Elder).where(Elder.id == elder_id, Elder.is_deleted == 0)
    ).scalar_one_or_none()
    if elder is None:
        raise NotFound("老人档案不存在或已删除")
    ensure_elder_visible(db, user, elder_id)
    return elder


def _filtered_query(
    db: Session,
    user: User,
    *,
    keyword: str | None = None,
    ability_level: str | None = None,
    tag: str | None = None,
    grid_id: int | None = None,
    community_id: int | None = None,
):
    stmt = apply_scope(select(Elder).where(Elder.is_deleted == 0), Elder, user, db)
    if keyword and keyword.strip():
        kw = keyword.strip()
        like = f"%{kw}%"
        digests = {field_digest(kw)}
        if kw.upper() != kw:  # 身份证末位 X 大小写不敏感
            digests.add(field_digest(kw.upper()))
        digest_list = list(digests)
        stmt = stmt.where(
            or_(
                Elder.name.like(like),
                Elder.code.like(like),
                Elder.phone_hash.in_(digest_list),
                Elder.id_card_hash.in_(digest_list),
            )
        )
    if ability_level:
        stmt = stmt.where(Elder.ability_level == ability_level.strip().lower())
    if tag:
        stmt = stmt.where(
            Elder.id.in_(select(ElderTag.elder_id).where(ElderTag.tag == tag.strip()))
        )
    if grid_id:
        stmt = stmt.where(Elder.grid_org_id == grid_id)
    if community_id:
        stmt = stmt.where(Elder.community_org_id == community_id)
    return stmt


def list_elders(
    db: Session,
    user: User,
    *,
    keyword: str | None = None,
    ability_level: str | None = None,
    tag: str | None = None,
    grid_id: int | None = None,
    community_id: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    stmt = _filtered_query(
        db,
        user,
        keyword=keyword,
        ability_level=ability_level,
        tag=tag,
        grid_id=grid_id,
        community_id=community_id,
    )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    page = max(page, 1)
    size = max(size, 1)
    rows = db.execute(
        stmt.order_by(Elder.id.desc()).offset((page - 1) * size).limit(size)
    ).scalars().all()
    tags = _tags_map(db, [e.id for e in rows])
    return [to_masked_dict(e, tags.get(e.id, [])) for e in rows], int(total)


def detail_view(db: Session, user: User, elder_id: int) -> dict[str, Any]:
    elder = get_elder(db, user, elder_id)
    return to_masked_dict(elder, _tags_map(db, [elder.id]).get(elder.id, []))


# ---------------- 新增 / 编辑 / 删除 ----------------
def _check_orgs(db: Session, community_org_id: int | None, grid_org_id: int | None) -> None:
    if community_org_id is not None and db.get(Org, community_org_id) is None:
        raise RuleViolation(f"所属社区不存在：{community_org_id}")
    if grid_org_id is not None and db.get(Org, grid_org_id) is None:
        raise RuleViolation(f"所属网格不存在：{grid_org_id}")


def _ensure_code_available(db: Session, code: str) -> None:
    exists = db.execute(select(Elder.id).where(Elder.code == code)).first()
    if exists is not None:
        raise Conflict(f"老人编码已存在：{code}")


def _upsert_tags(db: Session, elder_id: int, tags: list[str]) -> list[str]:
    if not tags:
        return []
    existing = set(
        db.execute(
            select(ElderTag.tag).where(
                ElderTag.elder_id == elder_id, ElderTag.tag.in_(tags)
            )
        ).scalars().all()
    )
    for tag in tags:
        if tag not in existing:
            db.add(ElderTag(elder_id=elder_id, tag=tag))
            existing.add(tag)
    db.flush()
    return sorted(existing)


def create_elder(
    db: Session,
    user: User,
    payload: ElderCreate,
    request: Request | None = None,
    *,
    commit: bool = True,
) -> Elder:
    _ensure_code_available(db, payload.code)
    _check_orgs(db, payload.community_org_id, payload.grid_org_id)

    elder = Elder(
        code=payload.code,
        name=payload.name,
        gender=payload.gender or "unknown",
        birth_date=payload.birth_date,
        community_org_id=payload.community_org_id,
        grid_org_id=payload.grid_org_id,
        ability_level=payload.ability_level or "intact",
        difficulty_type=payload.difficulty_type or "normal",
        insurance_type=payload.insurance_type,
        ltc_type=payload.ltc_type,
        emergency_name=payload.emergency_name,
        emergency_relation=payload.emergency_relation,
        allergy=payload.allergy,
        notes=payload.notes,
        lat=payload.lat,
        lng=payload.lng,
        created_by=getattr(user, "id", None),
    )
    for field in SENSITIVE_ENC_ATTR:
        _write_sensitive(elder, field, getattr(payload, field, None))
    db.add(elder)
    db.flush()
    _upsert_tags(db, elder.id, payload.tags)

    after = _snapshot(elder)
    db.add(
        ArchiveChangeLog(
            elder_id=elder.id,
            changed_by=getattr(user, "id", None),
            changed_fields=list(TRACKED_FIELDS),
            before_json={},
            after_json=after,
            reason="新增档案",
        )
    )
    write_audit(
        db,
        user=user,
        action=AuditAction.CREATE.value,
        entity_type="elder",
        entity_id=elder.id,
        before=None,
        after=after,
        detail=f"新增老人档案 {elder.code}",
        request=request,
    )
    if commit:
        db.commit()
    return elder


def update_elder(
    db: Session,
    user: User,
    elder_id: int,
    payload: ElderUpdate,
    request: Request | None = None,
    *,
    commit: bool = True,
) -> Elder:
    elder = get_elder(db, user, elder_id)
    data = payload.model_dump(exclude_unset=True)
    if "code" in data and data["code"] is not None and data["code"].strip() != elder.code:
        raise RuleViolation("老人编码不允许修改")
    if "community_org_id" in data and data["community_org_id"] is None:
        raise RuleViolation("所属社区不能为空")
    if "community_org_id" in data or "grid_org_id" in data:
        _check_orgs(
            db,
            data.get("community_org_id", elder.community_org_id),
            data.get("grid_org_id", elder.grid_org_id),
        )

    before = _snapshot(elder)
    changed: list[str] = []
    skip = {"code", "tags", "reason"}
    for key, new_value in data.items():
        if key in skip:
            continue
        if key in SENSITIVE_ENC_ATTR:
            raw_new = _clean_str(new_value)
            raw_old = _clean_str(_plain(elder, key))
            if raw_new == raw_old:
                continue
            _write_sensitive(elder, key, raw_new)
            changed.append(key)
            continue
        if getattr(elder, key) == new_value:
            continue
        setattr(elder, key, new_value)
        changed.append(key)

    if not changed:
        if commit:
            db.commit()
        return elder

    after = _snapshot(elder)
    db.add(
        ArchiveChangeLog(
            elder_id=elder.id,
            changed_by=getattr(user, "id", None),
            changed_fields=changed,
            before_json=before,
            after_json=after,
            reason=payload.reason,
        )
    )
    write_audit(
        db,
        user=user,
        action=AuditAction.UPDATE.value,
        entity_type="elder",
        entity_id=elder.id,
        before=before,
        after=after,
        detail=f"编辑老人档案 {elder.code}，变更字段：{','.join(changed)}",
        request=request,
    )
    if commit:
        db.commit()
    return elder


def delete_elder(
    db: Session, user: User, elder_id: int, request: Request | None = None
) -> Elder:
    elder = get_elder(db, user, elder_id)
    before = _snapshot(elder)
    elder.is_deleted = 1
    elder.deleted_at = datetime.now()
    after = {"is_deleted": 1, "deleted_at": _iso(elder.deleted_at)}
    write_audit(
        db,
        user=user,
        action=AuditAction.DELETE.value,
        entity_type="elder",
        entity_id=elder.id,
        before=before,
        after=after,
        detail=f"逻辑删除老人档案 {elder.code}",
        request=request,
    )
    db.commit()
    return elder


# ---------------- 变更日志 ----------------
def list_changes(db: Session, user: User, elder_id: int) -> list[dict[str, Any]]:
    get_elder(db, user, elder_id)
    rows = db.execute(
        select(ArchiveChangeLog)
        .where(ArchiveChangeLog.elder_id == elder_id)
        .order_by(ArchiveChangeLog.created_at.desc(), ArchiveChangeLog.id.desc())
    ).scalars().all()
    return [
        {
            "id": row.id,
            "elder_id": row.elder_id,
            "changed_by": row.changed_by,
            "changed_fields": list(row.changed_fields or []),
            "before": row.before_json or {},
            "after": row.after_json or {},
            "reason": row.reason,
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


# ---------------- 二次授权 ----------------
def create_grant(
    db: Session,
    user: User,
    elder_id: int,
    payload: GrantCreate,
    request: Request | None = None,
) -> dict[str, Any]:
    get_elder(db, user, elder_id)  # 404 / 数据范围 403
    expires_at = datetime.now() + timedelta(minutes=GRANT_TTL_MINUTES)
    grant = AccessGrant(
        user_id=user.id,
        entity_type="elder",
        entity_id=elder_id,
        fields=list(payload.fields),
        reason=payload.reason,
        expires_at=expires_at,
    )
    db.add(grant)
    db.flush()
    after = {
        "grant_id": grant.id,
        "elder_id": elder_id,
        "fields": grant.fields,
        "reason": grant.reason,
        "expires_at": _iso(expires_at),
    }
    write_audit(
        db,
        user=user,
        action=AuditAction.AUTHORIZE.value,
        entity_type="elder",
        entity_id=elder_id,
        after=after,
        detail=f"申请二次授权：{','.join(grant.fields)}；理由：{grant.reason}",
        request=request,
    )
    db.commit()
    return {
        "grant_id": grant.id,
        "elder_id": elder_id,
        "fields": grant.fields,
        "reason": grant.reason,
        "expires_at": _iso(expires_at),
        "expires_in_seconds": GRANT_TTL_MINUTES * 60,
    }


def read_sensitive(
    db: Session,
    user: User,
    elder_id: int,
    grant_id: int | None,
    fields: list[str] | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    elder = get_elder(db, user, elder_id)
    if not grant_id:
        raise Forbidden("缺少二次授权，禁止查看敏感字段")
    grant = db.get(AccessGrant, grant_id)
    if grant is None:
        raise Forbidden("二次授权不存在或已失效")
    if grant.user_id != getattr(user, "id", None):
        raise Forbidden("二次授权不属于当前用户")
    if grant.entity_type != "elder" or grant.entity_id != elder_id:
        raise Forbidden("二次授权与目标档案不匹配")
    if grant.expires_at is None or grant.expires_at < datetime.now():
        raise Forbidden("二次授权已过期，请重新申请")

    granted = [f for f in (grant.fields or []) if f in SENSITIVE_ENC_ATTR]
    requested = list(fields or granted)
    unknown = [f for f in requested if f not in SENSITIVE_ENC_ATTR]
    if unknown:
        raise Forbidden(f"不支持的敏感字段：{','.join(unknown)}")
    if not set(requested).issubset(set(granted)):
        raise Forbidden(f"二次授权字段范围不足，已授权：{granted}")

    plain = {field: _plain(elder, field) for field in requested}
    grant.used_at = datetime.now()
    db.flush()
    write_audit(
        db,
        user=user,
        action=AuditAction.AUTHORIZE.value,
        entity_type="elder",
        entity_id=elder_id,
        detail=f"凭授权 {grant.id} 查看敏感字段：{','.join(requested)}",
        request=request,
    )
    db.commit()
    return {
        "grant_id": grant.id,
        "used_at": _iso(grant.used_at),
        "fields": plain,
        **plain,
    }


# ---------------- 标签 / 慢病 / 用药 / 评估 ----------------
def list_tags(db: Session, user: User, elder_id: int) -> list[str]:
    get_elder(db, user, elder_id)
    return list(
        db.execute(
            select(ElderTag.tag)
            .where(ElderTag.elder_id == elder_id)
            .order_by(ElderTag.id.asc())
        ).scalars().all()
    )


def add_tags(
    db: Session,
    user: User,
    elder_id: int,
    tags: list[str],
    request: Request | None = None,
) -> list[str]:
    get_elder(db, user, elder_id)
    before = list_tags(db, user, elder_id)
    merged = _upsert_tags(db, elder_id, tags)
    added = [t for t in merged if t not in before]
    if added:
        write_audit(
            db,
            user=user,
            action=AuditAction.UPDATE.value,
            entity_type="elder",
            entity_id=elder_id,
            before={"tags": before},
            after={"tags": merged},
            detail=f"新增老人标签：{','.join(added)}",
            request=request,
        )
    db.commit()
    return list_tags(db, user, elder_id)


def _disease_dict(row: ElderDisease) -> dict[str, Any]:
    return {
        "id": row.id,
        "elder_id": row.elder_id,
        "name": row.name,
        "diagnosed_at": _iso(row.diagnosed_at),
        "note": row.note,
    }


def list_diseases(db: Session, user: User, elder_id: int) -> list[dict[str, Any]]:
    get_elder(db, user, elder_id)
    rows = db.execute(
        select(ElderDisease)
        .where(ElderDisease.elder_id == elder_id)
        .order_by(ElderDisease.id.desc())
    ).scalars().all()
    return [_disease_dict(row) for row in rows]


def add_disease(
    db: Session, user: User, elder_id: int, payload: Any
) -> dict[str, Any]:
    get_elder(db, user, elder_id)
    row = ElderDisease(
        elder_id=elder_id,
        name=payload.name,
        diagnosed_at=payload.diagnosed_at,
        note=payload.note,
    )
    db.add(row)
    db.commit()
    return _disease_dict(row)


def _medication_dict(row: ElderMedication) -> dict[str, Any]:
    return {
        "id": row.id,
        "elder_id": row.elder_id,
        "name": row.name,
        "dosage": row.dosage,
        "frequency": row.frequency,
    }


def list_medications(db: Session, user: User, elder_id: int) -> list[dict[str, Any]]:
    get_elder(db, user, elder_id)
    rows = db.execute(
        select(ElderMedication)
        .where(ElderMedication.elder_id == elder_id)
        .order_by(ElderMedication.id.desc())
    ).scalars().all()
    return [_medication_dict(row) for row in rows]


def add_medication(
    db: Session, user: User, elder_id: int, payload: Any
) -> dict[str, Any]:
    get_elder(db, user, elder_id)
    row = ElderMedication(
        elder_id=elder_id,
        name=payload.name,
        dosage=payload.dosage,
        frequency=payload.frequency,
    )
    db.add(row)
    db.commit()
    return _medication_dict(row)


def _assessment_dict(row: AbilityAssessment) -> dict[str, Any]:
    return {
        "id": row.id,
        "elder_id": row.elder_id,
        "level": row.level,
        "level_name": ABILITY_TEXT.get(row.level, row.level),
        "score": row.score,
        "assessed_at": _iso(row.assessed_at),
        "assessor_id": row.assessor_id,
        "basis": row.basis,
        "created_at": _iso(row.created_at),
    }


def list_assessments(db: Session, user: User, elder_id: int) -> list[dict[str, Any]]:
    get_elder(db, user, elder_id)
    rows = db.execute(
        select(AbilityAssessment)
        .where(AbilityAssessment.elder_id == elder_id)
        .order_by(AbilityAssessment.assessed_at.desc(), AbilityAssessment.id.desc())
    ).scalars().all()
    return [_assessment_dict(row) for row in rows]


def add_assessment(
    db: Session,
    user: User,
    elder_id: int,
    payload: Any,
    request: Request | None = None,
) -> dict[str, Any]:
    elder = get_elder(db, user, elder_id)
    before_level = elder.ability_level
    assessed_at = payload.assessed_at or datetime.now()
    row = AbilityAssessment(
        elder_id=elder_id,
        level=payload.level,
        score=payload.score,
        assessed_at=assessed_at,
        assessor_id=getattr(user, "id", None),
        basis=payload.basis,
    )
    db.add(row)
    db.flush()
    elder.ability_level = payload.level
    write_audit(
        db,
        user=user,
        action=AuditAction.UPDATE.value,
        entity_type="elder",
        entity_id=elder_id,
        before={"ability_level": before_level},
        after={
            "ability_level": payload.level,
            "assessment_id": row.id,
            "score": payload.score,
            "assessed_at": _iso(assessed_at),
        },
        detail="新增能力评估并同步老人能力等级",
        request=request,
    )
    db.commit()
    return _assessment_dict(row)


# ---------------- 批量导入 ----------------
def _validation_reason(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()))
        message = str(error.get("msg", "校验失败")).removeprefix("Value error, ")
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts)[:255]


def import_elders(
    db: Session,
    user: User,
    items: list[dict[str, Any]],
    request: Request | None = None,
) -> dict[str, Any]:
    """逐条导入：每条单独提交，单条失败只记录原因，不回滚已成功数据。"""
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            failed.append({"index": index, "reason": "数据格式错误：应为 JSON 对象"})
            continue
        code = _clean_str(item.get("code"))
        if not code:
            failed.append({"index": index, "reason": "缺少必填字段：code"})
            continue
        try:
            payload = ElderCreate(**item)
        except ValidationError as exc:
            failed.append(
                {"index": index, "code": code, "reason": _validation_reason(exc)}
            )
            continue
        try:
            elder = create_elder(db, user, payload, request=request)
        except BizError as exc:
            db.rollback()
            failed.append({"index": index, "code": code, "reason": exc.message})
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响整体导入
            db.rollback()
            failed.append({"index": index, "code": code, "reason": f"导入失败：{exc}"})
        else:
            created.append({"index": index, "id": elder.id, "code": elder.code})
    return {"success": len(created), "failed": failed, "created": created}


# ---------------- 导出 ----------------
EXPORT_HEADERS = [
    "编号",
    "姓名",
    "性别",
    "出生日期",
    "能力等级",
    "困难类型",
    "社区ID",
    "网格ID",
    "身份证号（脱敏）",
    "手机号（脱敏）",
    "住址（脱敏）",
    "标签",
    "创建时间",
]


def export_csv(
    db: Session,
    user: User,
    *,
    keyword: str | None = None,
    ability_level: str | None = None,
    tag: str | None = None,
    grid_id: int | None = None,
    community_id: int | None = None,
    request: Request | None = None,
) -> tuple[bytes, str]:
    stmt = _filtered_query(
        db,
        user,
        keyword=keyword,
        ability_level=ability_level,
        tag=tag,
        grid_id=grid_id,
        community_id=community_id,
    )
    rows = db.execute(stmt.order_by(Elder.code.asc())).scalars().all()
    tags = _tags_map(db, [elder.id for elder in rows])

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_HEADERS)
    for elder in rows:
        writer.writerow(
            [
                elder.code,
                mask_name(elder.name),
                GENDER_TEXT.get(elder.gender, elder.gender),
                _iso(elder.birth_date) or "",
                ABILITY_TEXT.get(elder.ability_level, elder.ability_level),
                DIFFICULTY_TEXT.get(elder.difficulty_type, elder.difficulty_type),
                elder.community_org_id,
                elder.grid_org_id or "",
                mask_id_card(_plain(elder, "id_card")),
                mask_phone(_plain(elder, "phone")),
                elder.address_masked or mask_address(_plain(elder, "address")),
                "、".join(TAG_TEXT.get(t, t) for t in tags.get(elder.id, [])),
                _iso(elder.created_at) or "",
            ]
        )

    content = b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")
    write_audit(
        db,
        user=user,
        action=AuditAction.EXPORT.value,
        entity_type="elder",
        after={
            "count": len(rows),
            "filters": {
                "keyword": keyword,
                "ability_level": ability_level,
                "tag": tag,
                "grid_id": grid_id,
                "community_id": community_id,
            },
        },
        detail=f"导出老人档案 {len(rows)} 条（脱敏）",
        request=request,
    )
    db.commit()
    return content, f"elders_{datetime.now():%Y%m%d%H%M%S}.csv"
