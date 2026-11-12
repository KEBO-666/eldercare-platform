"""费用结算与补贴核算服务（E1）。

职责：
- 月度账单生成：按工单价格快照计价 → 补贴分账（跨明细累计上限）→ 四类对账差异；
- 账单流转：submit → confirm（>1% 差异强制复核说明）→ pay（三方分账登记）→ reverse（红冲）；
- 对账差异：清单查询与处理留痕；
- 退费红冲：`create_refund` 供投诉模块调用，永不抛未捕获异常；
- Excel 导出（openpyxl + 审计）。
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi import Request
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.algorithms.subsidy import DEFAULT_POLICY_MATRIX, money, pick_policy, split_amount
from app.core.audit import write_audit
from app.core.config import settings
from app.core.deps import (
    apply_scope,
    effective_scope,
    get_elder_of_user,
    visible_elder_ids,
)
from app.core.enums import (
    AuditAction,
    BillStatus,
    DataScope,
    DiffStatus,
    OrderStatus,
    RoleCode,
)
from app.core.exceptions import BadRequest, Conflict, Forbidden, NotFound, RuleViolation
from app.core.export import safe_cell
from app.core.state_machine import BILL
from app.db.base import utcnow
from app.models import (
    Bill,
    BillDetail,
    Elder,
    PaymentRecord,
    ReconciliationDiff,
    ServiceItem,
    SubsidyPolicy,
    User,
    WorkOrder,
)

logger = logging.getLogger("eldercare.billing")

PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DURATION_FACTOR = 3
ZERO = Decimal("0.00")

RULE_DUPLICATE = "duplicate"
RULE_SUBSIDY_LIMIT = "subsidy_limit"
RULE_UNFINISHED = "unfinished"
RULE_DURATION = "duration"

RULE_NAMES = {
    RULE_DUPLICATE: "重复计费",
    RULE_SUBSIDY_LIMIT: "补贴超限",
    RULE_UNFINISHED: "未完成计费",
    RULE_DURATION: "时长异常",
}

BILL_STATUS_NAMES = {
    BillStatus.DRAFT.value: "草稿",
    BillStatus.PENDING_REVIEW.value: "待复核",
    BillStatus.CONFIRMED.value: "已确认",
    BillStatus.PAID.value: "已缴费",
    BillStatus.REVERSED.value: "已红冲",
}

DIFF_STATUS_NAMES = {
    DiffStatus.OPEN.value: "待处理",
    DiffStatus.EXPLAINED.value: "已说明",
    DiffStatus.HANDLED.value: "已处理",
}

ORDER_STATUS_NAMES = {
    OrderStatus.PENDING_ACCEPT.value: "待接单",
    OrderStatus.ACCEPTED.value: "已接单",
    OrderStatus.IN_SERVICE.value: "服务中",
    OrderStatus.PENDING_EVALUATION.value: "待评价",
    OrderStatus.COMPLETED.value: "已完成",
    OrderStatus.ANOMALY.value: "异常",
    OrderStatus.CANCELLED.value: "已取消",
}

PAYER_NAMES = {"government": "政府补贴", "insurance": "长护险", "self": "个人自付"}
PAYER_ALIASES = {"government": "government", "insurance": "insurance", "self": "self", "self_pay": "self"}

# 视为「已发生却未完成」的工单状态（用于未完成计费差异）
UNFINISHED_ORDER_STATUSES = (
    OrderStatus.IN_SERVICE.value,
    OrderStatus.PENDING_EVALUATION.value,
    OrderStatus.ANOMALY.value,
)


# ================================================================ 通用工具
def month_range(period: str) -> tuple[datetime, datetime]:
    """把 "YYYY-MM" 转为 [月初, 次月初) 的 naive 时间区间。"""
    text = str(period or "").strip()
    if not PERIOD_PATTERN.match(text):
        raise BadRequest("账期格式应为 YYYY-MM")
    year, month = int(text[:4]), int(text[5:7])
    start = datetime(year, month, 1)
    end = datetime(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)
    return start, end


def _num(value: Any) -> float:
    return float(money(value))


def _scope(stmt: Select, model: Any, user: User | None, db: Session) -> Select:
    return apply_scope(stmt, model, user, db) if user is not None else stmt


def _bill_scope(stmt: Select, user: User | None, db: Session) -> Select:
    """账单数据范围：机构/网格/老人各自可见，超管与街道不受限。"""
    if user is None:
        return stmt
    role = user.role.code if getattr(user, "role", None) else None
    if str(role) == RoleCode.ELDER.value:
        elder = get_elder_of_user(db, user)
        if elder is None:
            return stmt.where(Bill.id.is_(None))
        return stmt.where(Bill.id.in_(select(BillDetail.bill_id).where(BillDetail.elder_id == elder.id)))
    if str(role) == RoleCode.GRID.value:
        elder_ids = visible_elder_ids(db, user) or []
        return stmt.where(Bill.id.in_(select(BillDetail.bill_id).where(BillDetail.elder_id.in_(elder_ids))))
    return apply_scope(stmt, Bill, user, db)


def _diff_scope(stmt: Select, user: User | None, db: Session) -> Select:
    if user is None:
        return stmt
    role = user.role.code if getattr(user, "role", None) else None
    stmt = apply_scope(stmt, ReconciliationDiff, user, db)
    if str(role) in (RoleCode.SUPER.value, RoleCode.STREET.value):
        return stmt
    bill_ids = _bill_scope(select(Bill.id), user, db)
    return stmt.where(or_(ReconciliationDiff.bill_id.is_(None), ReconciliationDiff.bill_id.in_(bill_ids)))


def _next_bill_code(db: Session, period: str) -> str:
    prefix = f"BILL-{str(period).replace('-', '')}"
    codes = db.execute(select(Bill.code).where(Bill.code.like(f"{prefix}%"))).scalars().all()
    seq = 0
    for code in codes:
        head = str(code)[len(prefix):].lstrip("-").split("-")[0]
        if head.isdigit():
            seq = max(seq, int(head))
    return f"{prefix}-{seq + 1:04d}"


def _next_red_code(db: Session, period: str) -> str:
    prefix = f"BILL-{str(period).replace('-', '')}"
    codes = db.execute(select(Bill.code).where(Bill.code.like(f"{prefix}%"))).scalars().all()
    seq = 0
    for code in codes:
        head = str(code)[len(prefix):].lstrip("-").split("-")[0]
        if head.isdigit():
            seq = max(seq, int(head))
    return f"{prefix}-{seq + 1:04d}-R"


# ================================================================ 序列化
def serialize_bill(bill: Bill) -> dict[str, Any]:
    return {
        "id": bill.id,
        "code": bill.code,
        "community_org_id": bill.community_org_id,
        "period": bill.period,
        "total_amount": _num(bill.total_amount),
        "subsidy_amount": _num(bill.subsidy_amount),
        "ltc_amount": _num(bill.ltc_amount),
        "self_pay_amount": _num(bill.self_pay_amount),
        "diff_amount": _num(bill.diff_amount),
        "diff_ratio": float(bill.diff_ratio or 0.0),
        "status": str(bill.status),
        "status_name": BILL_STATUS_NAMES.get(str(bill.status), str(bill.status)),
        "available_actions": BILL.actions(str(bill.status)),
        "needs_review": bool(bill.needs_review),
        "created_by": bill.created_by,
        "reviewed_by": bill.reviewed_by,
        "confirmed_by": bill.confirmed_by,
        "confirmed_at": bill.confirmed_at,
        "spot_check_passed": bool(bill.spot_check_passed),
        "spot_check_ratio": float(bill.spot_check_ratio or 0.0),
        "created_at": bill.created_at,
        "updated_at": bill.updated_at,
    }


def bill_view(db: Session, bill: Bill, user: User | None) -> dict[str, Any]:
    """本人/网格视角只汇总可见老人，不能由同社区账单泄露其他老人的费用。"""
    data = serialize_bill(bill)
    if user is None or effective_scope(user) not in (DataScope.SELF, DataScope.GRID):
        return data
    ids = visible_elder_ids(db, user) or []
    details = db.scalars(select(BillDetail).where(BillDetail.bill_id == bill.id, BillDetail.elder_id.in_(ids))).all()
    for attr in ("total_amount", "subsidy_amount", "ltc_amount", "self_pay_amount"):
        column = "amount" if attr == "total_amount" else attr
        data[attr] = _num(sum((money(getattr(row, column)) for row in details), ZERO))
    diff_amount = db.scalar(select(func.coalesce(func.sum(ReconciliationDiff.amount), 0)).where(
        ReconciliationDiff.bill_id == bill.id, ReconciliationDiff.elder_id.in_(ids),
    ))
    data["diff_amount"] = _num(diff_amount)
    data["diff_ratio"] = float(money(diff_amount) / money(data["total_amount"])) if data["total_amount"] else 0.0
    data["view_scope"] = "visible_elders"
    return data


def _visible_details(rows: list, db: Session, user: User | None) -> list:
    if user is None:
        return rows
    ids = visible_elder_ids(db, user)
    return rows if ids is None else [row for row in rows if row.elder_id in ids]


def serialize_detail(
    detail: BillDetail,
    elder: Elder | None = None,
    order: WorkOrder | None = None,
) -> dict[str, Any]:
    return {
        "id": detail.id,
        "bill_id": detail.bill_id,
        "work_order_id": detail.work_order_id,
        "work_order_code": order.code if order is not None else None,
        "elder_id": detail.elder_id,
        "elder_code": elder.code if elder is not None else None,
        "elder_name": elder.name if elder is not None else None,
        "price_version_id": detail.price_version_id,
        "unit_price": _num(detail.unit_price),
        "quantity": float(detail.quantity or 0),
        "amount": _num(detail.amount),
        "subsidy_amount": _num(detail.subsidy_amount),
        "ltc_amount": _num(detail.ltc_amount),
        "self_pay_amount": _num(detail.self_pay_amount),
        "created_at": detail.created_at,
    }


def serialize_diff(
    diff: ReconciliationDiff,
    elder: Elder | None = None,
    order: WorkOrder | None = None,
) -> dict[str, Any]:
    return {
        "id": diff.id,
        "bill_id": diff.bill_id,
        "work_order_id": diff.work_order_id,
        "work_order_code": order.code if order is not None else None,
        "elder_id": diff.elder_id,
        "elder_code": elder.code if elder is not None else None,
        "elder_name": elder.name if elder is not None else None,
        "rule_code": diff.rule_code,
        "rule_name": RULE_NAMES.get(diff.rule_code, diff.rule_code),
        "description": diff.description,
        "amount": _num(diff.amount),
        "status": str(diff.status),
        "status_name": DIFF_STATUS_NAMES.get(str(diff.status), str(diff.status)),
        "explanation": diff.explanation,
        "handler_id": diff.handler_id,
        "handled_at": diff.handled_at,
        "created_at": diff.created_at,
    }


def serialize_payment(record: PaymentRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "bill_id": record.bill_id,
        "payer_type": record.payer_type,
        "payer_name": PAYER_NAMES.get(record.payer_type, record.payer_type),
        "amount": _num(record.amount),
        "status": str(record.status),
        "paid_at": record.paid_at,
        "remark": record.remark,
    }


# ================================================================ 查询
def get_bill_or_404(db: Session, bill_id: int, user: User | None = None) -> Bill:
    bill = db.get(Bill, bill_id)
    if bill is None:
        raise NotFound("账单不存在")
    if user is not None:
        visible = db.execute(_bill_scope(select(Bill.id).where(Bill.id == bill.id), user, db)).first()
        if visible is None:
            raise Forbidden("无权访问该账单（数据范围限制）")
    return bill


def list_bills(
    db: Session,
    user: User | None = None,
    *,
    period: str | None = None,
    status: str | None = None,
    community_org_id: int | None = None,
    keyword: str | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    stmt = _bill_scope(select(Bill), user, db)
    if period:
        stmt = stmt.where(Bill.period == str(period).strip())
    if status:
        stmt = stmt.where(Bill.status == str(status).strip())
    if community_org_id:
        stmt = stmt.where(Bill.community_org_id == community_org_id)
    if keyword:
        stmt = stmt.where(Bill.code.like(f"%{keyword.strip()}%"))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.execute(
        stmt.order_by(Bill.period.desc(), Bill.id.desc())
        .offset(max(0, (page - 1) * size))
        .limit(size)
    ).scalars().all()
    return [bill_view(db, bill, user) for bill in rows], total


def get_bill_detail(db: Session, bill_id: int, user: User | None = None) -> dict[str, Any]:
    bill = get_bill_or_404(db, bill_id, user)
    details = db.execute(
        select(BillDetail).where(BillDetail.bill_id == bill.id).order_by(BillDetail.id.asc())
    ).scalars().all()
    diffs = db.execute(
        select(ReconciliationDiff).where(ReconciliationDiff.bill_id == bill.id).order_by(ReconciliationDiff.id.asc())
    ).scalars().all()
    payments = db.execute(
        select(PaymentRecord).where(PaymentRecord.bill_id == bill.id).order_by(PaymentRecord.id.asc())
    ).scalars().all()

    details = _visible_details(details, db, user)
    diffs = _visible_details(diffs, db, user)
    if user is not None and effective_scope(user) in (DataScope.SELF, DataScope.GRID):
        payments = []  # 社区统一缴费凭证不能作为个人或网格缴费明细公开。
    elder_ids = {d.elder_id for d in details} | {d.elder_id for d in diffs if d.elder_id}
    order_ids = {d.work_order_id for d in details if d.work_order_id} | {
        d.work_order_id for d in diffs if d.work_order_id
    }
    elders = (
        {e.id: e for e in db.execute(select(Elder).where(Elder.id.in_(elder_ids))).scalars().all()}
        if elder_ids
        else {}
    )
    orders = (
        {o.id: o for o in db.execute(select(WorkOrder).where(WorkOrder.id.in_(order_ids))).scalars().all()}
        if order_ids
        else {}
    )

    payload = bill_view(db, bill, user)
    payload["details"] = [
        serialize_detail(d, elders.get(d.elder_id), orders.get(d.work_order_id)) for d in details
    ]
    payload["diffs"] = [
        serialize_diff(d, elders.get(d.elder_id), orders.get(d.work_order_id)) for d in diffs
    ]
    payload["payments"] = [serialize_payment(p) for p in payments]
    payload["details_count"] = len(details)
    payload["diffs_count"] = len(diffs)
    return payload


def list_diffs(
    db: Session,
    user: User | None = None,
    *,
    status: str | None = None,
    rule_code: str | None = None,
    period: str | None = None,
    bill_id: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    stmt = _diff_scope(select(ReconciliationDiff), user, db)
    if period:
        stmt = stmt.join(Bill, ReconciliationDiff.bill_id == Bill.id).where(Bill.period == str(period).strip())
    if status:
        stmt = stmt.where(ReconciliationDiff.status == str(status).strip())
    if rule_code:
        stmt = stmt.where(ReconciliationDiff.rule_code == str(rule_code).strip())
    if bill_id:
        stmt = stmt.where(ReconciliationDiff.bill_id == bill_id)

    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.execute(
        stmt.order_by(ReconciliationDiff.id.desc())
        .offset(max(0, (page - 1) * size))
        .limit(size)
    ).scalars().all()

    elder_ids = {d.elder_id for d in rows if d.elder_id}
    order_ids = {d.work_order_id for d in rows if d.work_order_id}
    elders = (
        {e.id: e for e in db.execute(select(Elder).where(Elder.id.in_(elder_ids))).scalars().all()}
        if elder_ids
        else {}
    )
    orders = (
        {o.id: o for o in db.execute(select(WorkOrder).where(WorkOrder.id.in_(order_ids))).scalars().all()}
        if order_ids
        else {}
    )
    return [
        serialize_diff(d, elders.get(d.elder_id), orders.get(d.work_order_id)) for d in rows
    ], total


def create_diff(
    db: Session,
    *,
    rule_code: str,
    description: str,
    amount: Decimal | str | float | int | None = None,
    bill_id: int | None = None,
    work_order_id: int | None = None,
    elder_id: int | None = None,
    user: User | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """手工登记对账差异（监管抽查、人工补录场景）。

    校验：rule_code / description 必填；bill_id / work_order_id 若传入必须存在。
    写入 state=open，并记审计（CREATE）。
    """
    if not str(rule_code or "").strip():
        raise RuleViolation("rule_code 不能为空")
    if not str(description or "").strip():
        raise RuleViolation("差异说明不能为空")
    if bill_id is not None and db.get(Bill, bill_id) is None:
        raise NotFound("账单不存在")
    if work_order_id is not None:
        from app.models import WorkOrder as _WorkOrder

        if db.get(_WorkOrder, work_order_id) is None:
            raise NotFound("工单不存在")

    diff = ReconciliationDiff(
        bill_id=bill_id,
        work_order_id=work_order_id,
        elder_id=elder_id,
        rule_code=str(rule_code).strip(),
        description=str(description).strip(),
        amount=money(amount) if amount is not None else ZERO,
        status=DiffStatus.OPEN.value,
    )
    db.add(diff)
    db.flush()
    write_audit(
        db,
        user=user,
        action=AuditAction.CREATE.value,
        entity_type="reconciliation_diff",
        entity_id=diff.id,
        after={
            "bill_id": bill_id,
            "work_order_id": work_order_id,
            "elder_id": elder_id,
            "rule_code": diff.rule_code,
            "amount": _num(diff.amount),
        },
        detail=f"登记对账差异：{diff.description}"[:255],
        request=request,
    )
    db.commit()
    return serialize_diff(diff)


# ================================================================ 账单生成
def _order_amount(order: WorkOrder) -> Decimal:
    unit_price = money(order.unit_price or 0)
    quantity = float(order.quantity) if order.quantity not in (None, 0) else 1.0
    return money(unit_price * Decimal(str(quantity)))


def _order_interval(order: WorkOrder, item: ServiceItem | None) -> tuple[datetime, datetime]:
    start = order.scheduled_start
    end = order.scheduled_end
    if end is None or end <= start:
        minutes = 60.0
        if item is not None and item.standard_duration_min:
            minutes = float(item.standard_duration_min)
        end = start + timedelta(minutes=minutes)
    return start, end


def _make_diff(
    bill: Bill | None,
    *,
    rule_code: str,
    description: str,
    amount: Decimal,
    elder_id: int | None = None,
    work_order_id: int | None = None,
) -> ReconciliationDiff:
    return ReconciliationDiff(
        bill_id=bill.id if bill is not None else None,
        work_order_id=work_order_id,
        elder_id=elder_id,
        rule_code=rule_code,
        description=description[:255],
        amount=money(amount),
        status=DiffStatus.OPEN.value,
    )


def _prepare_draft_bill(db: Session, *, period: str, org_id: int, user: User | None) -> Bill:
    existing = db.execute(
        select(Bill)
        .where(
            Bill.period == period,
            Bill.community_org_id == org_id,
            Bill.status != BillStatus.REVERSED.value,
        )
        .order_by(Bill.id.desc())
    ).scalars().first()
    if existing is not None:
        if str(existing.status) != BillStatus.DRAFT.value:
            raise Conflict(f"账期 {period} 的账单已提交复核或已确认，不能重复生成")
        db.query(BillDetail).filter(BillDetail.bill_id == existing.id).delete(synchronize_session=False)
        db.query(ReconciliationDiff).filter(ReconciliationDiff.bill_id == existing.id).delete(
            synchronize_session=False
        )
        existing.total_amount = ZERO
        existing.subsidy_amount = ZERO
        existing.ltc_amount = ZERO
        existing.self_pay_amount = ZERO
        existing.diff_amount = ZERO
        existing.diff_ratio = 0.0
        existing.needs_review = False
        db.flush()
        return existing

    bill = Bill(
        code=_next_bill_code(db, period),
        community_org_id=org_id,
        period=period,
        total_amount=ZERO,
        subsidy_amount=ZERO,
        ltc_amount=ZERO,
        self_pay_amount=ZERO,
        diff_amount=ZERO,
        diff_ratio=0.0,
        status=BillStatus.DRAFT.value,
        needs_review=False,
        created_by=user.id if user is not None else None,
    )
    db.add(bill)
    db.flush()
    return bill


def _used_subsidy(db: Session, period: str) -> dict[int, Decimal]:
    """本月（跨账单）已用政府补贴：老人 → 金额。红冲单的负数会自动抵扣。"""
    rows = db.execute(
        select(BillDetail.elder_id, func.coalesce(func.sum(BillDetail.subsidy_amount), 0))
        .join(Bill, BillDetail.bill_id == Bill.id)
        .where(Bill.period == period, Bill.status != BillStatus.REVERSED.value)
        .group_by(BillDetail.elder_id)
    ).all()
    return {int(elder_id): money(amount) for elder_id, amount in rows}


def _load_maps(db: Session, order_list: list[WorkOrder]) -> tuple[dict[int, Elder], dict[int, ServiceItem]]:
    elder_ids = {o.elder_id for o in order_list if o.elder_id}
    item_ids = {o.service_item_id for o in order_list if o.service_item_id}
    elders = (
        {e.id: e for e in db.execute(select(Elder).where(Elder.id.in_(elder_ids))).scalars().all()}
        if elder_ids
        else {}
    )
    items = (
        {i.id: i for i in db.execute(select(ServiceItem).where(ServiceItem.id.in_(item_ids))).scalars().all()}
        if item_ids
        else {}
    )
    return elders, items


def _build_bill_content(
    db: Session,
    bill: Bill,
    orders: list[WorkOrder],
    *,
    elders: dict[int, Elder],
    items: dict[int, ServiceItem],
    policies: list[Any],
    used: dict[int, Decimal],
) -> tuple[list[BillDetail], list[ReconciliationDiff], dict[int, Decimal]]:
    details: list[BillDetail] = []
    diffs: list[ReconciliationDiff] = []
    amount_by_order: dict[int, Decimal] = {}

    for order in orders:
        elder = elders.get(order.elder_id)
        item = items.get(order.service_item_id)
        unit_price = money(order.unit_price or 0)
        quantity = float(order.quantity) if order.quantity not in (None, 0) else 1.0
        amount = money(unit_price * Decimal(str(quantity)))
        amount_by_order[order.id] = amount

        policy = None
        if item is None or bool(item.is_subsidized):
            policy = pick_policy(
                policies,
                elder.ability_level if elder is not None else None,
                elder.difficulty_type if elder is not None else None,
            )

        used_before = used.get(order.elder_id, ZERO)
        split = split_amount(amount, policy, used_before)
        used[order.elder_id] = money(used_before + split["subsidy"])

        detail = BillDetail(
            bill_id=bill.id,
            work_order_id=order.id,
            elder_id=order.elder_id,
            price_version_id=order.price_version_id,
            unit_price=unit_price,
            quantity=quantity,
            amount=amount,
            subsidy_amount=split["subsidy"],
            ltc_amount=split["ltc"],
            self_pay_amount=split["self_pay"],
        )
        db.add(detail)
        details.append(detail)

        # 差异①：补贴超上限（政策应补 vs 实际到账）
        if policy is not None:
            ratio = Decimal(str(getattr(policy, "subsidy_ratio", 0) or 0))
            requested = money(amount * ratio)
            if split["subsidy"] < requested:
                elder_label = elder.code if elder is not None else str(order.elder_id)
                limit = money(getattr(policy, "monthly_limit", 0))
                diffs.append(
                    _make_diff(
                        bill,
                        rule_code=RULE_SUBSIDY_LIMIT,
                        description=(
                            f"老人 {elder_label} 月度补贴达上限 {limit}，"
                            f"本单应补 {requested}、实补 {split['subsidy']}"
                        ),
                        amount=requested - split["subsidy"],
                        elder_id=order.elder_id,
                        work_order_id=order.id,
                    )
                )

        # 差异②：服务时长 > 标准时长 × 3（恰好 3 倍不拦截）
        if item is not None and item.standard_duration_min and order.duration_min is not None:
            standard = float(item.standard_duration_min)
            if standard > 0 and float(order.duration_min) > standard * DURATION_FACTOR:
                diffs.append(
                    _make_diff(
                        bill,
                        rule_code=RULE_DURATION,
                        description=(
                            f"工单 {order.code} 服务时长 {order.duration_min} 分钟，"
                            f"超过标准时长 {standard:g} 分钟的 {DURATION_FACTOR} 倍"
                        ),
                        amount=amount,
                        elder_id=order.elder_id,
                        work_order_id=order.id,
                    )
                )

    # 差异③：同一老人同一时段重复计费
    intervals = [
        (order, *_order_interval(order, items.get(order.service_item_id))) for order in orders
    ]
    for i in range(len(intervals)):
        left, left_start, left_end = intervals[i]
        for j in range(i + 1, len(intervals)):
            right, right_start, right_end = intervals[j]
            if left.elder_id != right.elder_id:
                continue
            if left_start < right_end and right_start < left_end:
                elder = elders.get(right.elder_id)
                elder_label = elder.code if elder is not None else str(right.elder_id)
                diffs.append(
                    _make_diff(
                        bill,
                        rule_code=RULE_DUPLICATE,
                        description=(
                            f"老人 {elder_label} 工单 {left.code}（{left_start:%m-%d %H:%M}）"
                            f"与 {right.code}（{right_start:%m-%d %H:%M}）时段重叠"
                        ),
                        amount=amount_by_order.get(right.id, _order_amount(right)),
                        elder_id=right.elder_id,
                        work_order_id=right.id,
                    )
                )

    for diff in diffs:
        db.add(diff)

    return details, diffs, amount_by_order


def _finalize_bill(
    bill: Bill,
    details: list[BillDetail],
    diffs: list[ReconciliationDiff],
) -> None:
    total = money(sum((money(d.amount) for d in details), ZERO))
    subsidy = money(sum((money(d.subsidy_amount) for d in details), ZERO))
    ltc = money(sum((money(d.ltc_amount) for d in details), ZERO))
    self_pay = money(total - subsidy - ltc)
    if self_pay < 0:
        self_pay = ZERO
    diff_amount = money(sum((abs(money(d.amount)) for d in diffs), ZERO))

    threshold = Decimal(str(settings.diff_ratio_threshold))
    if total > 0:
        ratio = diff_amount / total
        needs_review = diff_amount > (total * threshold)
    else:
        ratio = Decimal("0") if diff_amount == 0 else Decimal("1")
        needs_review = diff_amount > 0

    bill.total_amount = total
    bill.subsidy_amount = subsidy
    bill.ltc_amount = ltc
    bill.self_pay_amount = self_pay
    bill.diff_amount = diff_amount
    bill.diff_ratio = round(float(ratio), 6)
    bill.needs_review = bool(needs_review)


def generate_bills(
    db: Session,
    *,
    period: str,
    user: User | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """按账期生成账单：只取 status=completed 的工单，按 scheduled_start 归月。"""
    period = str(period or "").strip()
    start, end = month_range(period)
    ensure_default_policies(db)
    policies = list(
        db.execute(select(SubsidyPolicy).where(SubsidyPolicy.enabled.is_(True))).scalars().all()
    )

    base = select(WorkOrder).where(
        WorkOrder.scheduled_start >= start,
        WorkOrder.scheduled_start < end,
    )
    completed = list(
        db.execute(
            _scope(base.where(WorkOrder.status == OrderStatus.COMPLETED.value), WorkOrder, user, db)
            .order_by(WorkOrder.scheduled_start.asc(), WorkOrder.id.asc())
        ).scalars().all()
    )
    unfinished = list(
        db.execute(
            _scope(
                base.where(WorkOrder.status.in_(list(UNFINISHED_ORDER_STATUSES))),
                WorkOrder,
                user,
                db,
            ).order_by(WorkOrder.scheduled_start.asc(), WorkOrder.id.asc())
        ).scalars().all()
    )
    if not completed and not unfinished:
        return {"period": period, "bill": None, "bills": [], "details_count": 0, "diffs_count": 0}

    elders, items = _load_maps(db, completed + unfinished)

    groups: dict[int, list[WorkOrder]] = {}
    for order in completed:
        groups.setdefault(order.community_org_id, []).append(order)

    bills: dict[int, Bill] = {}
    for org_id in groups:
        bills[org_id] = _prepare_draft_bill(db, period=period, org_id=org_id, user=user)
    db.flush()

    used = _used_subsidy(db, period)

    details_count = 0
    diffs_count = 0
    bill_details: dict[int, list[BillDetail]] = {}
    bill_diffs: dict[int, list[ReconciliationDiff]] = {}

    for org_id, org_orders in groups.items():
        bill = bills[org_id]
        details, diffs, _ = _build_bill_content(
            db,
            bill,
            org_orders,
            elders=elders,
            items=items,
            policies=policies,
            used=used,
        )
        bill_details[bill.id] = details
        bill_diffs[bill.id] = diffs
        details_count += len(details)
        diffs_count += len(diffs)

    # 差异④：未完成工单却产生计费（不计入账单，仅列入差异清单）
    for order in unfinished:
        bill = bills.get(order.community_org_id)
        amount = _order_amount(order)
        diff = _make_diff(
            bill,
            rule_code=RULE_UNFINISHED,
            description=(
                f"工单 {order.code} 状态为「{ORDER_STATUS_NAMES.get(str(order.status), str(order.status))}」，"
                f"未完成却产生计费 {amount}"
            ),
            amount=amount,
            elder_id=order.elder_id,
            work_order_id=order.id,
        )
        db.add(diff)
        diffs_count += 1
        if bill is not None:
            bill_diffs.setdefault(bill.id, []).append(diff)
        else:
            logger.info("账期 %s 存在未完成工单 %s，但该社区无账单可归属", period, order.code)

    for bill in bills.values():
        _finalize_bill(bill, bill_details.get(bill.id, []), bill_diffs.get(bill.id, []))
        write_audit(
            db,
            user=user,
            action=AuditAction.CREATE.value,
            entity_type="bill",
            entity_id=bill.id,
            after={
                "code": bill.code,
                "period": bill.period,
                "total_amount": _num(bill.total_amount),
                "details_count": len(bill_details.get(bill.id, [])),
                "diffs_count": len(bill_diffs.get(bill.id, [])),
                "needs_review": bool(bill.needs_review),
            },
            detail=f"生成 {period} 月度账单",
            request=request,
        )

    serialized = [serialize_bill(bill) for bill in bills.values()]
    db.commit()
    return {
        "period": period,
        "bill": serialized[0] if serialized else None,
        "bills": serialized,
        "details_count": details_count,
        "diffs_count": diffs_count,
    }


# ================================================================ 账单流转
def submit_bill(
    db: Session,
    bill_id: int,
    *,
    user: User | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    bill = get_bill_or_404(db, bill_id, user)
    target = BILL.next(str(bill.status), "submit")
    before = {"status": str(bill.status)}
    bill.status = target
    write_audit(
        db,
        user=user,
        action=AuditAction.UPDATE.value,
        entity_type="bill",
        entity_id=bill.id,
        before=before,
        after={"status": target},
        detail="账单提交复核",
        request=request,
    )
    db.commit()
    return serialize_bill(bill)


def confirm_bill(
    db: Session,
    bill_id: int,
    *,
    review_note: str | None = None,
    user: User | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    bill = get_bill_or_404(db, bill_id, user)
    note = (review_note or "").strip()
    if bill.needs_review and not note:
        raise RuleViolation(
            f"账单差异率 {float(bill.diff_ratio or 0) * 100:.2f}% 超过阈值 "
            f"{settings.diff_ratio_threshold * 100:.0f}%，复核通过必须填写复核说明"
        )
    target = BILL.next(str(bill.status), "confirm")
    before = {"status": str(bill.status), "needs_review": bool(bill.needs_review)}
    now = utcnow()
    bill.status = target
    bill.reviewed_by = user.id if user is not None else None
    bill.confirmed_by = user.id if user is not None else None
    bill.confirmed_at = now
    write_audit(
        db,
        user=user,
        action=AuditAction.UPDATE.value,
        entity_type="bill",
        entity_id=bill.id,
        before=before,
        after={"status": target, "review_note": note or None, "confirmed_at": now},
        detail="账单复核通过" + (f"：{note}" if note else ""),
        request=request,
    )
    db.commit()
    return serialize_bill(bill)


def pay_bill(
    db: Session,
    bill_id: int,
    *,
    government: Decimal | None = None,
    insurance: Decimal | None = None,
    self_pay: Decimal | None = None,
    payer_type: str | None = None,
    amount: Decimal | None = None,
    remark: str | None = None,
    user: User | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """登记缴费：支持「三方金额」与「单付款方」两种写法，金额必须与账单分账一致。"""
    bill = get_bill_or_404(db, bill_id, user)
    total = money(bill.total_amount)
    expected = {
        "government": money(bill.subsidy_amount),
        "insurance": money(bill.ltc_amount),
        "self": money(bill.self_pay_amount),
    }

    records: list[tuple[str, Decimal]] = []
    if government is not None or insurance is not None or self_pay is not None:
        if government is None or insurance is None or self_pay is None:
            raise BadRequest("government / insurance / self_pay 三个金额必须同时提供")
        gov, ins, slf = money(government), money(insurance), money(self_pay)
        if gov != expected["government"] or ins != expected["insurance"] or slf != expected["self"]:
            raise RuleViolation(
                "缴费金额与账单不符："
                f"政府补贴应缴 {expected['government']}、长护险应缴 {expected['insurance']}、"
                f"个人自付应缴 {expected['self']}"
            )
        if money(gov + ins + slf) != total:
            raise RuleViolation(f"三方缴费合计 {money(gov + ins + slf)} 与账单总额 {total} 不符")
        records = [("government", gov), ("insurance", ins), ("self", slf)]
        target = BILL.next(str(bill.status), "pay")
    elif payer_type:
        key = PAYER_ALIASES.get(str(payer_type).strip().lower())
        if key is None:
            raise BadRequest("付款方只能是 government / insurance / self")
        if amount is None:
            raise BadRequest("单付款方登记必须提供缴费金额 amount")
        value = money(amount)
        if value != expected[key]:
            raise RuleViolation(f"{PAYER_NAMES[key]}应缴 {expected[key]}，实际登记 {value} 不符")
        records = [(key, value)]
        paid_before = money(
            db.scalar(
                select(func.coalesce(func.sum(PaymentRecord.amount), 0)).where(PaymentRecord.bill_id == bill.id)
            )
            or 0
        )
        if str(bill.status) == BillStatus.PAID.value:
            raise Conflict("账单已缴费，不能重复登记")
        target = (
            BILL.next(str(bill.status), "pay")
            if money(paid_before + value) == total and str(bill.status) == BillStatus.CONFIRMED.value
            else str(bill.status)
        )
        if target == str(bill.status):
            BILL.next(str(bill.status), "pay")  # 校验当前状态可缴费，否则抛 Conflict
    else:
        raise BadRequest("请提供 government/insurance/self_pay 三方金额，或 payer_type + amount")

    before = {"status": str(bill.status)}
    now = utcnow()
    for key, value in records:
        db.add(
            PaymentRecord(
                bill_id=bill.id,
                payer_type=key,
                amount=value,
                status="paid",
                paid_at=now,
                remark=remark,
            )
        )
    bill.status = target
    write_audit(
        db,
        user=user,
        action=AuditAction.UPDATE.value,
        entity_type="bill",
        entity_id=bill.id,
        before=before,
        after={
            "status": target,
            "payments": [{"payer_type": key, "amount": _num(value)} for key, value in records],
        },
        detail="登记缴费",
        request=request,
    )
    db.commit()

    payload = serialize_bill(bill)
    payload["payments"] = [
        serialize_payment(record)
        for record in db.execute(
            select(PaymentRecord).where(PaymentRecord.bill_id == bill.id).order_by(PaymentRecord.id.asc())
        ).scalars().all()
    ]
    return payload


# ================================================================ 红冲 / 退费
def _red_components(
    total_abs: Decimal,
    subsidy_abs: Decimal,
    ltc_abs: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    self_abs = money(total_abs - subsidy_abs - ltc_abs)
    if self_abs < 0:
        self_abs = ZERO
    return money(-subsidy_abs), money(-ltc_abs), money(-self_abs)


def _build_red_bill(
    db: Session,
    source: Bill,
    *,
    amount: Decimal | None = None,
    source_detail: BillDetail | None = None,
    reason: str = "",
    operator_id: int | None = None,
    components: tuple[Decimal, Decimal, Decimal] | None = None,
) -> Bill:
    """生成负数红冲账单（原单金额与状态由调用方处理，本函数只负责新单）。"""
    if amount is not None:
        total_abs = money(abs(money(amount)))
    elif source_detail is not None:
        total_abs = money(abs(money(source_detail.amount)))
    else:
        total_abs = money(abs(money(source.total_amount)))

    if source_detail is not None:
        base_amount = money(abs(money(source_detail.amount)))
        scale = (total_abs / base_amount) if base_amount > 0 else Decimal("0")
        subsidy_abs = money(abs(money(source_detail.subsidy_amount)) * scale)
        ltc_abs = money(abs(money(source_detail.ltc_amount)) * scale)
    else:
        base_amount = money(abs(money(source.total_amount)))
        scale = (total_abs / base_amount) if base_amount > 0 else Decimal("0")
        subsidy_abs = money(abs(money(source.subsidy_amount)) * scale)
        ltc_abs = money(abs(money(source.ltc_amount)) * scale)

    subsidy, ltc, self_pay = _red_components(total_abs, subsidy_abs, ltc_abs)
    if components is not None:
        subsidy, ltc, self_pay = (-money(value) for value in components)
    if money(subsidy + ltc + self_pay) != -total_abs:
        raise RuleViolation("红冲分账金额与退费总额不一致")
    now = utcnow()
    red = Bill(
        code=_next_red_code(db, source.period),
        community_org_id=source.community_org_id,
        period=source.period,
        total_amount=money(-total_abs),
        subsidy_amount=subsidy,
        ltc_amount=ltc,
        self_pay_amount=self_pay,
        diff_amount=ZERO,
        diff_ratio=0.0,
        status=BillStatus.DRAFT.value,
        needs_review=False,
        created_by=operator_id,
        reviewed_by=operator_id,
        confirmed_by=operator_id,
        confirmed_at=now,
    )
    db.add(red)
    db.flush()

    # 状态机流转：红冲单也走 draft → pending_review → confirmed，不允许直接赋终态
    red.status = BILL.next(red.status, "submit")
    red.status = BILL.next(red.status, "confirm")

    if source_detail is not None:
        db.add(
            BillDetail(
                bill_id=red.id,
                work_order_id=source_detail.work_order_id,
                elder_id=source_detail.elder_id,
                price_version_id=source_detail.price_version_id,
                unit_price=money(source_detail.unit_price),
                quantity=float(source_detail.quantity or 0),
                amount=money(-total_abs),
                subsidy_amount=subsidy,
                ltc_amount=ltc,
                self_pay_amount=self_pay,
            )
        )
    else:
        source_details = db.execute(
            select(BillDetail).where(BillDetail.bill_id == source.id).order_by(BillDetail.id.asc())
        ).scalars().all()
        for detail in source_details:
            db.add(
                BillDetail(
                    bill_id=red.id,
                    work_order_id=detail.work_order_id,
                    elder_id=detail.elder_id,
                    price_version_id=detail.price_version_id,
                    unit_price=money(detail.unit_price),
                    quantity=float(detail.quantity or 0),
                    amount=money(-money(detail.amount)),
                    subsidy_amount=money(-money(detail.subsidy_amount)),
                    ltc_amount=money(-money(detail.ltc_amount)),
                    self_pay_amount=money(-money(detail.self_pay_amount)),
                )
            )
    db.flush()
    logger.info("生成红冲账单 %s（原单 %s，金额 %s）：%s", red.code, source.code, red.total_amount, reason)
    return red


def reverse_bill(
    db: Session,
    bill_id: int,
    *,
    reason: str = "",
    user: User | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """红冲：原单状态置 reversed、金额不变；同时生成负数红冲新单。"""
    bill = get_bill_or_404(db, bill_id, user)
    if money(bill.total_amount) <= 0:
        raise RuleViolation("负数红冲单不能再次红冲")
    order_ids = select(BillDetail.work_order_id).where(BillDetail.bill_id == bill.id)
    refunded = db.scalar(select(func.count(BillDetail.id)).where(
        BillDetail.work_order_id.in_(order_ids), BillDetail.amount < 0,
    ))
    if refunded:
        raise Conflict("账单已有工单退费，请逐工单退还剩余金额，避免重复红冲")
    target = BILL.next(str(bill.status), "reverse")
    before = {
        "status": str(bill.status),
        "total_amount": _num(bill.total_amount),
    }
    bill.status = target
    red = _build_red_bill(
        db,
        bill,
        reason=reason,
        operator_id=user.id if user is not None else None,
    )
    write_audit(
        db,
        user=user,
        action=AuditAction.REFUND.value,
        entity_type="bill",
        entity_id=bill.id,
        before=before,
        after={
            "status": target,
            "reverse_bill_id": red.id,
            "reverse_bill_code": red.code,
            "reverse_amount": _num(red.total_amount),
        },
        detail=f"账单红冲：{reason or '无'}"[:255],
        request=request,
    )
    db.commit()

    payload = serialize_bill(bill)
    payload["reverse_bill"] = serialize_bill(red)
    payload["reverse_bill_id"] = red.id
    payload["reason"] = reason
    return payload


def create_refund(
    db: Session,
    work_order_id: int,
    amount: Decimal | None = None,
    reason: str = "",
    operator_id: int | None = None,
) -> dict[str, Any]:
    """按工单生成退费红冲（负数新单）。供投诉结论成立时调用，**不抛未捕获异常**。"""
    try:
        with db.begin_nested():
            detail = db.execute(
                select(BillDetail)
                .join(Bill, BillDetail.bill_id == Bill.id)
                .where(
                    BillDetail.work_order_id == work_order_id,
                    BillDetail.amount > 0,
                    Bill.status != BillStatus.REVERSED.value,
                )
                .order_by(BillDetail.id.desc())
            ).scalars().first()
            if detail is None:
                return {
                    "ok": False,
                    "bill_id": None,
                    "work_order_id": work_order_id,
                    "reason": "该工单未生成账单明细，无需红冲",
                }

            source = db.get(Bill, detail.bill_id)
            if source is None:
                return {
                    "ok": False,
                    "bill_id": None,
                    "work_order_id": work_order_id,
                    "reason": "原账单不存在",
                }

            # 原明细只能退一次总额；负数明细绝不能被再次用作退费来源。
            previous = db.execute(select(
                func.coalesce(func.sum(BillDetail.amount), 0),
                func.coalesce(func.sum(BillDetail.subsidy_amount), 0),
                func.coalesce(func.sum(BillDetail.ltc_amount), 0),
                func.coalesce(func.sum(BillDetail.self_pay_amount), 0),
            ).where(BillDetail.work_order_id == work_order_id, BillDetail.amount < 0)).one()
            remaining = money(detail.amount + money(previous[0]))
            if remaining <= 0:
                return {"ok": False, "bill_id": None, "work_order_id": work_order_id, "reason": "该工单已全额退费"}
            requested = money(amount) if amount is not None else remaining
            refund_amount = min(requested, remaining)
            if refund_amount <= 0:
                return {
                    "ok": False,
                    "bill_id": None,
                    "work_order_id": work_order_id,
                    "reason": "退费金额必须大于 0",
                }

            remaining_parts = [money(getattr(detail, attr) + money(previous[index + 1]))
                               for index, attr in enumerate(("subsidy_amount", "ltc_amount", "self_pay_amount"))]
            # 按剩余各方金额分摊；最后一次精确退尽各方余额，避免累计分币误差。
            if refund_amount == remaining:
                parts = remaining_parts
            else:
                gov = min(remaining_parts[0], money(refund_amount * remaining_parts[0] / remaining))
                ltc = min(remaining_parts[1], refund_amount - gov,
                          money(refund_amount * remaining_parts[1] / remaining))
                personal = money(refund_amount - gov - ltc)
                if personal > remaining_parts[2]:
                    shift = personal - remaining_parts[2]
                    extra_gov = min(shift, remaining_parts[0] - gov)
                    gov += extra_gov
                    ltc += shift - extra_gov
                    personal -= shift
                parts = [gov, ltc, personal]
            red = _build_red_bill(
                db,
                source,
                amount=refund_amount,
                source_detail=detail,
                reason=reason,
                operator_id=operator_id,
                components=tuple(parts),
            )
            user = db.get(User, operator_id) if operator_id else None
            write_audit(
                db,
                user=user,
                action=AuditAction.REFUND.value,
                entity_type="bill",
                entity_id=red.id,
                after={
                    "code": red.code,
                    "amount": _num(red.total_amount),
                    "original_bill_id": source.id,
                    "work_order_id": work_order_id,
                },
                detail=f"工单退费红冲：{reason}".strip("：")[:255],
            )
            result = {
                "ok": True,
                "bill_id": red.id,
                "id": red.id,
                "code": red.code,
                "bill": serialize_bill(red),
                "amount": _num(red.total_amount),
                "work_order_id": work_order_id,
                "original_bill_id": source.id,
                "reason": reason,
            }
        return result
    except Exception as exc:  # noqa: BLE001 - 对外承诺不抛异常
        logger.exception("工单 %s 退费红冲失败：%s", work_order_id, exc)
        return {"ok": False, "bill_id": None, "work_order_id": work_order_id, "reason": reason, "error": str(exc)}


# ================================================================ 差异处理
def handle_diff(
    db: Session,
    diff_id: int,
    *,
    status: str,
    explanation: str,
    user: User | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    diff = db.get(ReconciliationDiff, diff_id)
    if diff is None:
        raise NotFound("对账差异不存在")
    target = str(status or "").strip()
    if target not in (DiffStatus.EXPLAINED.value, DiffStatus.HANDLED.value):
        raise BadRequest("处理状态只能是 explained 或 handled")
    note = (explanation or "").strip()
    if not note:
        raise RuleViolation("处理差异必须填写说明")
    if diff.bill_id is not None and user is not None:
        get_bill_or_404(db, diff.bill_id, user)

    before = {"status": str(diff.status), "explanation": diff.explanation}
    now = utcnow()
    diff.status = target
    diff.explanation = note
    diff.handler_id = user.id if user is not None else None
    diff.handled_at = now
    write_audit(
        db,
        user=user,
        action=AuditAction.UPDATE.value,
        entity_type="reconciliation_diff",
        entity_id=diff.id,
        before=before,
        after={"status": target, "explanation": note},
        detail=f"处理对账差异（{RULE_NAMES.get(diff.rule_code, diff.rule_code)}）",
        request=request,
    )
    db.commit()
    return serialize_diff(diff)


# ================================================================ Excel 导出
def build_bill_workbook(
    bill: Bill,
    details: list[BillDetail],
    elders: dict[int, Elder],
    orders: dict[int, WorkOrder],
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "账单明细"

    sheet["A1"] = f"社区智慧养老服务账单 · {bill.code}"
    sheet["A1"].font = Font(bold=True, size=14)
    sheet.merge_cells("A1:I1")
    sheet["A1"].alignment = Alignment(horizontal="center", vertical="center")

    info = [
        ("账期", bill.period, "状态", BILL_STATUS_NAMES.get(str(bill.status), str(bill.status))),
        ("社区机构", bill.community_org_id, "差异率", f"{float(bill.diff_ratio or 0) * 100:.2f}%"),
        ("账单总额", _num(bill.total_amount), "差异金额", _num(bill.diff_amount)),
        ("政府补贴", _num(bill.subsidy_amount), "长护险", _num(bill.ltc_amount)),
        ("个人自付", _num(bill.self_pay_amount), "需人工复核", "是" if bill.needs_review else "否"),
    ]
    row = 3
    for left_key, left_value, right_key, right_value in info:
        sheet.cell(row=row, column=1, value=left_key).font = Font(bold=True)
        sheet.cell(row=row, column=2, value=safe_cell(left_value))
        sheet.cell(row=row, column=4, value=right_key).font = Font(bold=True)
        sheet.cell(row=row, column=5, value=safe_cell(right_value))
        row += 1

    header_row = row + 1
    headers = ["老人编号", "老人姓名", "工单号", "单价", "数量", "金额", "政府补贴", "长护险", "个人自付"]
    fill = PatternFill("solid", fgColor="DCE6F1")
    for index, title in enumerate(headers, start=1):
        cell = sheet.cell(row=header_row, column=index, value=title)
        cell.font = Font(bold=True)
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    row = header_row + 1
    for detail in details:
        elder = elders.get(detail.elder_id)
        order = orders.get(detail.work_order_id) if detail.work_order_id else None
        values = [
            elder.code if elder is not None else None,
            elder.name if elder is not None else None,
            order.code if order is not None else None,
            _num(detail.unit_price),
            float(detail.quantity or 0),
            _num(detail.amount),
            _num(detail.subsidy_amount),
            _num(detail.ltc_amount),
            _num(detail.self_pay_amount),
        ]
        for index, value in enumerate(values, start=1):
            cell = sheet.cell(row=row, column=index, value=safe_cell(value))
            if index >= 4:
                cell.number_format = "#,##0.00"
        row += 1

    total_row = row
    sheet.cell(row=total_row, column=1, value="合计").font = Font(bold=True)
    totals = [
        None,
        None,
        None,
        None,
        None,
        _num(bill.total_amount),
        _num(bill.subsidy_amount),
        _num(bill.ltc_amount),
        _num(bill.self_pay_amount),
    ]
    for index, value in enumerate(totals, start=1):
        if value is None:
            continue
        cell = sheet.cell(row=total_row, column=index, value=value)
        cell.font = Font(bold=True)
        cell.number_format = "#,##0.00"

    widths = [16, 14, 18, 12, 10, 14, 14, 12, 14]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = f"A{header_row + 1}"

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def export_bill_xlsx(
    db: Session,
    bill_id: int,
    *,
    user: User | None = None,
    request: Request | None = None,
) -> tuple[bytes, str]:
    bill = get_bill_or_404(db, bill_id, user)
    details = list(
        db.execute(select(BillDetail).where(BillDetail.bill_id == bill.id).order_by(BillDetail.id.asc()))
        .scalars()
        .all()
    )
    details = _visible_details(details, db, user)
    elder_ids = {d.elder_id for d in details}
    order_ids = {d.work_order_id for d in details if d.work_order_id}
    elders = (
        {e.id: e for e in db.execute(select(Elder).where(Elder.id.in_(elder_ids))).scalars().all()}
        if elder_ids
        else {}
    )
    orders = (
        {o.id: o for o in db.execute(select(WorkOrder).where(WorkOrder.id.in_(order_ids))).scalars().all()}
        if order_ids
        else {}
    )
    content = build_bill_workbook(SimpleNamespace(**bill_view(db, bill, user)), details, elders, orders)
    write_audit(
        db,
        user=user,
        action=AuditAction.EXPORT.value,
        entity_type="bill",
        entity_id=bill.id,
        after={"code": bill.code, "details_count": len(details)},
        detail="导出账单 Excel",
        request=request,
    )
    db.commit()
    return content, f"bill_{bill.code}.xlsx"


def ensure_default_policies(db: Session) -> list[SubsidyPolicy]:
    """幂等写入默认补贴政策矩阵（中度/重度 × 普通/低保/特困，共 6 条）。

    说明：该函数依赖数据库，因此放在服务层；算法层只保留 `DEFAULT_POLICY_MATRIX`
    常量与 `pick_policy` / `split_amount` 纯函数（分层契约：algorithms 不触碰 DB）。
    已存在的 (ability_level, difficulty_type) 组合不会被覆盖，避免冲掉运营配置。
    """
    existing = {
        (str(p.ability_level or "").strip().lower(), str(p.difficulty_type or "").strip().lower()): p
        for p in db.execute(select(SubsidyPolicy)).scalars().all()
    }
    result: list[SubsidyPolicy] = []
    created = False
    for ability, difficulty, ratio, limit, ltc_ratio, note in DEFAULT_POLICY_MATRIX:
        key = (str(ability).strip().lower(), str(difficulty).strip().lower())
        policy = existing.get(key)
        if policy is None:
            policy = SubsidyPolicy(
                ability_level=ability,
                difficulty_type=difficulty,
                subsidy_ratio=float(ratio),
                monthly_limit=Decimal(limit),
                ltc_ratio=float(ltc_ratio),
                effective_from=datetime(2020, 1, 1),
                enabled=True,
                note=note,
            )
            db.add(policy)
            existing[key] = policy
            created = True
        result.append(policy)
    if created:
        db.flush()
    return result


__all__ = [
    "BILL_STATUS_NAMES",
    "DIFF_STATUS_NAMES",
    "RULE_DUPLICATE",
    "RULE_DURATION",
    "RULE_NAMES",
    "RULE_SUBSIDY_LIMIT",
    "RULE_UNFINISHED",
    "build_bill_workbook",
    "confirm_bill",
    "create_refund",
    "ensure_default_policies",
    "export_bill_xlsx",
    "generate_bills",
    "get_bill_detail",
    "get_bill_or_404",
    "handle_diff",
    "list_bills",
    "list_diffs",
    "month_range",
    "pay_bill",
    "reverse_bill",
    "serialize_bill",
    "serialize_detail",
    "serialize_diff",
    "create_diff",
    "serialize_payment",
    "submit_bill",
]
