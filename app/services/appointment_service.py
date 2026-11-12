"""预约受理业务逻辑：三重校验、状态机流转、取消计费、状态日志与审计。

校验不通过且需人工介入的场景统一抛 `NeedsManual`（HTTP 422 / code=NEEDS_MANUAL）：
1. 同一老人 desired_start ±2 小时内已有有效预约或工单，或实际服务区间重叠；
2. 老人所在社区不在当前用户可服务范围（网格员/机构管理员按本机构，老人/家属按绑定老人社区）；
3. 服务项目 `applicable_levels` 非空且不包含老人能力等级。
项目已下架则直接拒单（RuleViolation，HTTP 400）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import Request
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.config import settings
from app.core.deps import effective_scope, ensure_elder_visible, get_elder_of_user, get_staff, scope_condition
from app.core.enums import (
    AppointmentStatus,
    AuditAction,
    DataScope,
    ItemStatus,
    OrderStatus,
    RoleCode,
)
from app.core.exceptions import Forbidden, NeedsManual, NotFound, RuleViolation
from app.core.security import decrypt_field, encrypt_field, mask_address
from app.core.state_machine import APPOINTMENT, ORDER
from app.models import Appointment, AppointmentLog, Elder, Org, ServiceItem, User, WorkOrder
from app.services import catalog_service

CONFLICT_WINDOW_HOURS = 2.0
# 视为"已结束/不占用时段"的预约与工单状态
INACTIVE_APPOINTMENT_STATUSES = (AppointmentStatus.CANCELLED.value, AppointmentStatus.NO_SHOW.value)
INACTIVE_ORDER_STATUSES = (OrderStatus.CANCELLED.value,)

APPOINTMENT_STATUS_NAMES = {
    AppointmentStatus.PENDING_ACCEPT.value: "待受理",
    AppointmentStatus.ACCEPTED.value: "已受理",
    AppointmentStatus.PENDING_DISPATCH.value: "待派单",
    AppointmentStatus.DISPATCHED.value: "已派单",
    AppointmentStatus.IN_SERVICE.value: "服务中",
    AppointmentStatus.COMPLETED.value: "已完成",
    AppointmentStatus.CANCELLED.value: "已取消",
    AppointmentStatus.NO_SHOW.value: "已爽约",
}
TERMINAL_APPOINTMENT_STATUSES = (
    AppointmentStatus.COMPLETED.value,
    AppointmentStatus.CANCELLED.value,
    AppointmentStatus.NO_SHOW.value,
)


# ---------------- 通用工具 ----------------
def to_naive(value: datetime | None) -> datetime | None:
    """统一转换为 naive 本地时间。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


def get_or_404(db: Session, appointment_id: int) -> Appointment:
    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise NotFound("预约单不存在")
    return appointment


def _next_code(db: Session, day: date) -> str:
    prefix = f"AP{day:%Y%m%d}"
    codes = db.execute(select(Appointment.code).where(Appointment.code.like(f"{prefix}%"))).scalars().all()
    seq = 0
    for code in codes:
        tail = code[len(prefix):]
        if tail.isdigit():
            seq = max(seq, int(tail))
    return f"{prefix}{seq + 1:04d}"


def write_log(
    db: Session,
    appointment: Appointment,
    *,
    from_status: str | None,
    to_status: str,
    action: str,
    operator_id: int | None = None,
    remark: str | None = None,
) -> AppointmentLog:
    log = AppointmentLog(
        appointment_id=appointment.id,
        from_status=from_status,
        to_status=to_status,
        action=action,
        operator_id=operator_id,
        remark=str(remark)[:255] if remark else None,
    )
    db.add(log)
    db.flush()
    return log


def transition_appointment(
    db: Session,
    appointment: Appointment,
    action: str,
    *,
    user: User | None,
    remark: str | None = None,
) -> str:
    """调用状态机流转并落状态日志（不允许直接赋状态值）。"""
    from_status = appointment.status
    to_status = APPOINTMENT.next(from_status, action)
    appointment.status = to_status
    write_log(
        db,
        appointment,
        from_status=from_status,
        to_status=to_status,
        action=action,
        operator_id=getattr(user, "id", None),
        remark=remark,
    )
    db.flush()
    return to_status


