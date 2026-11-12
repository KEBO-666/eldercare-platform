"""工单执行与留痕服务（C 模块）。

覆盖：
- 工单列表/详情（数据范围：护理员只看本人、老人只看本人、社区/网格按组织）；
- 接单 / 拒单（拒单回退预约到待派单并通知网格员）；
- 签到（Haversine 计算与老人坐标距离，超阈值必须填写说明，写定位轨迹）；
- 逐项勾选（仅服务中工单）；
- 服务照片上传（Pillow 压缩到最长边 1280 / JPEG 80，最多 settings.photo_limit 张）；
- 签退（时长与项目标准时长偏差超阈值标记异常；可带健康指标写入 HealthMetric 并触发预警）；
- 异常上报与人工处置队列（等待分钟数、处置时长）。
"""
from __future__ import annotations

import io
import math
import uuid
from datetime import date as date_type
from datetime import datetime, time, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.config import BASE_DIR, settings
from app.core.deps import get_staff, scope_condition
from app.core.enums import (
    AnomalyType,
    MetricCode,
    MetricSource,
    OrderStatus,
    RoleCode,
)
from app.core.exceptions import Conflict, Forbidden, NotFound, RuleViolation
from app.core.state_machine import APPOINTMENT, ORDER
from app.db.base import utcnow
from app.models import (
    AnomalyReport,
    Appointment,
    AppointmentLog,
    Elder,
    Evaluation,
    HealthMetric,
    Notification,
    Org,
    Role,
    ServiceItem,
    Staff,
    User,
    WorkOrder,
    WorkOrderItem,
    WorkOrderPhoto,
    WorkOrderTrack,
)

EARTH_RADIUS_M = 6_371_000.0
PHOTO_MAX_EDGE = 1280
PHOTO_JPEG_QUALITY = 80

METRIC_UNITS: dict[str, str] = {
    str(MetricCode.SBP): "mmHg",
    str(MetricCode.DBP): "mmHg",
    str(MetricCode.HR): "次/分",
    str(MetricCode.SPO2): "%",
    str(MetricCode.STEPS): "步",
    str(MetricCode.MOTION): "分",
}


# ---------------------------------------------------------------- 纯函数
def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点间球面距离（米）。"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def compress_image(raw: bytes) -> tuple[bytes, int, int]:
    """压缩图片：最长边 1280、JPEG quality 80，返回 (字节, 宽, 高)。"""
    from PIL import Image, ImageOps

    try:
        if len(raw) > settings.photo_upload_max_mb * 1024 * 1024:
            raise RuleViolation(f"原始照片不能超过 {settings.photo_upload_max_mb}MB")
        image = Image.open(io.BytesIO(raw))
        if image.width * image.height > settings.photo_max_pixels:
            raise RuleViolation("图片像素过大，请缩小后上传")
        image.load()
        image = ImageOps.exif_transpose(image)
    except RuleViolation:
        raise
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都视为非法图片
        raise RuleViolation("无法解析上传的图片文件") from exc
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.thumbnail((PHOTO_MAX_EDGE, PHOTO_MAX_EDGE), Image.LANCZOS)
    # 重建像素图，移除 EXIF/GPS/XMP 等来源元数据。
    image = Image.frombytes("RGB", image.size, image.tobytes())
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=PHOTO_JPEG_QUALITY, optimize=True)
    return buffer.getvalue(), image.width, image.height


# ---------------------------------------------------------------- 数据范围
def resolve_community_org_id(db: Session, org_id: int | None) -> int | None:
    """向上追溯组织所属社区（网格 → 社区）；已是社区则返回本身。"""
    org = db.get(Org, org_id) if org_id else None
    hops = 0
    while org is not None and hops < 6:
        if org.level == "community":
            return org.id
        if org.parent_id is None:
            return org.id
        org = db.get(Org, org.parent_id)
        hops += 1
    return None


def order_scope_condition(db: Session, user: User):
    """工单数据范围条件；None 表示不受限。"""
    return scope_condition(WorkOrder, user, db)


def get_order(db: Session, order_id: int) -> WorkOrder:
    order = db.get(WorkOrder, order_id)
    if order is None:
        raise NotFound("工单不存在")
    return order


def ensure_order_visible(db: Session, user: User, order: WorkOrder) -> None:
    cond = order_scope_condition(db, user)
    if cond is None:
        return
    visible = db.execute(select(WorkOrder.id).where(WorkOrder.id == order.id, cond)).scalar_one_or_none()
    if visible is None:
        raise Forbidden("无权访问该工单（数据范围限制）")


