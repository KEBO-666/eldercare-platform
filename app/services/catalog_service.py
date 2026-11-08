"""服务项目与服务价格版本化业务逻辑。

价格版本规则（评分点：历史价格快照不被改写）：
- 新建项目自动创建 v1 价格版本，并回填 `service_item.current_price_version_id`；
- 新增价格版本时，自动把上一版本 `effective_to` 置为新版本生效时间，并让
  `current_price_version_id` 指向新版本；
- 工单只保存 `price_version_id / unit_price / amount` 快照，后续改价不影响历史工单。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.enums import AuditAction, ItemStatus, PricingMode
from app.core.exceptions import NotFound, RuleViolation
from app.models import ServiceItem, ServicePriceVersion, User

CENT = Decimal("0.01")
# 编辑接口允许修改的字段（价格必须走价格版本接口）
EDITABLE_FIELDS = (
    "name",
    "category",
    "pricing_mode",
    "standard_duration_min",
    "required_skills",
    "applicable_levels",
    "is_subsidized",
    "subsidy_ratio",
    "description",
)


def to_audit_payload(value: Any) -> Any:
    """审计 before/after 落 JSON 列前，把 datetime/Decimal 转成可序列化值。"""
    if isinstance(value, dict):
        return {str(key): to_audit_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_audit_payload(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def q2(value: Any) -> Decimal:
    """金额四舍五入到分。"""
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(CENT)


def _dt(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now()
    if value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


def get_item_or_404(db: Session, item_id: int) -> ServiceItem:
    item = db.get(ServiceItem, item_id)
    if item is None or item.is_deleted:
        raise NotFound("服务项目不存在")
    return item


def current_price_version(db: Session, item: ServiceItem) -> ServicePriceVersion | None:
    """当前价格版本：优先 current_price_version_id，其次按生效区间回退。"""
    if item.current_price_version_id:
        version = db.get(ServicePriceVersion, item.current_price_version_id)
        if version is not None and not version.is_deleted:
            return version
    versions = (
        db.execute(
            select(ServicePriceVersion)
            .where(
                ServicePriceVersion.service_item_id == item.id,
                ServicePriceVersion.is_deleted == 0,
            )
            .order_by(ServicePriceVersion.version.desc(), ServicePriceVersion.effective_from.desc())
        )
        .scalars()
        .all()
    )
    if not versions:
        return None
    now = datetime.now()
    for version in versions:
        if version.effective_from <= now and (version.effective_to is None or version.effective_to > now):
            return version
    return versions[0]


def current_price(db: Session, item: ServiceItem) -> Decimal:
    """项目当前单价（用于取消计费等场景）。"""
    version = current_price_version(db, item)
    return q2(version.unit_price) if version is not None else Decimal("0.00")


def price_snapshot(db: Session, item: ServiceItem) -> tuple[int | None, Decimal]:
    """返回 (价格版本 id, 单价)，供工单落库时做价格快照。"""
    version = current_price_version(db, item)
    if version is None:
        return None, Decimal("0.00")
    return version.id, q2(version.unit_price)


# ---------------- 序列化 ----------------
def item_to_dict(db: Session, item: ServiceItem) -> dict[str, Any]:
    version = current_price_version(db, item)
    return {
        "id": item.id,
        "code": item.code,
        "name": item.name,
        "category": item.category,
        "pricing_mode": item.pricing_mode,
        "standard_duration_min": int(item.standard_duration_min or 0),
        "required_skills": list(item.required_skills or []),
        "applicable_levels": list(item.applicable_levels or []),
        "is_subsidized": bool(item.is_subsidized),
        "subsidy_ratio": float(item.subsidy_ratio or 0),
        "status": item.status,
        "description": item.description,
        "current_price_version_id": item.current_price_version_id,
        "current_version": version.version if version is not None else None,
        "unit_price": float(q2(version.unit_price)) if version is not None else 0.0,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def price_to_dict(version: ServicePriceVersion, item_id: int | None = None) -> dict[str, Any]:
    return {
        "id": version.id,
        "service_item_id": version.service_item_id if item_id is None else item_id,
        "version": version.version,
        "unit_price": float(q2(version.unit_price)),
        "effective_from": version.effective_from,
        "effective_to": version.effective_to,
        "created_by": version.created_by,
        "remark": version.remark,
        "created_at": version.created_at,
    }


# ---------------- 目录读写 ----------------
def list_items(
    db: Session,
    *,
    category: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
) -> list[ServiceItem]:
    stmt = select(ServiceItem).where(ServiceItem.is_deleted == 0)
    if category:
        stmt = stmt.where(ServiceItem.category == category)
    if status:
        stmt = stmt.where(ServiceItem.status == status)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(ServiceItem.name.like(like) | ServiceItem.code.like(like))
    return list(db.execute(stmt.order_by(ServiceItem.id.asc())).scalars().all())


def create_item(db: Session, payload: dict[str, Any], *, user: User | None, request: Request | None = None) -> dict:
    """新建服务项目：自动创建 v1 价格版本并回填 current_price_version_id。"""
    code = str(payload.get("code") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not code:
        raise RuleViolation("项目编码不能为空")
    if not name:
        raise RuleViolation("项目名称不能为空")
    exists = db.execute(select(ServiceItem.id).where(ServiceItem.code == code)).first()
    if exists:
        raise RuleViolation(f"项目编码已存在：{code}")

    pricing_mode = payload.get("pricing_mode") or PricingMode.PER_TIME.value
    if pricing_mode not in {mode.value for mode in PricingMode}:
        raise RuleViolation(f"非法的计价方式：{pricing_mode}")
    status = payload.get("status") or ItemStatus.ON.value
    if status not in {s.value for s in ItemStatus}:
        raise RuleViolation(f"非法的项目状态：{status}")

    item = ServiceItem(
        code=code,
        name=name,
        category=payload.get("category") or "other",
        pricing_mode=pricing_mode,
        standard_duration_min=int(payload.get("standard_duration_min") or 60),
        required_skills=list(payload.get("required_skills") or []),
        applicable_levels=list(payload.get("applicable_levels") or []),
        is_subsidized=bool(payload.get("is_subsidized", True)),
        subsidy_ratio=float(payload.get("subsidy_ratio") or 0.0),
        status=status,
        description=payload.get("description"),
    )
    db.add(item)
    db.flush()

    version = ServicePriceVersion(
        service_item_id=item.id,
        version=1,
        unit_price=q2(payload.get("unit_price", 0)),
        effective_from=_dt(payload.get("effective_from")),
        created_by=getattr(user, "id", None),
        remark=payload.get("remark") or "初始价格",
    )
    db.add(version)
    db.flush()
    item.current_price_version_id = version.id

    data = item_to_dict(db, item)
    write_audit(
        db,
        user=user,
        action=AuditAction.CREATE.value,
        entity_type="service_item",
        entity_id=item.id,
        after=to_audit_payload(data),
        detail=f"新建服务项目 {item.code}",
        request=request,
    )
    db.commit()
    return item_to_dict(db, item)


def update_item(
    db: Session,
    item_id: int,
    payload: dict[str, Any],
    *,
    user: User | None,
    request: Request | None = None,
) -> dict:
    """编辑项目基础信息（价格字段忽略，必须走 /prices 版本化接口）。"""
    item = get_item_or_404(db, item_id)
    before = item_to_dict(db, item)

    for field in EDITABLE_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if field == "standard_duration_min" and value is not None:
            value = int(value)
        elif field == "subsidy_ratio" and value is not None:
            value = float(value)
        elif field in ("required_skills", "applicable_levels") and value is not None:
            value = list(value)
        elif field == "pricing_mode" and value not in {mode.value for mode in PricingMode}:
            raise RuleViolation(f"非法的计价方式：{value}")
        setattr(item, field, value)

    db.flush()
    after = item_to_dict(db, item)
    write_audit(
        db,
        user=user,
        action=AuditAction.UPDATE.value,
        entity_type="service_item",
        entity_id=item.id,
        before=to_audit_payload(before),
        after=to_audit_payload(after),
        detail=f"编辑服务项目 {item.code}（价格字段不在此接口维护）",
        request=request,
    )
    db.commit()
    return item_to_dict(db, item)


def set_item_status(
    db: Session,
    item_id: int,
    status: str,
    *,
    user: User | None,
    request: Request | None = None,
) -> dict:
    """上下架：status 仅允许 on / off。"""
    item = get_item_or_404(db, item_id)
    new_status = str(status or "").strip()
    if new_status not in {s.value for s in ItemStatus}:
        raise RuleViolation(f"非法的项目状态：{status}（仅支持 on/off）")
    before = {"status": item.status}
    item.status = new_status
    db.flush()
    write_audit(
        db,
        user=user,
        action=AuditAction.UPDATE.value,
        entity_type="service_item",
        entity_id=item.id,
        before=to_audit_payload(before),
        after={"status": new_status},
        detail=("上架服务项目" if new_status == ItemStatus.ON.value else "下架服务项目") + f" {item.code}",
        request=request,
    )
    db.commit()
    return item_to_dict(db, item)


# ---------------- 价格版本 ----------------
def list_price_versions(db: Session, item_id: int) -> list[dict[str, Any]]:
    item = get_item_or_404(db, item_id)
    versions = (
        db.execute(
            select(ServicePriceVersion)
            .where(
                ServicePriceVersion.service_item_id == item.id,
                ServicePriceVersion.is_deleted == 0,
            )
            .order_by(ServicePriceVersion.version.asc())
        )
        .scalars()
        .all()
    )
    current_id = item.current_price_version_id
    result = []
    for version in versions:
        row = price_to_dict(version, item.id)
        row["is_current"] = version.id == current_id
        result.append(row)
    return result


def add_price_version(
    db: Session,
    item_id: int,
    *,
    unit_price: Any,
    effective_from: datetime | None = None,
    remark: str | None = None,
    user: User | None,
    request: Request | None = None,
) -> dict:
    """新增价格版本：关闭上一版本生效区间，current_price_version_id 指向新版本。"""
    item = get_item_or_404(db, item_id)
    price = q2(unit_price)
    if price < 0:
        raise RuleViolation("单价不能为负数")
    start = _dt(effective_from)

    versions = (
        db.execute(
            select(ServicePriceVersion)
            .where(
                ServicePriceVersion.service_item_id == item.id,
                ServicePriceVersion.is_deleted == 0,
            )
            .order_by(ServicePriceVersion.version.desc())
        )
        .scalars()
        .all()
    )
    next_version = (versions[0].version + 1) if versions else 1

    # 关闭上一版本：上一版本 effective_to = 新版本生效时间
    previous = current_price_version(db, item) or (versions[0] if versions else None)
    before = {
        "current_price_version_id": item.current_price_version_id,
        "unit_price": float(q2(previous.unit_price)) if previous is not None else None,
    }
    if previous is not None:
        previous.effective_to = start
        db.flush()

    version = ServicePriceVersion(
        service_item_id=item.id,
        version=next_version,
        unit_price=price,
        effective_from=start,
        created_by=getattr(user, "id", None),
        remark=remark or f"第 {next_version} 版价格",
    )
    db.add(version)
    db.flush()
    item.current_price_version_id = version.id
    db.flush()

    write_audit(
        db,
        user=user,
        action=AuditAction.UPDATE.value,
        entity_type="service_price_version",
        entity_id=version.id,
        before=to_audit_payload(before),
        after=to_audit_payload({**price_to_dict(version, item.id), "current_price_version_id": version.id}),
        detail=f"服务项目 {item.code} 新增价格版本 v{next_version}：{price}",
        request=request,
    )
    db.commit()
    return {"item": item_to_dict(db, item), "price": price_to_dict(version, item.id)}