# ---------------- 数据范围 ----------------
def _org_ids_of_user(db: Session, user: User) -> set[int]:
    """当前用户所属机构 + 其父级（社区）+ 其子级（网格）的组织 id 集合。"""
    ids: set[int] = set()
    if user.org_id is None:
        return ids
    ids.add(user.org_id)
    org = db.get(Org, user.org_id)
    if org is not None and org.parent_id:
        ids.add(org.parent_id)
    children = db.execute(select(Org.id).where(Org.parent_id == user.org_id)).scalars().all()
    ids.update(int(cid) for cid in children)
    return ids


def allowed_community_ids(db: Session, user: User) -> set[int] | None:
    """当前用户可服务的社区/网格组织 id 集合；None 表示不受限。"""
    role_code = user.role.code if user.role else None
    scope = effective_scope(user)
    if role_code in (RoleCode.SUPER, RoleCode.STREET) or scope == DataScope.STREET:
        return None
    if role_code == RoleCode.ELDER:
        bound = get_elder_of_user(db, user)
        if bound is None:
            return set()
        return {bound.community_org_id}
    if role_code in (RoleCode.ORG_ADMIN, RoleCode.GRID):
        return _org_ids_of_user(db, user)
    if role_code == RoleCode.NURSE:
        staff = get_staff(db, user)
        return {staff.org_id} if staff is not None and staff.org_id else set()
    return set()


def scope_conditions(db: Session, user: User) -> list[Any]:
    """预约与档案、工单共用数据范围规则。"""
    condition = scope_condition(Appointment, user, db)
    return [] if condition is None else [condition]


def apply_scope(stmt: Select, db: Session, user: User) -> Select:
    for condition in scope_conditions(db, user):
        stmt = stmt.where(condition)
    return stmt


def ensure_visible(db: Session, user: User, appointment: Appointment) -> None:
    """越权访问抛 403。"""
    conditions = scope_conditions(db, user)
    if not conditions:
        return
    exists = db.execute(select(Appointment.id).where(Appointment.id == appointment.id, *conditions)).first()
    if exists is None:
        raise Forbidden("无权访问该预约单（数据范围限制）")


# ---------------- 校验 ----------------
def _resolve_elder(db: Session, user: User | None, elder_id: int | None) -> Elder:
    role_code = user.role.code if user is not None and user.role else None
    if elder_id is None:
        if role_code == RoleCode.ELDER:
            bound = get_elder_of_user(db, user) if user is not None else None
            if bound is None:
                raise NotFound("当前账号未绑定老人档案")
            return bound
        raise RuleViolation("请指定老人档案 elder_id")
    elder = db.get(Elder, elder_id)
    if elder is None or elder.is_deleted:
        raise NotFound("老人档案不存在")
    if user is not None:
        ensure_elder_visible(db, user, elder.id)
    return elder


def _check_community_scope(db: Session, user: User | None, elder: Elder) -> None:
    if user is None:
        return
    allowed = allowed_community_ids(db, user)
    if allowed is None:
        return
    if elder.community_org_id in allowed:
        return
    if elder.grid_org_id is not None and elder.grid_org_id in allowed:
        return
    raise NeedsManual(
        f"老人所在社区不在当前用户可服务范围（elder.community_org_id={elder.community_org_id}，"
        f"allowed={sorted(allowed)}），需转人工受理"
    )


def _conflict_window(desired_start: datetime) -> tuple[datetime, datetime]:
    delta = timedelta(hours=CONFLICT_WINDOW_HOURS)
    return desired_start - delta, desired_start + delta


def _service_duration(minutes: float | None) -> timedelta:
    return timedelta(minutes=max(1, int(minutes or 60)))