def ensure_can_execute(db: Session, user: User, order: WorkOrder) -> Staff | None:
    """执行类动作：护理员只能操作本人名下的工单（未指派的可抢单）。"""
    role_code = user.role.code if user.role else None
    if role_code != RoleCode.NURSE:
        return None
    staff = get_staff(db, user)
    if staff is None:
        raise Forbidden("护理员档案不存在，无法执行工单")
    if order.nurse_id is not None and order.nurse_id != staff.id:
        raise Forbidden("只能操作本人名下的工单")
    return staff


# ---------------------------------------------------------------- 通知
def notify_users(
    db: Session,
    user_ids: list[int],
    *,
    title: str,
    content: str,
    related_type: str | None = None,
    related_id: int | None = None,
) -> int:
    targets = sorted({uid for uid in user_ids if uid})
    for uid in targets:
        db.add(
            Notification(
                user_id=uid,
                channel="inapp",
                title=title[:128],
                content=content,
                status="sent",
                related_type=related_type,
                related_id=related_id,
            )
        )
    return len(targets)


def community_user_ids(db: Session, community_org_id: int | None, role_codes: tuple[RoleCode, ...]) -> list[int]:
    """社区内指定角色的用户 id 列表（含社区本身与下属网格）。"""
    if not community_org_id:
        return []
    child_org_ids = [
        int(x)
        for x in db.execute(select(Org.id).where(Org.parent_id == community_org_id)).scalars().all()
    ]
    org_ids = [community_org_id, *child_org_ids]
    stmt = (
        select(User.id)
        .join(Role, User.role_id == Role.id)
        .where(
            Role.code.in_([str(r) for r in role_codes]),
            User.is_active.is_(True),
            User.org_id.in_(org_ids),
        )
    )
    return [int(x) for x in db.execute(stmt).scalars().all()]


def _sync_appointment(db: Session, order: WorkOrder, action: str, user: User) -> dict[str, Any] | None:
    """工单执行联动预约单状态（仅在状态机允许时推进，失败不影响主流程）。"""
    if not order.appointment_id:
        return None
    appointment = db.get(Appointment, order.appointment_id)
    if appointment is None or not APPOINTMENT.can(appointment.status, action):
        return None
    from_status = str(appointment.status)
    try:
        appointment.status = APPOINTMENT.next(appointment.status, action)
    except Conflict:  # pragma: no cover - can() 已兜住
        return None
    db.add(
        AppointmentLog(
            appointment_id=appointment.id,
            from_status=from_status,
            to_status=str(appointment.status),
            action=f"order_{action}",
            operator_id=user.id,
            remark=f"工单 {order.code} 触发",
        )
    )
    return {"appointment_id": appointment.id, "from_status": from_status, "to_status": str(appointment.status)}


# ---------------------------------------------------------------- 序列化
def serialize_photo(photo: WorkOrderPhoto) -> dict[str, Any]:
    return {
        "id": photo.id,
        "work_order_id": photo.work_order_id,
        "path": photo.path,
        "size_kb": photo.size_kb,
        "uploaded_at": photo.uploaded_at,
        "uploaded_by": photo.uploaded_by,
    }


def serialize_track(track: WorkOrderTrack) -> dict[str, Any]:
    return {
        "id": track.id,
        "kind": track.kind,
        "lat": track.lat,
        "lng": track.lng,
        "distance_m": track.distance_m,
        "note": track.note,
        "created_at": track.created_at,
    }


def serialize_order_item(item: WorkOrderItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "item_name": item.item_name,
        "is_required": item.is_required,
        "completed": item.completed,
        "completed_at": item.completed_at,
    }


