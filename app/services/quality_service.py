"""评价、回访、投诉与绩效服务（C 模块）。

覆盖：
- 评价：4 项 1-5 星 + 综合均分（2 位小数），工单 pending_evaluation → completed，
  回写护理员近 30 天评分缓存；均分 ≤3 自动生成 48 小时内回访任务（CallbackTask）；
- 回访：待办列表（含超时标记）、完成回访（可转投诉）；
- 投诉：登记 / 指派 / 复核结论（成立可退费 / 换人 / 扣分），
  同一护理员当月累计 3 次成立 → Staff.is_focus=True；
- 绩效：按月份实时聚合平均评分、准时率、异常率、完成单量并排名（同时回写月度绩效表）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.deps import effective_scope, get_elder_of_user, get_staff, visible_elder_ids
from app.core.enums import (
    CallbackStatus,
    ComplaintStatus,
    DataScope,
    OrderStatus,
    RoleCode,
)
from app.core.exceptions import NotFound, RuleViolation
from app.core.state_machine import COMPLAINT, ORDER
from app.db.base import utcnow
from app.models import (
    CallbackTask,
    Complaint,
    Elder,
    Evaluation,
    Notification,
    Org,
    Role,
    Staff,
    StaffPerformanceMonthly,
    User,
    WorkOrder,
)
from app.services.order_service import ensure_order_visible, get_order, order_scope_condition

CALLBACK_HOURS = 48
PUNCTUALITY_GRACE_MIN = 10
LOW_SCORE_THRESHOLD = 3.0
FOCUS_COMPLAINT_THRESHOLD = 3
DEDUCT_POINTS = 2

STAR_LABELS = {
    "punctuality": "准时性",
    "attitude": "服务态度",
    "professionalism": "专业程度",
    "effect": "服务效果",
}


# ---------------------------------------------------------------- 通用
def period_range(period: str | None) -> tuple[str, datetime, datetime]:
    """把 YYYY-MM 解析为 [start, end)，非法格式抛 RuleViolation。"""
    text = (period or "").strip() or utcnow().strftime("%Y-%m")
    parts = text.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise RuleViolation("period 格式应为 YYYY-MM")
    try:
        year, month = int(parts[0]), int(parts[1])
        start = datetime(year, month, 1)
    except ValueError as exc:
        raise RuleViolation("period 格式应为 YYYY-MM") from exc
    if not 1 <= month <= 12:
        raise RuleViolation("period 格式应为 YYYY-MM")
    normalized = f"{year:04d}-{month:02d}"
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return normalized, start, end


def _resolve_community_org_id(db: Session, org_id: int | None) -> int | None:
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


def _next_code(db: Session, model: Any, prefix: str) -> str:
    base = f"{prefix}{utcnow():%Y%m%d%H%M%S}"
    code = base
    seq = 1
    while db.execute(select(model.id).where(model.code == code)).scalar_one_or_none() is not None:
        code = f"{base}-{seq:02d}"[:32]
        seq += 1
        if seq > 99:
            code = f"{prefix}{uuid.uuid4().hex[:10].upper()}"[:32]
            break
    return code


def _find_community_manager_id(db: Session, community_org_id: int | None) -> int | None:
    """回访任务默认派给社区管理员（无则退到社区网格员）。"""
    if community_org_id:
        user_id = db.execute(
            select(User.id)
            .join(Role, User.role_id == Role.id)
            .where(
                Role.code == RoleCode.ORG_ADMIN,
                User.org_id == community_org_id,
                User.is_active.is_(True),
            )
            .order_by(User.id.asc())
            .limit(1)
        ).scalar_one_or_none()
        if user_id is not None:
            return int(user_id)
        grid_org_ids = [
            int(x) for x in db.execute(select(Org.id).where(Org.parent_id == community_org_id)).scalars().all()
        ]
        if grid_org_ids:
            user_id = db.execute(
                select(User.id)
                .join(Role, User.role_id == Role.id)
                .where(
                    Role.code == RoleCode.GRID,
                    User.org_id.in_(grid_org_ids),
                    User.is_active.is_(True),
                )
                .order_by(User.id.asc())
                .limit(1)
            ).scalar_one_or_none()
            if user_id is not None:
                return int(user_id)
    return None


def _notify(db: Session, user_ids: list[int], *, title: str, content: str,
            related_type: str | None = None, related_id: int | None = None) -> int:
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


# ---------------------------------------------------------------- 数据范围
def _evaluation_scope_condition(db: Session, user: User):
    role_code = user.role.code if user.role else None
    scope = effective_scope(user)
    if role_code in (RoleCode.SUPER, RoleCode.STREET) or scope == DataScope.STREET:
        return None
    if role_code == RoleCode.NURSE:
        staff = get_staff(db, user)
        return Evaluation.nurse_id == (staff.id if staff is not None else -1)
    if role_code == RoleCode.ELDER:
        elder = get_elder_of_user(db, user)
        return Evaluation.elder_id == (elder.id if elder is not None else -1)
    community_id = _resolve_community_org_id(db, user.org_id)
    if community_id is not None:
        return Evaluation.work_order_id.in_(select(WorkOrder.id).where(WorkOrder.community_org_id == community_id))
    return None


def _complaint_scope_condition(db: Session, user: User):
    role_code = user.role.code if user.role else None
    scope = effective_scope(user)
    if role_code in (RoleCode.SUPER, RoleCode.STREET) or scope == DataScope.STREET:
        return None
    if role_code == RoleCode.NURSE:
        staff = get_staff(db, user)
        return Complaint.nurse_id == (staff.id if staff is not None else -1)
    if role_code == RoleCode.ELDER:
        elder = get_elder_of_user(db, user)
        return Complaint.elder_id == (elder.id if elder is not None else -1)
    community_id = _resolve_community_org_id(db, user.org_id)
    if community_id is not None:
        return Complaint.elder_id.in_(select(Elder.id).where(Elder.community_org_id == community_id))
    return None


# ---------------------------------------------------------------- 序列化
def serialize_evaluation(evaluation: Evaluation, elder: Elder | None = None,
                         staff: Staff | None = None) -> dict[str, Any]:
    return {
        "id": evaluation.id,
        "work_order_id": evaluation.work_order_id,
        "elder_id": evaluation.elder_id,
        "elder_name": elder.name if elder is not None else None,
        "nurse_id": evaluation.nurse_id,
        "nurse_name": staff.name if staff is not None else None,
        "punctuality": evaluation.punctuality,
        "attitude": evaluation.attitude,
        "professionalism": evaluation.professionalism,
        "effect": evaluation.effect,
        "avg_score": evaluation.avg_score,
        "comment": evaluation.comment,
        "rated_by": evaluation.rated_by,
        "created_at": evaluation.created_at,
    }


def serialize_callback(db: Session, callback: CallbackTask, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    pending = str(callback.status) == CallbackStatus.PENDING
    overdue = bool(pending and callback.due_at is not None and callback.due_at < now)
    overdue_minutes = (
        max(0, int((now - callback.due_at).total_seconds() // 60)) if overdue and callback.due_at else 0
    )
    remain_minutes = (
        max(0, int((callback.due_at - now).total_seconds() // 60))
        if pending and callback.due_at is not None and callback.due_at >= now
        else 0
    )
    elder = db.get(Elder, callback.elder_id) if callback.elder_id else None
    evaluation = db.get(Evaluation, callback.evaluation_id) if callback.evaluation_id else None
    assignee = db.get(User, callback.assigned_to) if callback.assigned_to else None
    return {
        "id": callback.id,
        "evaluation_id": callback.evaluation_id,
        "complaint_id": callback.complaint_id,
        "elder_id": callback.elder_id,
        "elder_name": elder.name if elder is not None else None,
        "assigned_to": callback.assigned_to,
        "assigned_name": assignee.real_name if assignee is not None else None,
        "due_at": callback.due_at,
        "status": str(callback.status),
        "result": callback.result,
        "note": callback.note,
        "finished_at": callback.finished_at,
        "overdue": overdue,
        "overdue_minutes": overdue_minutes,
        "remain_minutes": remain_minutes,
        "avg_score": evaluation.avg_score if evaluation is not None else None,
    }


def serialize_complaint(db: Session, complaint: Complaint) -> dict[str, Any]:
    elder = db.get(Elder, complaint.elder_id) if complaint.elder_id else None
    nurse = db.get(Staff, complaint.nurse_id) if complaint.nurse_id else None
    order = db.get(WorkOrder, complaint.work_order_id) if complaint.work_order_id else None
    handler = db.get(User, complaint.handler_id) if complaint.handler_id else None
    return {
        "id": complaint.id,
        "code": complaint.code,
        "elder_id": complaint.elder_id,
        "elder_name": elder.name if elder is not None else None,
        "work_order_id": complaint.work_order_id,
        "work_order_code": order.code if order is not None else None,
        "nurse_id": complaint.nurse_id,
        "nurse_name": nurse.name if nurse is not None else None,
        "content": complaint.content,
        "status": str(complaint.status),
        "available_actions": COMPLAINT.actions(str(complaint.status)),
        "handler_id": complaint.handler_id,
        "handler_name": handler.real_name if handler is not None else None,
        "institution_statement": complaint.institution_statement,
        "conclusion": complaint.conclusion,
        "is_established": bool(complaint.is_established),
        "resolution": complaint.resolution,
        "refund_bill_id": complaint.refund_bill_id,
        "concluded_at": complaint.concluded_at,
        "created_at": complaint.created_at,
    }


# ---------------------------------------------------------------- 评价
def create_evaluation(
    db: Session,
    user: User,
    *,
    work_order_id: int,
    punctuality: int,
    attitude: int,
    professionalism: int,
    effect: int,
    comment: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    scores = {
        "punctuality": punctuality,
        "attitude": attitude,
        "professionalism": professionalism,
        "effect": effect,
    }
    for field, value in scores.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuleViolation(f"{STAR_LABELS[field]}评分必须是 1-5 的整数")
        if value < 1 or value > 5:
            raise RuleViolation(f"{STAR_LABELS[field]}评分必须在 1-5 之间")

    order = get_order(db, work_order_id)
    ensure_order_visible(db, user, order)
    if str(order.status) != OrderStatus.PENDING_EVALUATION:
        raise RuleViolation(f"工单当前状态为 {order.status}，不在待评价状态，无法提交评价")

    now = utcnow()
    avg_score = round(sum(scores.values()) / len(scores), 2)
    evaluation = Evaluation(
        work_order_id=order.id,
        elder_id=order.elder_id,
        nurse_id=order.nurse_id,
        punctuality=punctuality,
        attitude=attitude,
        professionalism=professionalism,
        effect=effect,
        avg_score=avg_score,
        comment=comment,
        rated_by=user.id,
    )
    db.add(evaluation)
    db.flush()

    order.status = ORDER.next(order.status, "complete")
    order.completed_at = now
    order.rating = avg_score
    db.flush()

    staff = db.get(Staff, order.nurse_id) if order.nurse_id else None
    rating_30d = None
    if staff is not None:
        since = now - timedelta(days=30)
        rating_30d = db.execute(
            select(func.avg(Evaluation.avg_score)).where(
                Evaluation.nurse_id == staff.id,
                Evaluation.created_at >= since,
            )
        ).scalar()
        if rating_30d is not None:
            staff.rating_cached = round(float(rating_30d), 2)
        staff.order_count_cached = int(
            db.scalar(
                select(func.count())
                .select_from(WorkOrder)
                .where(WorkOrder.nurse_id == staff.id, WorkOrder.status == OrderStatus.COMPLETED)
            )
            or 0
        )

    need_callback = avg_score <= LOW_SCORE_THRESHOLD
    callback: CallbackTask | None = None
    if need_callback:
        callback = CallbackTask(
            evaluation_id=evaluation.id,
            elder_id=order.elder_id,
            assigned_to=_find_community_manager_id(db, order.community_org_id),
            due_at=now + timedelta(hours=CALLBACK_HOURS),
            status=CallbackStatus.PENDING,
        )
        db.add(callback)
        db.flush()

    write_audit(
        db,
        user=user,
        action="create",
        entity_type="evaluation",
        entity_id=evaluation.id,
        after={
            "work_order_id": order.id,
            "avg_score": avg_score,
            "need_callback": need_callback,
            "nurse_id": order.nurse_id,
        },
        request=request,
    )
    db.commit()
    return {
        "evaluation": serialize_evaluation(evaluation),
        "order": {
            "id": order.id,
            "code": order.code,
            "status": str(order.status),
            "completed_at": order.completed_at,
            "rating": order.rating,
        },
        "nurse": (
            {"id": staff.id, "rating_cached": staff.rating_cached, "order_count_cached": staff.order_count_cached}
            if staff is not None
            else None
        ),
        "rating_30d": round(float(rating_30d), 2) if rating_30d is not None else None,
        "need_callback": need_callback,
        "callback_task_id": callback.id if callback is not None else None,
        "callback_due_at": callback.due_at if callback is not None else None,
        "callback_task": (
            {
                "id": callback.id,
                "evaluation_id": callback.evaluation_id,
                "elder_id": callback.elder_id,
                "assigned_to": callback.assigned_to,
                "due_at": callback.due_at,
                "status": str(callback.status),
            }
            if callback is not None
            else None
        ),
    }


def list_evaluations(
    db: Session,
    user: User,
    *,
    nurse_id: int | None = None,
    work_order_id: int | None = None,
    elder_id: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(Evaluation)
    if nurse_id:
        stmt = stmt.where(Evaluation.nurse_id == nurse_id)
    if work_order_id:
        stmt = stmt.where(Evaluation.work_order_id == work_order_id)
    if elder_id:
        stmt = stmt.where(Evaluation.elder_id == elder_id)
    cond = _evaluation_scope_condition(db, user)
    if cond is not None:
        stmt = stmt.where(cond)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        db.execute(
            stmt.order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        .scalars()
        .all()
    )
    elder_ids = {e.elder_id for e in rows if e.elder_id}
    nurse_ids = {e.nurse_id for e in rows if e.nurse_id}
    elders = (
        {x.id: x for x in db.execute(select(Elder).where(Elder.id.in_(elder_ids))).scalars().all()}
        if elder_ids
        else {}
    )
    staffs = (
        {x.id: x for x in db.execute(select(Staff).where(Staff.id.in_(nurse_ids))).scalars().all()}
        if nurse_ids
        else {}
    )
    return [serialize_evaluation(e, elders.get(e.elder_id), staffs.get(e.nurse_id)) for e in rows], total


# ---------------------------------------------------------------- 回访
def list_callbacks(
    db: Session,
    user: User,
    *,
    status: str | None = "pending",
    elder_id: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int, int]:
    stmt = select(CallbackTask)
    if status and status != "all":
        stmt = stmt.where(CallbackTask.status == status)
    if elder_id:
        stmt = stmt.where(CallbackTask.elder_id == elder_id)
    elder_ids = visible_elder_ids(db, user)
    if elder_ids is not None:
        stmt = stmt.where(CallbackTask.elder_id.in_(elder_ids))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        db.execute(
            stmt.order_by(CallbackTask.due_at.asc(), CallbackTask.id.asc()).offset((page - 1) * size).limit(size)
        )
        .scalars()
        .all()
    )
    now = utcnow()
    items = [serialize_callback(db, row, now) for row in rows]
    overdue_count = sum(1 for item in items if item["overdue"])
    return items, total, overdue_count


def finish_callback(
    db: Session,
    user: User,
    callback_id: int,
    *,
    result: str,
    note: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    callback = db.get(CallbackTask, callback_id)
    if callback is None:
        raise NotFound("回访任务不存在")
    if str(callback.status) != CallbackStatus.PENDING:
        raise RuleViolation("该回访任务已完成，不能重复处理")
    if result not in ("improve", "to_complaint"):
        raise RuleViolation("回访结果只能是 improve（已改善）或 to_complaint（转投诉）")

    now = utcnow()
    evaluation = db.get(Evaluation, callback.evaluation_id) if callback.evaluation_id else None
    complaint: Complaint | None = None
    if result == "to_complaint":
        content = (note or "").strip() or (
            f"低评分回访转投诉（均分 {evaluation.avg_score}）" if evaluation is not None else "回访转投诉"
        )
        complaint = _create_complaint(
            db,
            user,
            elder_id=callback.elder_id,
            work_order_id=evaluation.work_order_id if evaluation is not None else None,
            nurse_id=evaluation.nurse_id if evaluation is not None else None,
            content=content,
        )
        callback.complaint_id = complaint.id

    callback.status = CallbackStatus.DONE
    callback.result = result
    callback.note = note
    callback.finished_at = now
    write_audit(
        db,
        user=user,
        action="update",
        entity_type="callback_task",
        entity_id=callback.id,
        after={"result": result, "note": note, "complaint_id": callback.complaint_id},
        detail="完成回访",
        request=request,
    )
    db.commit()
    data = serialize_callback(db, callback, now)
    data["complaint"] = serialize_complaint(db, complaint) if complaint is not None else None
    return data


# ---------------------------------------------------------------- 投诉
def _create_complaint(
    db: Session,
    user: User | None,
    *,
    elder_id: int,
    work_order_id: int | None = None,
    nurse_id: int | None = None,
    content: str,
) -> Complaint:
    elder = db.get(Elder, elder_id)
    if elder is None:
        raise NotFound("老人档案不存在")
    order = db.get(WorkOrder, work_order_id) if work_order_id else None
    if work_order_id and order is None:
        raise NotFound("关联工单不存在")
    if nurse_id is None and order is not None:
        nurse_id = order.nurse_id
    complaint = Complaint(
        code=_next_code(db, Complaint, "CP"),
        elder_id=elder.id,
        work_order_id=order.id if order is not None else None,
        nurse_id=nurse_id,
        content=content.strip(),
        status=ComplaintStatus.NEW,
    )
    db.add(complaint)
    db.flush()
    return complaint


def create_complaint(
    db: Session,
    user: User,
    *,
    elder_id: int,
    content: str,
    work_order_id: int | None = None,
    nurse_id: int | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    clean_content = (content or "").strip()
    if not clean_content:
        raise RuleViolation("投诉内容不能为空")
    complaint = _create_complaint(
        db,
        user,
        elder_id=elder_id,
        work_order_id=work_order_id,
        nurse_id=nurse_id,
        content=clean_content,
    )
    handler_ids: list[int] = []
    elder = db.get(Elder, elder_id)
    if elder is not None and elder.community_org_id:
        origin = _find_community_manager_id(db, elder.community_org_id)
        if origin:
            handler_ids.append(origin)
    _notify(
        db,
        handler_ids,
        title="收到新的服务投诉",
        content=f"投诉单 {complaint.code} 待指派处理：{clean_content[:80]}",
        related_type="complaint",
        related_id=complaint.id,
    )
    write_audit(
        db,
        user=user,
        action="create",
        entity_type="complaint",
        entity_id=complaint.id,
        after={"elder_id": elder_id, "work_order_id": work_order_id, "nurse_id": complaint.nurse_id},
        detail="投诉登记",
        request=request,
    )
    db.commit()
    return serialize_complaint(db, complaint)


def list_complaints(
    db: Session,
    user: User,
    *,
    status: str | None = None,
    nurse_id: int | None = None,
    elder_id: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(Complaint)
    if status:
        stmt = stmt.where(Complaint.status == status)
    if nurse_id:
        stmt = stmt.where(Complaint.nurse_id == nurse_id)
    if elder_id:
        stmt = stmt.where(Complaint.elder_id == elder_id)
    cond = _complaint_scope_condition(db, user)
    if cond is not None:
        stmt = stmt.where(cond)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        db.execute(
            stmt.order_by(Complaint.created_at.desc(), Complaint.id.desc()).offset((page - 1) * size).limit(size)
        )
        .scalars()
        .all()
    )
    return [serialize_complaint(db, row) for row in rows], total


def assign_complaint(
    db: Session,
    user: User,
    complaint_id: int,
    *,
    handler_id: int,
    request: Request | None = None,
) -> dict[str, Any]:
    complaint = db.get(Complaint, complaint_id)
    if complaint is None:
        raise NotFound("投诉单不存在")
    handler = db.get(User, handler_id)
    if handler is None:
        raise NotFound("处置人不存在")
    if not handler.is_active:
        raise RuleViolation("处置人账号已停用")
    before = {"status": str(complaint.status), "handler_id": complaint.handler_id}
    complaint.status = COMPLAINT.next(complaint.status, "assign")
    complaint.handler_id = handler.id
    _notify(
        db,
        [handler.id],
        title="投诉单已指派给你",
        content=f"投诉单 {complaint.code} 已指派，请及时处理",
        related_type="complaint",
        related_id=complaint.id,
    )
    write_audit(
        db,
        user=user,
        action="update",
        entity_type="complaint",
        entity_id=complaint.id,
        before=before,
        after={"status": str(complaint.status), "handler_id": complaint.handler_id},
        detail="投诉指派",
        request=request,
    )
    db.commit()
    return serialize_complaint(db, complaint)


def _try_refund(db: Session, complaint: Complaint, user: User) -> dict[str, Any]:
    """尝试调用结算模块生成退费红冲；模块未就绪 / 无可退账单时降级登记。"""
    order = db.get(WorkOrder, complaint.work_order_id) if complaint.work_order_id else None
    if order is None:
        return {
            "status": "skipped",
            "fallback": True,
            "amount": 0.0,
            "refund_bill_id": None,
            "reason": "投诉未关联工单，无法自动退费",
        }
    amount = Decimal(str(order.amount or order.unit_price or 0))
    try:
        from app.services.billing_service import create_refund
    except Exception:  # noqa: BLE001 - 结算模块未就绪时降级
        create_refund = None
    if create_refund is None:
        return {
            "status": "pending_manual",
            "fallback": True,
            "amount": float(amount),
            "refund_bill_id": None,
            "reason": "结算模块未就绪，已登记待人工退费",
        }
    reason = f"投诉退费：{complaint.code} {complaint.conclusion or ''}".strip()[:200]
    try:
        result = create_refund(db, order.id, amount, reason, user.id)
    except Exception as exc:  # noqa: BLE001 - 退费失败不阻断投诉结论
        return {
            "status": "failed",
            "fallback": True,
            "amount": float(amount),
            "refund_bill_id": None,
            "error": str(exc),
        }
    bill_id = None
    if isinstance(result, dict):
        bill_id = result.get("bill_id") or result.get("id")
        if bill_id is None and isinstance(result.get("bill"), dict):
            bill_id = result["bill"].get("id")
    if bill_id is not None:
        try:
            complaint.refund_bill_id = int(bill_id)
        except (TypeError, ValueError):  # pragma: no cover - 明细异常时不影响结论
            complaint.refund_bill_id = None
    # 结算模块返回 ok=False 表示无账单可红冲（如未出账），非模块缺失
    if isinstance(result, dict) and result.get("ok") is False:
        return {
            "status": "skipped",
            "fallback": False,
            "amount": float(amount),
            "bill_id": complaint.refund_bill_id,
            "refund_bill_id": complaint.refund_bill_id,
            "reason": result.get("reason"),
            "detail": result,
        }
    return {
        "status": "created",
        "fallback": False,
        "amount": float(amount),
        "bill_id": complaint.refund_bill_id,
        "refund_bill_id": complaint.refund_bill_id,
        "detail": result if isinstance(result, dict) else str(result),
    }


def conclude_complaint(
    db: Session,
    user: User,
    complaint_id: int,
    *,
    conclusion: str,
    is_established: bool,
    resolution: str = "none",
    request: Request | None = None,
) -> dict[str, Any]:
    if resolution not in ("refund", "change_nurse", "deduct", "none"):
        raise RuleViolation("处理方式只能是 refund/change_nurse/deduct/none")
    complaint = db.get(Complaint, complaint_id)
    if complaint is None:
        raise NotFound("投诉单不存在")
    clean_conclusion = (conclusion or "").strip()
    if not clean_conclusion:
        raise RuleViolation("投诉结论不能为空")
    if str(complaint.status) in (ComplaintStatus.CONCLUDED, ComplaintStatus.ARCHIVED):
        raise RuleViolation("投诉已处置完毕，不能重复结案")
    before_status = str(complaint.status)

    # 状态机推进：new → assigned → reviewing → concluded
    if str(complaint.status) == ComplaintStatus.NEW:
        complaint.status = COMPLAINT.next(complaint.status, "assign")
        complaint.handler_id = complaint.handler_id or user.id
    if str(complaint.status) == ComplaintStatus.ASSIGNED:
        complaint.status = COMPLAINT.next(complaint.status, "review")
    complaint.status = COMPLAINT.next(complaint.status, "conclude")

    now = utcnow()
    complaint.conclusion = clean_conclusion
    complaint.is_established = bool(is_established)
    complaint.resolution = resolution if is_established else "none"
    complaint.concluded_at = now

    refund_info: dict[str, Any] | None = None
    focus_marked = False
    established_count = 0
    deduct_points = 0
    if is_established:
        if resolution == "refund":
            refund_info = _try_refund(db, complaint, user)
        elif resolution == "change_nurse":
            statement = (complaint.institution_statement or "") + f"[{now:%Y-%m-%d}] 换人处理：{clean_conclusion}"
            complaint.institution_statement = statement[:2000]
        elif resolution == "deduct":
            deduct_points = DEDUCT_POINTS
            statement = (complaint.institution_statement or "") + (
                f"[{now:%Y-%m-%d}] 服务扣分 {deduct_points} 分：{clean_conclusion}"
            )
            complaint.institution_statement = statement[:2000]
        db.flush()
        if complaint.nurse_id:
            _, start, end = period_range(now.strftime("%Y-%m"))
            established_count = int(
                db.scalar(
                    select(func.count())
                    .select_from(Complaint)
                    .where(
                        Complaint.nurse_id == complaint.nurse_id,
                        Complaint.is_established.is_(True),
                        Complaint.concluded_at >= start,
                        Complaint.concluded_at < end,
                    )
                )
                or 0
            )
            if established_count >= FOCUS_COMPLAINT_THRESHOLD:
                staff = db.get(Staff, complaint.nurse_id)
                if staff is not None and not staff.is_focus:
                    staff.is_focus = True
                focus_marked = True

    write_audit(
        db,
        user=user,
        action="refund" if resolution == "refund" and is_established else "update",
        entity_type="complaint",
        entity_id=complaint.id,
        before={"status": before_status},
        after={
            "conclusion": clean_conclusion,
            "is_established": bool(is_established),
            "resolution": complaint.resolution,
            "established_count": established_count,
            "deduct_points": deduct_points,
            "focus_marked": focus_marked,
            "refund": refund_info,
        },
        detail="投诉结论",
        request=request,
    )
    db.commit()
    nurse_staff = db.get(Staff, complaint.nurse_id) if complaint.nurse_id else None
    return {
        "complaint": serialize_complaint(db, complaint),
        "complaint_id": complaint.id,
        "status": str(complaint.status),
        "is_established": bool(complaint.is_established),
        "resolution": complaint.resolution,
        "refund": refund_info,
        "established_count": established_count,
        "focus_marked": focus_marked,
        "deduct_points": deduct_points,
        "nurse_is_focus": bool(nurse_staff.is_focus) if nurse_staff is not None else False,
    }


# ---------------------------------------------------------------- 绩效
def performance_board(db: Session, user: User, period: str | None = None) -> dict[str, Any]:
    normalized, start, end = period_range(period)
    notes: list[str] = []

    stmt = select(WorkOrder).where(
        WorkOrder.scheduled_start >= start,
        WorkOrder.scheduled_start < end,
        WorkOrder.nurse_id.is_not(None),
    )
    cond = order_scope_condition(db, user)
    if cond is not None:
        stmt = stmt.where(cond)
    orders = list(db.execute(stmt).scalars().all())

    order_ids = [o.id for o in orders]
    evaluations = (
        list(
            db.execute(select(Evaluation).where(Evaluation.work_order_id.in_(order_ids))).scalars().all()
        )
        if order_ids
        else []
    )

    stats: dict[int, dict[str, Any]] = {}
    for order in orders:
        if order.nurse_id is None:
            continue
        bucket = stats.setdefault(
            int(order.nurse_id),
            {
                "order_count": 0,
                "completed_count": 0,
                "anomaly_count": 0,
                "checkin_count": 0,
                "punctual_count": 0,
                "scores": [],
                "complaint_count": 0,
            },
        )
        bucket["order_count"] += 1
        if str(order.status) == OrderStatus.COMPLETED:
            bucket["completed_count"] += 1
        if str(order.status) == OrderStatus.ANOMALY:
            bucket["anomaly_count"] += 1
        if order.checkin_at is not None:
            bucket["checkin_count"] += 1
            if order.checkin_at <= order.scheduled_start + timedelta(minutes=PUNCTUALITY_GRACE_MIN):
                bucket["punctual_count"] += 1

    for evaluation in evaluations:
        if evaluation.nurse_id in stats:
            stats[int(evaluation.nurse_id)]["scores"].append(evaluation.avg_score)

    if stats:
        complaint_rows = list(
            db.execute(
                select(Complaint).where(
                    Complaint.created_at >= start,
                    Complaint.created_at < end,
                    Complaint.nurse_id.in_(list(stats.keys())),
                )
            )
            .scalars()
            .all()
        )
        for row in complaint_rows:
            if row.nurse_id in stats:
                stats[int(row.nurse_id)]["complaint_count"] += 1

    staffs = (
        {s.id: s for s in db.execute(select(Staff).where(Staff.id.in_(list(stats.keys())))).scalars().all()}
        if stats
        else {}
    )
    rows: list[dict[str, Any]] = []
    for nurse_id, bucket in stats.items():
        staff = staffs.get(nurse_id)
        scores = bucket["scores"]
        avg_score = round(sum(scores) / len(scores), 2) if scores else 0.0
        punctuality_rate = (
            round(bucket["punctual_count"] / bucket["checkin_count"], 4) if bucket["checkin_count"] else 0.0
        )
        anomaly_rate = round(bucket["anomaly_count"] / bucket["order_count"], 4) if bucket["order_count"] else 0.0
        rows.append(
            {
                "period": normalized,
                "nurse_id": nurse_id,
                "nurse_name": staff.name if staff is not None else None,
                "employee_no": staff.employee_no if staff is not None else None,
                "org_id": staff.org_id if staff is not None else None,
                "avg_score": avg_score,
                "punctuality_rate": punctuality_rate,
                "anomaly_rate": anomaly_rate,
                "order_count": bucket["completed_count"],
                "scheduled_count": bucket["order_count"],
                "complaint_count": bucket["complaint_count"],
                "is_focus": bool(staff.is_focus) if staff is not None else False,
            }
        )

    rows.sort(key=lambda r: (-r["avg_score"], -r["order_count"], r["nurse_id"]))
    for index, row in enumerate(rows, start=1):
        row["rank_no"] = index

    for row in rows:
        record = db.execute(
            select(StaffPerformanceMonthly).where(
                StaffPerformanceMonthly.staff_id == row["nurse_id"],
                StaffPerformanceMonthly.period == normalized,
            )
        ).scalar_one_or_none()
        if record is None:
            record = StaffPerformanceMonthly(staff_id=row["nurse_id"], period=normalized)
            db.add(record)
        record.avg_score = row["avg_score"]
        record.punctuality_rate = row["punctuality_rate"]
        record.anomaly_rate = row["anomaly_rate"]
        record.order_count = row["order_count"]
        record.complaint_count = row["complaint_count"]
        record.rank_no = row["rank_no"]
    db.commit()

    if not rows:
        notes.append("该时间段内没有可统计的工单数据")
    return {
        "period": normalized,
        "items": rows,
        "ranking": rows,
        "total": len(rows),
        "punctuality_grace_minutes": PUNCTUALITY_GRACE_MIN,
        "notes": notes,
    }