def _check_appointment_conflict(
    db: Session, elder_id: int, desired_start: datetime, duration: timedelta,
) -> None:
    low, high = _conflict_window(desired_start)
    desired_end = desired_start + duration
    # 不设开始时间下界：数小时前开始的长服务仍可能占用当前时段。
    appointments = db.execute(
        select(Appointment, ServiceItem.standard_duration_min)
        .outerjoin(ServiceItem, ServiceItem.id == Appointment.service_item_id)
        .where(
            Appointment.elder_id == elder_id,
            Appointment.status.notin_(INACTIVE_APPOINTMENT_STATUSES),
            Appointment.desired_start <= max(high, desired_end),
        ).order_by(Appointment.desired_start, Appointment.id)
    ).all()
    orders = db.execute(
        select(WorkOrder, ServiceItem.standard_duration_min)
        .outerjoin(ServiceItem, ServiceItem.id == WorkOrder.service_item_id)
        .where(
            WorkOrder.elder_id == elder_id,
            WorkOrder.status.notin_(INACTIVE_ORDER_STATUSES),
            WorkOrder.scheduled_start <= max(high, desired_end),
        ).order_by(WorkOrder.scheduled_start, WorkOrder.id)
    ).all()
    for source, label, rows in (("appointment", "预约单", appointments), ("work_order", "工单", orders)):
        for record, minutes in rows:
            start = record.desired_start if source == "appointment" else record.scheduled_start
            end = (record.scheduled_end if source == "work_order" else None) or (start + _service_duration(minutes))
            nearby = low <= start <= high
            overlap = start < desired_end and end > desired_start
            if nearby or overlap:
                explanation = f"开始时间前后 {CONFLICT_WINDOW_HOURS:g} 小时内" if nearby else "实际服务时段重叠"
                raise NeedsManual(
                    f"该老人在 {desired_start:%Y-%m-%d %H:%M} 已存在{label} {record.code}"
                    f"（{start:%Y-%m-%d %H:%M} 至 {end:%Y-%m-%d %H:%M}），{explanation}；"
                    "本次未创建，请调整时段或联系网格员",
                    data={"conflict": {"source": source, "code": record.code,
                                       "start": start.isoformat(), "end": end.isoformat(),
                                       "rule": "start_window" if nearby else "service_overlap"}},
                )


def _check_ability(item: ServiceItem, elder: Elder) -> None:
    levels = [str(level) for level in (item.applicable_levels or [])]
    if not levels:
        return
    if str(elder.ability_level) in levels:
        return
    raise NeedsManual(
        f"老人能力等级「{elder.ability_level}」与服务项目「{item.name}」适用等级 "
        f"{'/'.join(levels)} 不匹配，需转人工受理"
    )


def _check_item_available(item: ServiceItem) -> None:
    if str(item.status) != ItemStatus.ON.value:
        raise RuleViolation(f"服务项目「{item.name}」已下架，暂不可预约")


# ---------------- 序列化 ----------------
def appointment_to_dict(db: Session, appointment: Appointment, *, with_logs: bool = False) -> dict[str, Any]:
    elder = db.get(Elder, appointment.elder_id)
    item = db.get(ServiceItem, appointment.service_item_id)
    address_masked = ""
    if appointment.address_enc:
        address_masked = mask_address(decrypt_field(appointment.address_enc))
    elif elder is not None:
        address_masked = elder.address_masked or ""
    data: dict[str, Any] = {
        "id": appointment.id,
        "code": appointment.code,
        "elder_id": appointment.elder_id,
        "elder_name": elder.name if elder is not None else None,
        "elder_code": elder.code if elder is not None else None,
        "service_item_id": appointment.service_item_id,
        "item_name": item.name if item is not None else None,
        "item_code": item.code if item is not None else None,
        "desired_start": appointment.desired_start,
        "status": appointment.status,
        "status_name": APPOINTMENT_STATUS_NAMES.get(str(appointment.status), str(appointment.status)),
        "address_masked": address_masked,
        "remark": appointment.remark,
        "cycle_rule": appointment.cycle_rule,
        "cancel_reason": appointment.cancel_reason,
        "cancellation_fee": float(appointment.cancellation_fee or Decimal("0.00")),
        "is_chargeable_cancel": bool(appointment.is_chargeable_cancel),
        "community_org_id": appointment.community_org_id,
        "created_by": appointment.created_by,
        "accepted_by": appointment.accepted_by,
        "created_at": appointment.created_at,
        "updated_at": appointment.updated_at,
    }
    if with_logs:
        logs = (
            db.execute(
                select(AppointmentLog)
                .where(AppointmentLog.appointment_id == appointment.id)
                .order_by(AppointmentLog.id.asc())
            )
            .scalars()
            .all()
        )
        data["logs"] = [
            {
                "id": log.id,
                "from_status": log.from_status,
                "to_status": log.to_status,
                "action": log.action,
                "operator_id": log.operator_id,
                "remark": log.remark,
                "created_at": log.created_at,
            }
            for log in logs
        ]
        data["available_actions"] = APPOINTMENT.actions(appointment.status)
    return data