def serialize_anomaly(report: AnomalyReport, now: datetime | None = None, order_code: str | None = None) -> dict[str, Any]:
    now = now or utcnow()
    waiting_minutes = None
    if str(report.status) == "pending" and report.reported_at is not None:
        waiting_minutes = max(0, int((now - report.reported_at).total_seconds() // 60))
    return {
        "id": report.id,
        "work_order_id": report.work_order_id,
        "work_order_code": order_code,
        "elder_id": report.elder_id,
        "anomaly_type": report.anomaly_type,
        "description": report.description,
        "reported_by": report.reported_by,
        "reported_at": report.reported_at,
        "status": str(report.status),
        "waiting_minutes": waiting_minutes,
        "handled_by": report.handled_by,
        "handled_at": report.handled_at,
        "handle_minutes": report.handle_minutes,
        "handle_note": report.handle_note,
    }


def serialize_order(
    order: WorkOrder,
    elder: Elder | None = None,
    staff: Staff | None = None,
    item: ServiceItem | None = None,
) -> dict[str, Any]:
    standard_duration = None
    if item is not None and item.standard_duration_min is not None:
        standard_duration = int(item.standard_duration_min)
    return {
        "id": order.id,
        "code": order.code,
        "status": str(order.status),
        "available_actions": ORDER.actions(str(order.status)),
        "appointment_id": order.appointment_id,
        "elder_id": order.elder_id,
        "elder_name": elder.name if elder is not None else None,
        "elder_address": elder.address_masked if elder is not None else None,
        "nurse_id": order.nurse_id,
        "nurse_name": staff.name if staff is not None else None,
        "service_item_id": order.service_item_id,
        "service_item_name": item.name if item is not None else None,
        "standard_duration_min": standard_duration,
        "scheduled_start": order.scheduled_start,
        "scheduled_end": order.scheduled_end,
        "accepted_at": order.accepted_at,
        "checkin_at": order.checkin_at,
        "checkin_lat": order.checkin_lat,
        "checkin_lng": order.checkin_lng,
        "checkin_distance_m": order.checkin_distance_m,
        "checkin_note": order.checkin_note,
        "checkout_at": order.checkout_at,
        "duration_min": order.duration_min,
        "duration_anomaly": bool(order.duration_anomaly),
        "summary": order.summary,
        "price_version_id": order.price_version_id,
        "unit_price": float(order.unit_price or 0),
        "quantity": order.quantity,
        "amount": float(order.amount or 0),
        "community_org_id": order.community_org_id,
        "completed_at": order.completed_at,
        "rating": order.rating,
        "created_at": order.created_at,
    }


def serialize_orders(db: Session, orders: list[WorkOrder]) -> list[dict[str, Any]]:
    elder_ids = {o.elder_id for o in orders if o.elder_id}
    nurse_ids = {o.nurse_id for o in orders if o.nurse_id}
    item_ids = {o.service_item_id for o in orders if o.service_item_id}
    elders = (
        {e.id: e for e in db.execute(select(Elder).where(Elder.id.in_(elder_ids))).scalars().all()}
        if elder_ids
        else {}
    )
    staffs = (
        {s.id: s for s in db.execute(select(Staff).where(Staff.id.in_(nurse_ids))).scalars().all()}
        if nurse_ids
        else {}
    )
    items = (
        {i.id: i for i in db.execute(select(ServiceItem).where(ServiceItem.id.in_(item_ids))).scalars().all()}
        if item_ids
        else {}
    )
    return [
        serialize_order(o, elders.get(o.elder_id), staffs.get(o.nurse_id), items.get(o.service_item_id))
        for o in orders
    ]


# ---------------------------------------------------------------- 查询
def _date_range(
    day: str | None, start_date: str | None, end_date: str | None
) -> tuple[datetime | None, datetime | None]:
    def _parse(value: str | None, field: str) -> date_type | None:
        if not value:
            return None
        try:
            return date_type.fromisoformat(value.strip())
        except ValueError as exc:
            raise RuleViolation(f"{field} 格式应为 YYYY-MM-DD") from exc

    start: datetime | None = None
    end: datetime | None = None
    parsed_day = _parse(day, "date")
    if parsed_day is not None:
        start = datetime.combine(parsed_day, time.min)
        end = start + timedelta(days=1)
    parsed_start = _parse(start_date, "start_date")
    if parsed_start is not None:
        start = datetime.combine(parsed_start, time.min)
    parsed_end = _parse(end_date, "end_date")
    if parsed_end is not None:
        end = datetime.combine(parsed_end, time.min) + timedelta(days=1)
    return start, end


def list_orders(
    db: Session,
    user: User,
    *,
    status: str | None = None,
    nurse_id: int | None = None,
    elder_id: int | None = None,
    day: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    community_org_id: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(WorkOrder)
    if status:
        stmt = stmt.where(WorkOrder.status == status)
    if nurse_id:
        stmt = stmt.where(WorkOrder.nurse_id == nurse_id)
    if elder_id:
        stmt = stmt.where(WorkOrder.elder_id == elder_id)
    if community_org_id:
        stmt = stmt.where(WorkOrder.community_org_id == community_org_id)
    start, end = _date_range(day, start_date, end_date)
    if start is not None:
        stmt = stmt.where(WorkOrder.scheduled_start >= start)
    if end is not None:
        stmt = stmt.where(WorkOrder.scheduled_start < end)
    cond = order_scope_condition(db, user)
    if cond is not None:
        stmt = stmt.where(cond)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        db.execute(
            stmt.order_by(WorkOrder.scheduled_start.desc(), WorkOrder.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        .scalars()
        .all()
    )
    return serialize_orders(db, list(rows)), total


def get_order_detail(db: Session, user: User, order_id: int) -> dict[str, Any]:
    order = get_order(db, order_id)
    ensure_order_visible(db, user, order)
    elder = db.get(Elder, order.elder_id) if order.elder_id else None
    staff = db.get(Staff, order.nurse_id) if order.nurse_id else None
    item = db.get(ServiceItem, order.service_item_id) if order.service_item_id else None
    data = serialize_order(order, elder, staff, item)
    items = (
        db.execute(select(WorkOrderItem).where(WorkOrderItem.work_order_id == order.id).order_by(WorkOrderItem.id.asc()))
        .scalars()
        .all()
    )
    photos = (
        db.execute(
            select(WorkOrderPhoto).where(WorkOrderPhoto.work_order_id == order.id).order_by(WorkOrderPhoto.id.asc())
        )
        .scalars()
        .all()
    )
    tracks = (
        db.execute(select(WorkOrderTrack).where(WorkOrderTrack.work_order_id == order.id).order_by(WorkOrderTrack.id.asc()))
        .scalars()
        .all()
    )
    reports = (
        db.execute(
            select(AnomalyReport).where(AnomalyReport.work_order_id == order.id).order_by(AnomalyReport.id.asc())
        )
        .scalars()
        .all()
    )
    data["items"] = [serialize_order_item(i) for i in items]
    data["photos"] = [serialize_photo(p) for p in photos]
    data["tracks"] = [serialize_track(t) for t in tracks]
    data["anomalies"] = [serialize_anomaly(r, order_code=order.code) for r in reports]
    data["anomaly_reports"] = data["anomalies"]
    data["elder"] = (
        {
            "id": elder.id,
            "name": elder.name,
            "gender": elder.gender,
            "address_masked": elder.address_masked,
            "lat": elder.lat,
            "lng": elder.lng,
            "ability_level": str(elder.ability_level),
            "grid_org_id": elder.grid_org_id,
            "community_org_id": elder.community_org_id,
        }
        if elder is not None
        else None
    )
    data["nurse"] = (
        {"id": staff.id, "name": staff.name, "employee_no": staff.employee_no} if staff is not None else None
    )
    data["service_item"] = (
        {
            "id": item.id,
            "name": item.name,
            "category": item.category,
            "standard_duration_min": int(item.standard_duration_min) if item.standard_duration_min else None,
        }
        if item is not None
        else None
    )
    evaluation = db.execute(
        select(Evaluation).where(Evaluation.work_order_id == order.id).order_by(Evaluation.id.desc()).limit(1)
    ).scalar_one_or_none()
    data["evaluation"] = (
        {
            "id": evaluation.id,
            "avg_score": evaluation.avg_score,
            "punctuality": evaluation.punctuality,
            "attitude": evaluation.attitude,
            "professionalism": evaluation.professionalism,
            "effect": evaluation.effect,
            "comment": evaluation.comment,
        }
        if evaluation is not None
        else None
    )
    if order.appointment_id:
        appointment = db.get(Appointment, order.appointment_id)
        data["appointment"] = (
            {"id": appointment.id, "code": appointment.code, "status": str(appointment.status)}
            if appointment is not None
            else None
        )
    return data


# ---------------------------------------------------------------- 接单 / 拒单
def accept_order(db: Session, user: User, order_id: int, request: Request | None = None) -> dict[str, Any]:
    order = get_order(db, order_id)
    ensure_order_visible(db, user, order)
    staff = ensure_can_execute(db, user, order)
    before = {"status": str(order.status)}
    order.status = ORDER.next(order.status, "accept")
    order.accepted_at = utcnow()
    if staff is not None and order.nurse_id is None:
        order.nurse_id = staff.id
    write_audit(
        db,
        user=user,
        action="accept",
        entity_type="work_order",
        entity_id=order.id,
        before=before,
        after={"status": str(order.status), "accepted_at": str(order.accepted_at)},
        request=request,
    )
    db.commit()
    return get_order_detail(db, user, order.id)


def reject_order(
    db: Session, user: User, order_id: int, reason: str, request: Request | None = None
) -> dict[str, Any]:
    reason = (reason or "").strip()
    if not reason:
        raise RuleViolation("拒单必须填写原因")
    order = get_order(db, order_id)
    ensure_order_visible(db, user, order)
    ensure_can_execute(db, user, order)
    before = {"status": str(order.status)}
    order.status = ORDER.next(order.status, "reject")

    appointment_info: dict[str, Any] | None = None
    if order.appointment_id:
        appointment = db.get(Appointment, order.appointment_id)
        if appointment is not None:
            from_status = str(appointment.status)
            # 优先走状态机合法回退动作（契约新增 requeue），其次 to_dispatch，最后兜底
            if APPOINTMENT.can(appointment.status, "requeue"):
                appointment.status = APPOINTMENT.next(appointment.status, "requeue")
            elif APPOINTMENT.can(appointment.status, "to_dispatch"):
                appointment.status = APPOINTMENT.next(appointment.status, "to_dispatch")
            elif from_status != "pending_dispatch":
                appointment.status = "pending_dispatch"
            if from_status != str(appointment.status):
                db.add(
                    AppointmentLog(
                        appointment_id=appointment.id,
                        from_status=from_status,
                        to_status=str(appointment.status),
                        action="reject_rollback",
                        operator_id=user.id,
                        remark=reason[:255],
                    )
                )
            appointment_info = {
                "appointment_id": appointment.id,
                "from_status": from_status,
                "to_status": str(appointment.status),
            }

    grid_user_ids = community_user_ids(db, order.community_org_id, (RoleCode.GRID,))
    notified = notify_users(
        db,
        grid_user_ids,
        title="工单被拒单，请重新派单",
        content=f"工单 {order.code} 被拒单，原因：{reason}",
        related_type="work_order",
        related_id=order.id,
    )
    write_audit(
        db,
        user=user,
        action="reject",
        entity_type="work_order",
        entity_id=order.id,
        before=before,
        after={"status": str(order.status), "reason": reason, "appointment": appointment_info},
        request=request,
    )
    db.commit()
    data = get_order_detail(db, user, order.id)
    data["reject_reason"] = reason
    data["appointment"] = appointment_info
    data["notified_count"] = notified
    return data


# ---------------------------------------------------------------- 签到
def checkin_order(
    db: Session,
    user: User,
    order_id: int,
    *,
    lat: float,
    lng: float,
    note: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    order = get_order(db, order_id)
    ensure_order_visible(db, user, order)
    ensure_can_execute(db, user, order)
    new_status = ORDER.next(order.status, "checkin")  # 未接单直接签到 → 409

    elder = db.get(Elder, order.elder_id) if order.elder_id else None
    distance_m: float | None = None
    if elder is not None and elder.lat is not None and elder.lng is not None:
        distance_m = round(haversine_m(float(lat), float(lng), float(elder.lat), float(elder.lng)), 1)

    threshold = settings.checkin_distance_threshold_m
    clean_note = (note or "").strip() or None
    if clean_note is not None:
        clean_note = clean_note[:255]
    if distance_m is not None and distance_m > threshold and not clean_note:
        raise RuleViolation(
            f"距离服务地址超过 {threshold} 米，请填写说明",
            data={"distance_m": distance_m, "threshold_m": threshold},
        )

    before = {"status": str(order.status)}
    now = utcnow()
    order.status = new_status
    order.checkin_at = now
    order.checkin_lat = float(lat)
    order.checkin_lng = float(lng)
    order.checkin_distance_m = distance_m
    order.checkin_note = clean_note
    db.add(
        WorkOrderTrack(
            work_order_id=order.id,
            kind="checkin",
            lat=float(lat),
            lng=float(lng),
            distance_m=distance_m,
            note=clean_note,
        )
    )
    appointment_sync = _sync_appointment(db, order, "start", user)
    write_audit(
        db,
        user=user,
        action="checkin",
        entity_type="work_order",
        entity_id=order.id,
        before=before,
        after={"status": str(order.status), "distance_m": distance_m, "note": clean_note},
        request=request,
    )
    db.commit()
    data = get_order_detail(db, user, order.id)
    data["distance_m"] = distance_m
    data["distance_threshold_m"] = threshold
    data["appointment_sync"] = appointment_sync
    return data


# ---------------------------------------------------------------- 逐项勾选
def update_items(
    db: Session,
    user: User,
    order_id: int,
    items: list[dict[str, Any]],
    request: Request | None = None,
) -> dict[str, Any]:
    order = get_order(db, order_id)
    ensure_order_visible(db, user, order)
    ensure_can_execute(db, user, order)
    if str(order.status) != OrderStatus.IN_SERVICE:
        raise Conflict("仅服务中的工单可勾选服务项目")
    if not items:
        raise RuleViolation("items 不能为空")

    existing = {
        it.item_name: it
        for it in db.execute(select(WorkOrderItem).where(WorkOrderItem.work_order_id == order.id)).scalars().all()
    }
    now = utcnow()
    touched: list[WorkOrderItem] = []
    for entry in items:
        name = str(entry.get("name") or "").strip()
        if not name:
            raise RuleViolation("服务项目名称不能为空")
        completed = bool(entry.get("completed", False))
        record = existing.get(name)
        if record is None:
            record = WorkOrderItem(
                work_order_id=order.id,
                item_name=name[:64],
                is_required=bool(entry.get("is_required", True)),
                completed=completed,
                completed_at=now if completed else None,
            )
            db.add(record)
            existing[name] = record
        else:
            record.completed = completed
            record.completed_at = now if completed else None
        touched.append(record)
    db.flush()
    write_audit(
        db,
        user=user,
        action="update",
        entity_type="work_order_item",
        entity_id=order.id,
        after={"items": [{"name": i.item_name, "completed": i.completed} for i in touched]},
        detail="逐项勾选服务项目",
        request=request,
    )
    db.commit()
    all_items = (
        db.execute(select(WorkOrderItem).where(WorkOrderItem.work_order_id == order.id).order_by(WorkOrderItem.id.asc()))
        .scalars()
        .all()
    )
    return {"work_order_id": order.id, "items": [serialize_order_item(i) for i in all_items]}


# ---------------------------------------------------------------- 照片
def save_photo(
    db: Session,
    user: User,
    order_id: int,
    raw: bytes,
    filename: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    order = get_order(db, order_id)
    ensure_order_visible(db, user, order)
    ensure_can_execute(db, user, order)
    if str(order.status) not in (OrderStatus.ACCEPTED, OrderStatus.IN_SERVICE):
        raise Conflict("仅已接单或服务中的工单可上传服务照片")
    if not raw:
        raise RuleViolation("上传文件为空")
    limit = settings.photo_limit
    uploaded = int(
        db.scalar(select(func.count()).select_from(WorkOrderPhoto).where(WorkOrderPhoto.work_order_id == order.id)) or 0
    )
    if uploaded >= limit:
        raise RuleViolation(f"服务照片最多 {limit} 张，已上传 {uploaded} 张")

    data, width, height = compress_image(raw)
    size_kb = max(1, math.ceil(len(data) / 1024))
    if size_kb > settings.photo_max_kb:
        raise RuleViolation(f"照片压缩后大小超过 {settings.photo_max_kb}KB 限制")

    target_dir = settings.upload_path / str(order.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}.jpg"
    target = target_dir / stored_name
    target.write_bytes(data)
    try:
        rel_path = target.relative_to(BASE_DIR).as_posix()
    except ValueError:  # 自定义上传目录在项目外
        rel_path = target.as_posix()

    now = utcnow()
    photo = WorkOrderPhoto(
        work_order_id=order.id,
        path=rel_path,
        size_kb=size_kb,
        uploaded_at=now,
        uploaded_by=user.id,
    )
    db.add(photo)
    db.commit()
    db.refresh(photo)

    photos = (
        db.execute(
            select(WorkOrderPhoto).where(WorkOrderPhoto.work_order_id == order.id).order_by(WorkOrderPhoto.id.asc())
        )
        .scalars()
        .all()
    )
    return {
        "work_order_id": order.id,
        "photo": serialize_photo(photo),
        "photos": [serialize_photo(p) for p in photos],
        "count": len(photos),
        "limit": limit,
        "width": width,
        "height": height,
        "filename": filename,
    }


# ---------------------------------------------------------------- 签退
def _record_metrics(
    db: Session, user: User, order: WorkOrder, metrics: list[dict[str, Any]], now: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid_codes = {str(code) for code in MetricCode}
    created: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []

    evaluate_metric = None
    try:  # D 模块未就绪时降级，不影响签退
        from app.services.alert_service import ensure_default_rules
        from app.services.alert_service import evaluate_metric as _evaluate_metric

        evaluate_metric = _evaluate_metric
        try:
            ensure_default_rules(db)  # 幂等初始化默认规则，保证上门录入指标能触发预警
        except Exception:  # noqa: BLE001 - 规则初始化失败不影响签退
            pass
    except Exception:  # noqa: BLE001
        evaluate_metric = None

    for entry in metrics:
        code = str(entry.get("metric_code") or "").strip().lower()
        if code not in valid_codes:
            raise RuleViolation(f"不支持的指标编码：{code or '(空)'}")
        try:
            value = float(entry.get("value"))
        except (TypeError, ValueError) as exc:
            raise RuleViolation(f"指标 {code} 的值必须是数字") from exc
        metric = HealthMetric(
            elder_id=order.elder_id,
            metric_code=code,
            value=value,
            unit=METRIC_UNITS.get(code),
            source=MetricSource.VISIT,
            measured_at=now,
            work_order_id=order.id,
            recorded_by=user.id,
        )
        db.add(metric)
        db.flush()
        created.append({"id": metric.id, "metric_code": code, "value": value, "unit": metric.unit})
        if evaluate_metric is not None:
            try:
                generated = evaluate_metric(db, metric, operator_id=user.id) or []
            except Exception:  # noqa: BLE001 - 预警模块异常不影响签退
                generated = []
            for alert in generated:
                alerts.append(
                    {
                        "id": getattr(alert, "id", None),
                        "level": str(getattr(alert, "level", "")),
                        "summary": getattr(alert, "summary", None),
                    }
                )
    return created, alerts


def checkout_order(
    db: Session,
    user: User,
    order_id: int,
    *,
    summary: str = "",
    metrics: list[dict[str, Any]] | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    order = get_order(db, order_id)
    ensure_order_visible(db, user, order)
    ensure_can_execute(db, user, order)
    new_status = ORDER.next(order.status, "checkout")  # 未签到直接签退 → 409
    if order.checkin_at is None:
        raise Conflict("工单尚未签到，不能签退")

    now = utcnow()
    duration_min = int(round((now - order.checkin_at).total_seconds() / 60))
    if duration_min < 0:
        raise RuleViolation("签退时间早于签到时间，请检查数据")

    item = db.get(ServiceItem, order.service_item_id) if order.service_item_id else None
    standard_duration = int(item.standard_duration_min) if item is not None and item.standard_duration_min else 60
    deviation_min = abs(duration_min - standard_duration)
    anomaly = deviation_min > settings.duration_anomaly_minutes

    before = {"status": str(order.status)}
    order.status = new_status
    order.checkout_at = now
    order.duration_min = duration_min
    order.duration_anomaly = anomaly
    order.summary = (summary or "").strip() or None
    db.add(
        WorkOrderTrack(
            work_order_id=order.id,
            kind="checkout",
            note=(order.summary or "签退")[:255],
        )
    )
    created_metrics, alerts = _record_metrics(db, user, order, metrics or [], now)
    appointment_sync = _sync_appointment(db, order, "complete", user)
    write_audit(
        db,
        user=user,
        action="checkout",
        entity_type="work_order",
        entity_id=order.id,
        before=before,
        after={
            "status": str(order.status),
            "duration_min": duration_min,
            "standard_duration_min": standard_duration,
            "duration_anomaly": anomaly,
            "metrics": [m["metric_code"] for m in created_metrics],
        },
        request=request,
    )
    db.commit()
    data = get_order_detail(db, user, order.id)
    data.update(
        {
            "standard_duration_min": standard_duration,
            "deviation_min": deviation_min,
            "duration_anomaly_threshold_min": settings.duration_anomaly_minutes,
            "metrics_recorded": created_metrics,
            "alerts_created": alerts,
            "appointment_sync": appointment_sync,
        }
    )
    return data


# ---------------------------------------------------------------- 异常上报 / 处置
def report_anomaly(
    db: Session,
    user: User,
    order_id: int,
    *,
    anomaly_type: str,
    description: str,
    request: Request | None = None,
) -> dict[str, Any]:
    order = get_order(db, order_id)
    ensure_order_visible(db, user, order)
    ensure_can_execute(db, user, order)
    clean_type = str(anomaly_type or "").strip()
    if clean_type not in {str(t) for t in AnomalyType}:
        raise RuleViolation(f"不支持的异常类型：{clean_type or '(空)'}")
    clean_desc = (description or "").strip()
    if not clean_desc:
        raise RuleViolation("异常描述不能为空")

    new_status = ORDER.next(order.status, "report_anomaly")
    before = {"status": str(order.status)}
    now = utcnow()
    order.status = new_status
    report = AnomalyReport(
        work_order_id=order.id,
        elder_id=order.elder_id,
        anomaly_type=clean_type,
        description=clean_desc,
        reported_by=user.id,
        reported_at=now,
        status="pending",
    )
    db.add(report)
    db.flush()

    handler_user_ids = community_user_ids(db, order.community_org_id, (RoleCode.GRID, RoleCode.ORG_ADMIN))
    notified = notify_users(
        db,
        handler_user_ids,
        title="工单异常上报待处置",
        content=f"工单 {order.code} 上报异常（{clean_type}）：{clean_desc}",
        related_type="anomaly_report",
        related_id=report.id,
    )
    write_audit(
        db,
        user=user,
        action="create",
        entity_type="anomaly_report",
        entity_id=report.id,
        before=before,
        after={"work_order_id": order.id, "anomaly_type": clean_type, "description": clean_desc},
        request=request,
    )
    db.commit()
    data = get_order_detail(db, user, order.id)
    data["anomaly"] = serialize_anomaly(report, now, order.code)
    data["notified_count"] = notified
    return data


def list_anomalies(
    db: Session,
    user: User,
    *,
    status: str | None = "pending",
    work_order_id: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(AnomalyReport)
    if status and status != "all":
        stmt = stmt.where(AnomalyReport.status == status)
    if work_order_id:
        stmt = stmt.where(AnomalyReport.work_order_id == work_order_id)
    cond = order_scope_condition(db, user)
    if cond is not None:
        stmt = stmt.where(AnomalyReport.work_order_id.in_(select(WorkOrder.id).where(cond)))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        db.execute(
            stmt.order_by(AnomalyReport.reported_at.asc(), AnomalyReport.id.asc())
            .offset((page - 1) * size)
            .limit(size)
        )
        .scalars()
        .all()
    )
    now = utcnow()
    order_ids = {r.work_order_id for r in rows if r.work_order_id}
    codes = (
        {o.id: o.code for o in db.execute(select(WorkOrder).where(WorkOrder.id.in_(order_ids))).scalars().all()}
        if order_ids
        else {}
    )
    return [serialize_anomaly(r, now, codes.get(r.work_order_id)) for r in rows], total


def handle_anomaly(
    db: Session,
    user: User,
    anomaly_id: int,
    *,
    handle_note: str,
    request: Request | None = None,
) -> dict[str, Any]:
    report = db.get(AnomalyReport, anomaly_id)
    if report is None:
        raise NotFound("异常报告不存在")
    from app.core.deps import ensure_elder_visible, has_permission

    if not has_permission(user, "order:dispatch") and not has_permission(user, "order:reassign"):
        raise Forbidden("缺少异常处置权限")
    ensure_elder_visible(db, user, report.elder_id)
    if report.work_order_id is not None:
        ensure_order_visible(db, user, get_order(db, report.work_order_id))
    if str(report.status) == "handled":
        raise RuleViolation("该异常已处置完成")
    clean_note = (handle_note or "").strip()
    if not clean_note:
        raise RuleViolation("处置说明不能为空")

    now = utcnow()
    order: WorkOrder | None = db.get(WorkOrder, report.work_order_id) if report.work_order_id else None
    if order is not None and str(order.status) == OrderStatus.ANOMALY:
        order.status = ORDER.next(order.status, "resolve")
        order.completed_at = now
    report.status = "handled"
    report.handled_by = user.id
    report.handled_at = now
    report.handle_minutes = (
        max(0, int(round((now - report.reported_at).total_seconds() / 60))) if report.reported_at else 0
    )
    report.handle_note = clean_note
    write_audit(
        db,
        user=user,
        action="update",
        entity_type="anomaly_report",
        entity_id=report.id,
        after={
            "status": "handled",
            "handle_note": clean_note,
            "handle_minutes": report.handle_minutes,
            "order_status": str(order.status) if order is not None else None,
        },
        detail="异常处置",
        request=request,
    )
    db.commit()
    data = serialize_anomaly(report, now, order.code if order is not None else None)
    data["order_status"] = str(order.status) if order is not None else None
    return data
