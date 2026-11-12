"""智能派单业务逻辑：候选筛选与四因子评分、派单生成工单、改派、接单超时顺延。

评分权重读取顺序：ConfigItem["dispatch.weights"]（运行期可调） → settings.weights_tuple（.env）。
候选过滤：Staff.status=active、技能覆盖 required_skills、距离 ≤ 服务半径、
         当日已派工单数 < order_daily_limit、该时段无在途工单。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.algorithms.dispatch import (
    haversine,
    parse_weights,
    score_candidates,
)
from app.core.audit import write_audit
from app.core.config import settings
from app.core.deps import apply_scope
from app.core.enums import (
    AuditAction,
    ItemStatus,
    NotificationChannel,
    NotificationStatus,
    OrderStatus,
    RoleCode,
)
from app.core.exceptions import Conflict, NotFound, RuleViolation
from app.core.state_machine import APPOINTMENT, ORDER
from app.models import (
    Appointment,
    ConfigItem,
    Elder,
    Evaluation,
    Notification,
    Role,
    ServiceItem,
    Staff,
    User,
    WorkOrder,
)
from app.services import appointment_service as appt_service
from app.services import catalog_service

BUSY_EXCLUDED_STATUSES = (OrderStatus.CANCELLED.value, OrderStatus.COMPLETED.value)
DISPATCH_WEIGHTS_KEY = "dispatch.weights"
DEFAULT_ORDER_MINUTES = 60
MAX_ORDER_MINUTES = 24 * 60
ORDER_STATUS_NAMES = {
    OrderStatus.PENDING_ACCEPT.value: "待接单",
    OrderStatus.ACCEPTED.value: "已接单",
    OrderStatus.IN_SERVICE.value: "服务中",
    OrderStatus.PENDING_EVALUATION.value: "待评价",
    OrderStatus.COMPLETED.value: "已完成",
    OrderStatus.ANOMALY.value: "异常",
    OrderStatus.CANCELLED.value: "已取消",
}


# ---------------- 配置 ----------------
def get_dispatch_weights(db: Session) -> tuple[float, float, float, float]:
    """派单权重：ConfigItem 覆盖优先，其次环境变量默认值。"""
    raw = None
    row = db.execute(select(ConfigItem).where(ConfigItem.key == DISPATCH_WEIGHTS_KEY)).scalar_one_or_none()
    if row is not None and row.value:
        raw = row.value
    return parse_weights(raw, settings.weights_tuple)


# ---------------- 工具 ----------------
def item_duration(item: ServiceItem | None) -> timedelta:
    minutes = DEFAULT_ORDER_MINUTES
    if item is not None and item.standard_duration_min:
        minutes = max(1, int(item.standard_duration_min))
    return timedelta(minutes=minutes)


def _day_window(now: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(now.date(), time.min)
    return start, start + timedelta(days=1)


def _next_order_code(db: Session, day: date) -> str:
    prefix = f"WO{day:%Y%m%d}"
    codes = db.execute(select(WorkOrder.code).where(WorkOrder.code.like(f"{prefix}%"))).scalars().all()
    seq = 0
    for code in codes:
        tail = code[len(prefix):]
        if tail.isdigit():
            seq = max(seq, int(tail))
    return f"{prefix}{seq + 1:04d}"


def count_today_orders(db: Session, nurse_id: int, now: datetime | None = None) -> int:
    """护理员当日已派工单数（不含已取消）。"""
    now = now or datetime.now()
    start, end = _day_window(now)
    total = db.scalar(
        select(func.count())
        .select_from(WorkOrder)
        .where(
            WorkOrder.nurse_id == nurse_id,
            WorkOrder.status != OrderStatus.CANCELLED.value,
            WorkOrder.scheduled_start >= start,
            WorkOrder.scheduled_start < end,
        )
    )
    return int(total or 0)


def find_busy_order(
    db: Session,
    nurse_id: int,
    start: datetime,
    end: datetime,
    *,
    exclude_order_id: int | None = None,
) -> WorkOrder | None:
    """查找护理员在 [start, end) 内与之重叠的在途工单。"""
    margin = timedelta(minutes=MAX_ORDER_MINUTES)
    rows = db.execute(
        select(WorkOrder, ServiceItem.standard_duration_min)
        .join(ServiceItem, ServiceItem.id == WorkOrder.service_item_id, isouter=True)
        .where(
            WorkOrder.nurse_id == nurse_id,
            WorkOrder.status.notin_(BUSY_EXCLUDED_STATUSES),
            WorkOrder.scheduled_start >= start - margin,
            WorkOrder.scheduled_start < end,
        )
        .order_by(WorkOrder.scheduled_start.asc())
    ).all()
    for order, duration_min in rows:
        if exclude_order_id is not None and order.id == exclude_order_id:
            continue
        existing_start = order.scheduled_start
        existing_end = order.scheduled_end or (
            existing_start + timedelta(minutes=int(duration_min or DEFAULT_ORDER_MINUTES))
        )
        if existing_start < end and existing_end > start:
            return order
    return None


def _ensure_nurse_available(
    db: Session,
    staff: Staff,
    start: datetime,
    end: datetime,
    *,
    now: datetime | None = None,
    exclude_order_id: int | None = None,
) -> None:
    today_count = count_today_orders(db, staff.id, start)
    daily_limit = int(settings.order_daily_limit)
    if today_count >= daily_limit:
        raise Conflict(f"护理员「{staff.name}」当日已派 {today_count} 单，已达上限 {daily_limit} 单")
    busy = find_busy_order(db, staff.id, start, end, exclude_order_id=exclude_order_id)
    if busy is not None:
        raise Conflict(
            f"护理员「{staff.name}」在 {busy.scheduled_start:%Y-%m-%d %H:%M} 已有在途工单 {busy.code}，时段冲突"
        )


def _ensure_eligible(db: Session, staff: Staff, elder: Elder | None, item: ServiceItem | None) -> None:
    """手工派单与改派同样必须满足推荐算法的硬约束。"""
    if not is_item_available(item):
        raise RuleViolation("服务项目已下架或不存在，不可派单")
    if elder is None or elder.is_deleted:
        raise NotFound("老人档案不存在")
    missing = set(item.required_skills or []) - set(staff.skills or [])
    if missing:
        raise RuleViolation("护理员缺少必需技能：" + "、".join(sorted(missing)))
    distance = haversine(elder.lat, elder.lng, staff.base_lat, staff.base_lng)
    radius = min(float(settings.service_radius_km), float(staff.service_radius_km or 0))
    if distance > radius:
        raise RuleViolation("护理员超出服务半径或缺少定位坐标")


def _notify_family_and_grid(db: Session, appointment: Appointment, *, title: str, content: str) -> None:
    elder = db.get(Elder, appointment.elder_id)
    if elder is None:
        return
    recipients = set(db.scalars(
        select(User.id).join(Role, User.role_id == Role.id).where(
            User.is_active.is_(True),
            User.org_id == elder.grid_org_id,
            Role.code == RoleCode.GRID.value,
        )
    ).all())
    if elder.user_id:
        recipients.add(elder.user_id)
    for recipient in sorted(recipients):
        db.add(Notification(
            user_id=recipient, elder_id=elder.id,
            channel=NotificationChannel.INAPP.value, title=title, content=content,
            status=NotificationStatus.SENT.value, related_type="appointment", related_id=appointment.id,
        ))


def _notify_nurse(db: Session, staff: Staff, *, title: str, content: str, related_id: int | None) -> None:
    db.add(
        Notification(
            user_id=staff.user_id,
            channel=NotificationChannel.INAPP.value,
            title=title[:128],
            content=content,
            status=NotificationStatus.SENT.value,
            related_type="work_order",
            related_id=related_id,
        )
    )
    db.flush()


def order_to_dict(db: Session, order: WorkOrder) -> dict[str, Any]:
    elder = db.get(Elder, order.elder_id)
    item = db.get(ServiceItem, order.service_item_id)
    nurse = db.get(Staff, order.nurse_id) if order.nurse_id else None
    return {
        "id": order.id,
        "code": order.code,
        "appointment_id": order.appointment_id,
        "elder_id": order.elder_id,
        "elder_name": elder.name if elder is not None else None,
        "service_item_id": order.service_item_id,
        "item_name": item.name if item is not None else None,
        "nurse_id": order.nurse_id,
        "nurse_name": nurse.name if nurse is not None else None,
        "status": order.status,
        "status_name": ORDER_STATUS_NAMES.get(str(order.status), str(order.status)),
        "scheduled_start": order.scheduled_start,
        "scheduled_end": order.scheduled_end,
        "price_version_id": order.price_version_id,
        "unit_price": float(catalog_service.q2(order.unit_price)),
        "quantity": float(order.quantity or 0),
        "amount": float(catalog_service.q2(order.amount)),
        "community_org_id": order.community_org_id,
        "created_by": order.created_by,
        "created_at": order.created_at,
        "summary": order.summary,
    }


# ---------------- 候选推荐 ----------------
def get_candidates(
    db: Session,
    appointment_id: int,
    user: User | None,
    *,
    limit: int | None = None,
    now: datetime | None = None,
    exclude_nurse_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """返回 TopN 候选护理员及四项因子明细（按 score 倒序）。"""
    appointment = appt_service.get_or_404(db, appointment_id)
    if user is not None:
        appt_service.ensure_visible(db, user, appointment)
    if str(appointment.status) in appt_service.TERMINAL_APPOINTMENT_STATUSES:
        raise Conflict("预约单已结束，无法推荐候选护理员")

    elder = db.get(Elder, appointment.elder_id)
    item = db.get(ServiceItem, appointment.service_item_id)
    if elder is None or elder.is_deleted:
        raise NotFound("老人档案不存在")
    if not is_item_available(item):
        raise RuleViolation("服务项目已下架或不存在，不可派单")
    duration = item_duration(item)
    start = appointment.desired_start
    end = start + duration
    now = now or datetime.now()

    top_n = int(settings.dispatch_top_n if limit is None else limit)
    weights = get_dispatch_weights(db)
    daily_limit = int(settings.order_daily_limit)
    radius = float(settings.service_radius_km)

    staffs = db.execute(select(Staff).where(Staff.status == "active").order_by(Staff.id.asc())).scalars().all()
    # 近 30 天评分实时聚合，避免缓存评分过期后影响推荐。
    ratings = dict(db.execute(
        select(Evaluation.nurse_id, func.avg(Evaluation.avg_score))
        .where(Evaluation.created_at >= now - timedelta(days=30), Evaluation.created_at <= now)
        .group_by(Evaluation.nurse_id)
    ).all())
    day_start, day_end = _day_window(start)
    loads = dict(db.execute(
        select(WorkOrder.nurse_id, func.count(WorkOrder.id)).where(
            WorkOrder.scheduled_start >= day_start, WorkOrder.scheduled_start < day_end,
            WorkOrder.status != OrderStatus.CANCELLED.value,
        ).group_by(WorkOrder.nurse_id)
    ).all())
    raw_candidates: list[dict[str, Any]] = []
    for staff in staffs:
        if staff.id in (exclude_nurse_ids or set()):
            continue
        distance = haversine(elder.lat, elder.lng, staff.base_lat, staff.base_lng)
        raw_candidates.append(
            {
                "staff_id": staff.id,
                "name": staff.name,
                "distance_km": None if distance == float("inf") else distance,
                "today_count": loads.get(staff.id, 0),
                "skills": list(staff.skills or []),
                "rating": ratings.get(staff.id),
                "is_focus": bool(staff.is_focus),
                "service_radius_km": float(staff.service_radius_km or 0),
                "status": staff.status,
                "daily_limit": daily_limit,
            }
        )

    # 时段在途工单冲突的候选直接剔除（四因子之外的时间维度约束）
    available = [row for row in raw_candidates if find_busy_order(db, int(row["staff_id"]), start, end) is None]
    scored = score_candidates(
        available,
        weights,
        radius_km=radius,
        daily_limit=daily_limit,
        rating_default=float(settings.nurse_rating_default),
        required_skills=list(item.required_skills or []) if item is not None else [],
        top_n=top_n,
    )
    for row in scored:
        row["appointment_id"] = appointment.id
        row["desired_start"] = appointment.desired_start
        row["weights"] = [round(w, 4) for w in weights]
    return scored


# ---------------- 派单 ----------------
def assign_order(
    db: Session,
    appointment_id: int,
    nurse_id: int,
    *,
    user: User | None,
    request: Request | None = None,
    remark: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    appointment = appt_service.get_or_404(db, appointment_id)
    if user is not None:
        appt_service.ensure_visible(db, user, appointment)

    staff = db.get(Staff, nurse_id)
    if staff is None:
        raise NotFound("护理员不存在")
    if str(staff.status) != "active":
        raise RuleViolation(f"护理员「{staff.name}」已停用，不可派单")

    item = db.get(ServiceItem, appointment.service_item_id)
    _ensure_eligible(db, staff, db.get(Elder, appointment.elder_id), item)
    duration = item_duration(item)
    start = appointment.desired_start
    end = start + duration
    _ensure_nurse_available(db, staff, start, end)

    # 已受理的直接进入待派单，保证不绕过状态机
    if str(appointment.status) == "accepted":
        appt_service.transition_appointment(db, appointment, "to_dispatch", user=user, remark="派单前自动转待派单")
    from_status = appointment.status
    to_status = APPOINTMENT.next(from_status, "dispatch")

    price_version_id, unit_price = (
        catalog_service.price_snapshot(db, item) if item is not None else (None, Decimal("0.00"))
    )
    order = WorkOrder(
        code=_next_order_code(db, datetime.now().date()),
        appointment_id=appointment.id,
        elder_id=appointment.elder_id,
        service_item_id=appointment.service_item_id,
        nurse_id=staff.id,
        status=OrderStatus.PENDING_ACCEPT.value,
        scheduled_start=start,
        scheduled_end=end,
        price_version_id=price_version_id,
        unit_price=unit_price,
        quantity=1,
        amount=catalog_service.q2(unit_price * 1),
        community_org_id=appointment.community_org_id,
        created_by=getattr(user, "id", None),
        summary=remark,
    )
    db.add(order)
    db.flush()

    appointment.status = to_status
    appt_service.write_log(
        db,
        appointment,
        from_status=from_status,
        to_status=to_status,
        action="dispatch",
        operator_id=getattr(user, "id", None),
        remark=f"派单给 {staff.name}（工单 {order.code}）",
    )
    _notify_nurse(
        db,
        staff,
        title="新工单待接单",
        content=f"{start:%Y-%m-%d %H:%M} 上门服务工单 {order.code}，请在 {settings.accept_timeout_minutes} 分钟内接单",
        related_id=order.id,
    )
    _notify_family_and_grid(
        db, appointment, title="预约已派单",
        content=f"预约 {appointment.code} 已安排 {staff.name}，上门时间 {start:%Y-%m-%d %H:%M}",
    )
    data = order_to_dict(db, order)
    write_audit(
        db,
        user=user,
        action=AuditAction.DISPATCH.value,
        entity_type="work_order",
        entity_id=order.id,
        before={"appointment_status": from_status},
        after=catalog_service.to_audit_payload(data),
        detail=f"派单：预约 {appointment.code} → 工单 {order.code}，护理员 {staff.name}",
        request=request,
    )
    if commit:
        db.commit()
    return order_to_dict(db, order)


def reassign_order(
    db: Session,
    order_id: int,
    nurse_id: int,
    *,
    reason: str,
    user: User | None,
    request: Request | None = None,
) -> dict[str, Any]:
    """改派：reason 必填；原工单置 cancelled 保留历史，新建工单，写审计 REASSIGN。"""
    reason_text = str(reason or "").strip()
    if not reason_text:
        raise RuleViolation("改派原因必填")

    order = db.get(WorkOrder, order_id)
    if order is None:
        raise NotFound("工单不存在")
    if str(order.status) in (OrderStatus.CANCELLED.value, OrderStatus.COMPLETED.value):
        raise Conflict("工单已结束，无法改派")
    if order.appointment_id:
        appointment = appt_service.get_or_404(db, order.appointment_id)
        if user is not None:
            appt_service.ensure_visible(db, user, appointment)
    else:
        appointment = None

    staff = db.get(Staff, nurse_id)
    if staff is None:
        raise NotFound("护理员不存在")
    if str(staff.status) != "active":
        raise RuleViolation(f"护理员「{staff.name}」已停用，不可改派")
    if order.nurse_id == staff.id:
        raise RuleViolation("改派护理员不能与原工单相同")

    item = db.get(ServiceItem, order.service_item_id)
    _ensure_eligible(db, staff, db.get(Elder, order.elder_id), item)
    duration = item_duration(item)
    start = order.scheduled_start
    end = order.scheduled_end or (start + duration)
    _ensure_nurse_available(db, staff, start, end, exclude_order_id=order.id)

    old_nurse = db.get(Staff, order.nurse_id) if order.nurse_id else None
    before = order_to_dict(db, order)
    old_status = order.status
    order.status = ORDER.next(old_status, "cancel")
    order.summary = f"改派取消：{reason_text}"
    db.flush()

    new_order = WorkOrder(
        code=_next_order_code(db, datetime.now().date()),
        appointment_id=order.appointment_id,
        elder_id=order.elder_id,
        service_item_id=order.service_item_id,
        nurse_id=staff.id,
        status=OrderStatus.PENDING_ACCEPT.value,
        scheduled_start=start,
        scheduled_end=end,
        price_version_id=order.price_version_id,
        unit_price=order.unit_price,
        quantity=order.quantity,
        amount=order.amount,
        community_org_id=order.community_org_id,
        created_by=getattr(user, "id", None),
        summary=f"改派自 {order.code}：{reason_text}",
    )
    db.add(new_order)
    db.flush()

    _notify_nurse(
        db,
        staff,
        title="改派工单待接单",
        content=f"{start:%Y-%m-%d %H:%M} 上门服务工单 {new_order.code}（改派原因：{reason_text}）",
        related_id=new_order.id,
    )
    if old_nurse is not None:
        _notify_nurse(
            db,
            old_nurse,
            title="工单已改派",
            content=f"工单 {order.code} 已改派给 {staff.name}，原因：{reason_text}",
            related_id=order.id,
        )
    if order.appointment_id:
        _notify_family_and_grid(
            db, appointment, title="服务护理员已变更",
            content=f"预约 {appointment.code} 改派给 {staff.name}，原因：{reason_text}",
        )
        appt_service.write_log(
            db,
            appointment,
            from_status=appointment.status,
            to_status=appointment.status,
            action="reassign",
            operator_id=getattr(user, "id", None),
            remark=f"工单改派：{order.code} → {new_order.code}（{reason_text}）",
        )
    after = order_to_dict(db, new_order)
    write_audit(
        db,
        user=user,
        action=AuditAction.REASSIGN.value,
        entity_type="work_order",
        entity_id=new_order.id,
        before=catalog_service.to_audit_payload(before),
        after=catalog_service.to_audit_payload(
            {**after, "original_order_id": order.id, "reason": reason_text}
        ),
        detail=f"改派工单 {order.code} → {new_order.code}，原因：{reason_text}",
        request=request,
    )
    db.commit()
    return {
        "old_order_id": order.id,
        "old_order_code": order.code,
        "order": order_to_dict(db, new_order),
    }


# ---------------- 接单超时顺延 ----------------
def scan_accept_timeout(
    db: Session,
    *,
    now: datetime | None = None,
    timeout_minutes: int | None = None,
    operator_id: int | None = None,
    user: User | None = None,
) -> dict[str, Any]:
    """从派单创建时刻计时；超时后顺延至未尝试候选，无人可派则通知人工协调。"""
    now = now or datetime.now()
    minutes = int(settings.accept_timeout_minutes if timeout_minutes is None else timeout_minutes)
    if minutes <= 0:
        raise RuleViolation("接单超时分钟数必须大于 0")
    threshold = now - timedelta(minutes=minutes)

    stmt = select(WorkOrder).where(
        WorkOrder.status == OrderStatus.PENDING_ACCEPT.value,
        WorkOrder.created_at <= threshold,
    ).order_by(WorkOrder.id.asc())
    if user is None and operator_id is not None:
        user = db.get(User, operator_id)
        if user is None:
            raise NotFound("操作人不存在")
    if user is not None:
        stmt = apply_scope(stmt, WorkOrder, user, db)
    orders = (
        db.execute(stmt)
        .scalars()
        .all()
    )
    requeued_ids: list[int] = []
    requeued_orders: list[dict[str, Any]] = []
    reassigned_orders: list[dict[str, Any]] = []
    for order in orders:
        old_status = order.status
        order.status = ORDER.next(old_status, "cancel")
        order.summary = "接单超时自动顺延"
        db.flush()
        appointment = db.get(Appointment, order.appointment_id) if order.appointment_id else None
        if appointment is not None:
            appt_service.requeue_appointment(
                db,
                appointment,
                reason=f"工单 {order.code} 接单超时（{minutes} 分钟）自动顺延",
                operator_id=operator_id,
            )
            requeued_ids.append(appointment.id)
            attempted = set(db.scalars(select(WorkOrder.nurse_id).where(
                WorkOrder.appointment_id == appointment.id,
                WorkOrder.nurse_id.is_not(None),
            )).all())
            candidates = get_candidates(
                db, appointment.id, user, limit=1, now=now, exclude_nurse_ids=attempted,
            ) if is_item_available(db.get(ServiceItem, appointment.service_item_id)) else []
            if candidates and appointment.desired_start > now:
                replacement = assign_order(
                    db, appointment.id, candidates[0]["staff_id"], user=user,
                    remark=f"工单 {order.code} 接单超时自动顺延", commit=False,
                )
                reassigned_orders.append(replacement)
            else:
                _notify_family_and_grid(
                    db, appointment, title="派单需人工协调",
                    content=f"预约 {appointment.code} 接单超时，暂无可用新候选或已过预约时间，请网格员协调。",
                )
        requeued_orders.append(
            {
                "order_id": order.id,
                "order_code": order.code,
                "appointment_id": order.appointment_id,
                "appointment_code": appointment.code if appointment is not None else None,
                "nurse_id": order.nurse_id,
                "scheduled_start": order.scheduled_start,
                "appointment_status": appointment.status if appointment is not None else None,
            }
        )
        write_audit(
            db,
            user=user,
            action=AuditAction.UPDATE.value,
            entity_type="work_order",
            entity_id=order.id,
            before={"status": old_status},
            after={"status": order.status, "appointment_status": appointment.status if appointment else None},
            detail=f"接单超时自动顺延：工单 {order.code}（阈值 {minutes} 分钟）",
        )
    db.commit()
    return {
        "scanned": len(orders),
        "requeued": requeued_ids,
        "requeued_orders": requeued_orders,
        "timeout_minutes": minutes,
        "reassigned_orders": reassigned_orders,
        "now": now,
    }


def list_orders_for_nurse(db: Session, nurse_id: int, *, only_active: bool = False) -> list[WorkOrder]:
    """辅助查询：护理员工单（供调试与前端复用）。"""
    stmt = select(WorkOrder).where(WorkOrder.nurse_id == nurse_id)
    if only_active:
        stmt = stmt.where(WorkOrder.status.notin_(BUSY_EXCLUDED_STATUSES))
    return list(db.execute(stmt.order_by(WorkOrder.scheduled_start.desc())).scalars().all())


def is_item_available(item: ServiceItem | None) -> bool:
    """项目是否可派单（配合目录上下架）。"""
    return item is not None and not item.is_deleted and str(item.status) == ItemStatus.ON.value