# ---------------- 创建预约 ----------------
def preview_appointments(
    db: Session, *, service_item_id: int, desired_start: datetime,
    elder_id: int | None = None, cycle_rule: str | None = None, user: User | None = None,
) -> dict[str, Any]:
    """只读检查全部日期；不创建、不占位，提交仍在事务中重新校验。"""
    rule = str(cycle_rule or "").strip() or None
    if rule not in (None, "weekly"):
        raise RuleViolation("周期规则仅支持 weekly（每周固定，连续 4 周）")
    start = to_naive(desired_start)
    if start is None:
        raise RuleViolation("预约时间 desired_start 必填")
    item = db.get(ServiceItem, service_item_id)
    if item is None or item.is_deleted:
        raise NotFound("服务项目不存在")
    _check_item_available(item)
    elder = _resolve_elder(db, user, elder_id)
    _check_community_scope(db, user, elder)
    _check_ability(item, elder)
    count = 4 if rule == "weekly" else 1
    occurrences = []
    for week in range(count):
        try:
            occurrence_start = start + timedelta(weeks=week)
            occurrence_end = occurrence_start + _service_duration(item.standard_duration_min)
            if any(row["desired_end"] > occurrence_start for row in occurrences):
                raise NeedsManual("周期内的服务时段互相重叠，请缩短服务时长或改为单次预约")
            _check_appointment_conflict(db, elder.id, occurrence_start, occurrence_end - occurrence_start)
        except OverflowError as exc:
            raise RuleViolation("预约日期超出支持范围，请调整开始日期") from exc
        except NeedsManual as exc:
            detail = {**(exc.data or {}), "occurrence_number": week + 1, "occurrence_count": count,
                      "desired_start": occurrence_start.isoformat()}
            message = exc.message if count == 1 else f"第 {week + 1} 次预约校验失败：{exc.message}；4 次均未创建"
            raise NeedsManual(message, data=detail) from exc
        occurrences.append({"occurrence_number": week + 1, "desired_start": occurrence_start,
                            "desired_end": occurrence_end})
    return {"cycle_rule": rule, "occurrence_count": count, "occurrences": occurrences,
            "reservation_created": False}


def create_appointment(
    db: Session,
    *,
    service_item_id: int,
    desired_start: datetime,
    elder_id: int | None = None,
    address: str | None = None,
    remark: str | None = None,
    cycle_rule: str | None = None,
    user: User | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """每周固定预约一次生成 4 周，每次独立校验，任一次失败则整组回滚。"""
    occurrences = []
    with db.begin_nested():
        plan = preview_appointments(db, service_item_id=service_item_id, desired_start=desired_start,
                                    elder_id=elder_id, cycle_rule=cycle_rule, user=user)
        rule = plan["cycle_rule"]
        for occurrence in plan["occurrences"]:
            occurrences.append(_create_single_appointment(
                db, service_item_id=service_item_id,
                desired_start=occurrence["desired_start"],
                elder_id=elder_id, address=address, remark=remark, cycle_rule=rule,
                user=user, request=request,
            ))
    db.commit()
    data = dict(occurrences[0])
    if rule == "weekly":
        data["occurrences"] = occurrences
        data["occurrence_count"] = len(occurrences)
    return data


def _create_single_appointment(
    db: Session,
    *,
    service_item_id: int,
    desired_start: datetime,
    elder_id: int | None,
    address: str | None,
    remark: str | None,
    cycle_rule: str | None,
    user: User | None,
    request: Request | None,
) -> dict[str, Any]:
    """单次预约与状态、审计日志在调用者事务内写入。"""
    start = to_naive(desired_start)
    if start is None:
        raise RuleViolation("预约时间 desired_start 必填")

    item = db.get(ServiceItem, service_item_id)
    if item is None or item.is_deleted:
        raise NotFound("服务项目不存在")
    _check_item_available(item)

    elder = _resolve_elder(db, user, elder_id)
    _check_appointment_conflict(db, elder.id, start, _service_duration(item.standard_duration_min))
    _check_community_scope(db, user, elder)
    _check_ability(item, elder)

    appointment = Appointment(
        code=_next_code(db, datetime.now().date()),
        elder_id=elder.id,
        service_item_id=item.id,
        desired_start=start,
        address_enc=encrypt_field(address) if address else elder.address_enc,
        remark=remark,
        cycle_rule=cycle_rule,
        status=AppointmentStatus.PENDING_ACCEPT.value,
        community_org_id=elder.community_org_id,
        created_by=getattr(user, "id", None),
    )
    db.add(appointment)
    db.flush()
    write_log(
        db,
        appointment,
        from_status=None,
        to_status=AppointmentStatus.PENDING_ACCEPT.value,
        action="create",
        operator_id=getattr(user, "id", None),
        remark="提交预约",
    )
    data = appointment_to_dict(db, appointment, with_logs=True)
    write_audit(
        db,
        user=user,
        action=AuditAction.CREATE.value,
        entity_type="appointment",
        entity_id=appointment.id,
        after=catalog_service.to_audit_payload(data),
        detail=f"提交预约 {appointment.code}",
        request=request,
    )
    return data


def list_appointments(
    db: Session,
    user: User,
    *,
    status: str | None = None,
    elder_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(Appointment)
    stmt = apply_scope(stmt, db, user)
    if status:
        stmt = stmt.where(Appointment.status == status)
    if elder_id is not None:
        stmt = stmt.where(Appointment.elder_id == elder_id)
    low = to_naive(date_from)
    high = to_naive(date_to)
    if low is not None:
        stmt = stmt.where(Appointment.desired_start >= low)
    if high is not None:
        stmt = stmt.where(Appointment.desired_start <= high)

    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    page = max(1, int(page))
    size = max(1, int(size))
    rows = (
        db.execute(
            stmt.order_by(Appointment.desired_start.desc(), Appointment.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        .scalars()
        .all()
    )
    return [appointment_to_dict(db, row) for row in rows], total


def get_appointment_detail(db: Session, appointment_id: int, user: User) -> dict[str, Any]:
    appointment = get_or_404(db, appointment_id)
    ensure_visible(db, user, appointment)
    return appointment_to_dict(db, appointment, with_logs=True)


# ---------------- 状态流转 ----------------
def accept_appointment(
    db: Session,
    appointment_id: int,
    *,
    user: User | None,
    remark: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    appointment = get_or_404(db, appointment_id)
    if user is not None:
        ensure_visible(db, user, appointment)
    before = {"status": appointment.status}
    transition_appointment(db, appointment, "accept", user=user, remark=remark or "受理预约")
    appointment.accepted_by = getattr(user, "id", None)
    db.flush()
    write_audit(
        db,
        user=user,
        action=AuditAction.UPDATE.value,
        entity_type="appointment",
        entity_id=appointment.id,
        before=before,
        after={"status": appointment.status},
        detail=f"受理预约 {appointment.code}",
        request=request,
    )
    db.commit()
    return appointment_to_dict(db, appointment, with_logs=True)


def to_dispatch_appointment(
    db: Session,
    appointment_id: int,
    *,
    user: User | None,
    remark: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    appointment = get_or_404(db, appointment_id)
    if user is not None:
        ensure_visible(db, user, appointment)
    before = {"status": appointment.status}
    transition_appointment(db, appointment, "to_dispatch", user=user, remark=remark or "转待派单")
    write_audit(
        db,
        user=user,
        action=AuditAction.UPDATE.value,
        entity_type="appointment",
        entity_id=appointment.id,
        before=before,
        after={"status": appointment.status},
        detail=f"预约转待派单 {appointment.code}",
        request=request,
    )
    db.commit()
    return appointment_to_dict(db, appointment, with_logs=True)


def no_show_appointment(
    db: Session, appointment_id: int, *, user: User | None,
    reason: str | None = None, request: Request | None = None,
) -> dict[str, Any]:
    with db.begin_nested():
        data = _no_show_appointment(db, appointment_id, user=user, reason=reason, request=request)
    db.commit()
    return data


def _no_show_appointment(
    db: Session,
    appointment_id: int,
    *,
    user: User | None,
    reason: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """标记爽约并释放关联工单，不计费，记录状态日志与审计。"""
    reason = str(reason or "").strip()
    if not reason:
        raise RuleViolation("爽约原因必填")
    appointment = get_or_404(db, appointment_id)
    if user is not None:
        ensure_visible(db, user, appointment)
    before = {"status": appointment.status}
    transition_appointment(db, appointment, "no_show", user=user, remark=reason or "老人爽约")
    appointment.cancel_reason = reason[:255]
    released = _cancel_linked_orders(db, appointment, user=user, reason=f"爽约：{reason}", request=request)
    write_audit(
        db,
        user=user,
        action=AuditAction.UPDATE.value,
        entity_type="appointment",
        entity_id=appointment.id,
        before=before,
        after={"status": appointment.status, "reason": reason},
        detail=f"预约爽约 {appointment.code}",
        request=request,
    )
    data = appointment_to_dict(db, appointment, with_logs=True)
    data["released_order_count"] = released
    return data


def cancel_appointment(
    db: Session, appointment_id: int, *, reason: str,
    user: User | None, request: Request | None = None,
) -> dict[str, Any]:
    """预约、关联工单、费用、状态日志和审计在同一保存点内变更。"""
    with db.begin_nested():
        data = _cancel_appointment(db, appointment_id, reason=reason, user=user, request=request)
    db.commit()
    return data


def _cancel_appointment(
    db: Session,
    appointment_id: int,
    *,
    reason: str,
    user: User | None,
    request: Request | None = None,
) -> dict[str, Any]:
    """取消预约：reason 必填；距服务开始 < cancel_free_hours 小时则计费。

    已派单取工单价格快照；未派单取当前项目价格。
    """
    reason_text = str(reason or "").strip()
    if not reason_text:
        raise RuleViolation("取消原因必填")
    appointment = get_or_404(db, appointment_id)
    if user is not None:
        ensure_visible(db, user, appointment)

    APPOINTMENT.next(appointment.status, "cancel")
    item = db.get(ServiceItem, appointment.service_item_id)
    hours_left = (appointment.desired_start - datetime.now()).total_seconds() / 3600.0
    free_hours = float(settings.cancel_free_hours)
    chargeable = hours_left < free_hours
    fee = catalog_service.current_price(db, item) if (chargeable and item is not None) else Decimal("0.00")
    active_order = db.scalars(select(WorkOrder).where(
        WorkOrder.appointment_id == appointment.id,
        WorkOrder.status != OrderStatus.CANCELLED.value,
    ).order_by(WorkOrder.id.desc())).first()
    if chargeable and active_order is not None:
        fee = catalog_service.q2(active_order.amount)

    before = {
        "status": appointment.status,
        "is_chargeable_cancel": appointment.is_chargeable_cancel,
        "cancellation_fee": float(appointment.cancellation_fee or 0),
    }
    from_status = appointment.status
    appointment.is_chargeable_cancel = chargeable
    appointment.cancellation_fee = fee
    appointment.cancel_reason = reason_text[:255]
    to_status = transition_appointment(db, appointment, "cancel", user=user, remark=reason_text)
    released = _cancel_linked_orders(db, appointment, user=user, reason=reason_text, request=request)
    write_audit(
        db,
        user=user,
        action=AuditAction.UPDATE.value,
        entity_type="appointment",
        entity_id=appointment.id,
        before=before,
        after={
            "status": to_status,
            "is_chargeable_cancel": chargeable,
            "cancellation_fee": float(fee),
            "cancel_reason": reason_text,
            "hours_left": round(hours_left, 4),
        },
        detail=(
            f"取消预约 {appointment.code}：距服务开始 {hours_left:.2f} 小时，"
            + (f"计费 {fee} 元" if chargeable else "免费取消")
        ),
        request=request,
    )
    data = appointment_to_dict(db, appointment, with_logs=True)
    data["hours_to_start"] = round(hours_left, 4)
    data["free_hours"] = free_hours
    data["from_status"] = from_status
    data["released_order_count"] = released
    return data


# ---------------- 供其它模块复用 ----------------
def _cancel_linked_orders(
    db: Session, appointment: Appointment, *, user: User | None, reason: str, request: Request | None,
) -> int:
    orders = db.scalars(select(WorkOrder).where(
        WorkOrder.appointment_id == appointment.id,
        WorkOrder.status != OrderStatus.CANCELLED.value,
    )).all()
    # 全部确认可取消后再写入，异常由上层保存点回滚。
    targets = [ORDER.next(order.status, "cancel") for order in orders]
    for order, target in zip(orders, targets, strict=True):
        previous = order.status
        order.status = target
        order.summary = f"预约结束：{reason}"
        write_audit(db, user=user, action=AuditAction.UPDATE.value, entity_type="work_order",
                    entity_id=order.id, before={"status": previous}, after={"status": order.status},
                    detail=f"预约 {appointment.code} 结束，释放护理员时段：{reason}", request=request)
    return len(orders)


def requeue_appointment(
    db: Session,
    appointment: Appointment,
    *,
    reason: str,
    operator_id: int | None = None,
) -> None:
    """把已派单预约按状态机回退到待派单，禁止重开已结束的预约。"""
    if str(appointment.status) == AppointmentStatus.PENDING_DISPATCH.value:
        return
    from_status = appointment.status
    to_status = APPOINTMENT.next(from_status, "requeue")
    appointment.status = to_status
    write_log(
        db,
        appointment,
        from_status=from_status,
        to_status=to_status,
        action="requeue",
        operator_id=operator_id,
        remark=reason,
    )
    db.flush()
