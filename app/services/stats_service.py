"""统计分析与看板服务（E2 模块）。

聚合口径（只读，不写任何业务表）：
- 完单量：work_order.status == completed 且 completed_at 落在统计区间；
- 服务覆盖率：有 completed 工单的老人数 / 辖区老人总数（不含逻辑删除）；
- 满意度：evaluation.avg_score 均值；
- 收入：bill.subsidy_amount / ltc_amount / self_pay_amount（排除 reversed）；
- 预警响应时长：alert.response_seconds（缺失时回退 resolved_at - created_at），换算为分钟；
- 环比 mom =（本周期 - 上一周期）/ 上一周期，上一周期无基数时为 None；
- 去年 last_year：同比周期无数据时返回 null。

所有主查询先经 app.core.deps.apply_scope 做数据范围过滤；权限校验在 API 层用 require 完成。
"""
from __future__ import annotations

import csv
import io
import statistics
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from typing import Any

from fastapi import Request
from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.deps import apply_scope, effective_scope, visible_elder_ids
from app.core.enums import (
    ABILITY_NAMES,
    AbilityLevel,
    AlertLevel,
    AlertStatus,
    AuditAction,
    BillStatus,
    DataScope,
    OrderStatus,
    OrgLevel,
    RoleCode,
)
from app.core.exceptions import RuleViolation
from app.core.export import safe_cell
from app.db.base import utcnow
from app.models import (
    Alert,
    Bill,
    Elder,
    Evaluation,
    Org,
    ServiceItem,
    Staff,
    User,
    WorkOrder,
)

CENT = Decimal("0.01")

COMPLETED = OrderStatus.COMPLETED.value
PENDING_ORDER_STATUSES = (
    OrderStatus.PENDING_ACCEPT.value,
    OrderStatus.ACCEPTED.value,
    OrderStatus.IN_SERVICE.value,
    OrderStatus.PENDING_EVALUATION.value,
)

TREND_METRICS = ("orders", "alerts", "satisfaction")
GRANULARITIES = ("day", "week", "month")
RANKING_TYPES = ("nurse", "community")
DISTRIBUTION_TYPES = ("ability", "satisfaction", "service_category")
EXPORT_REPORTS = ("orders", "billing")

ABILITY_ORDER = (
    AbilityLevel.INTACT,
    AbilityLevel.MILD,
    AbilityLevel.MODERATE,
    AbilityLevel.SEVERE,
)

# 满意度分布：左闭右开区间，覆盖 0-5 分
SATISFACTION_BUCKETS = (
    ("5星", 4.5, 5.01),
    ("4星", 3.5, 4.5),
    ("3星", 2.5, 3.5),
    ("2星", 1.5, 2.5),
    ("1星", 0.0, 1.5),
)

SERVICE_CATEGORY_NAMES = {
    "bath": "助浴",
    "cleaning": "保洁",
    "meal": "助餐",
    "medical": "医疗护理",
    "bedcare": "卧床照护",
    "escort": "陪同外出",
    "rehab": "康复训练",
    "other": "其他",
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

BILL_STATUS_NAMES = {
    BillStatus.DRAFT.value: "草稿",
    BillStatus.PENDING_REVIEW.value: "待审核",
    BillStatus.CONFIRMED.value: "已确认",
    BillStatus.PAID.value: "已支付",
    BillStatus.REVERSED.value: "已冲正",
}


# ================================================================ 通用工具
def _q(value: Any) -> Decimal:
    """金额量化到分。"""
    return Decimal(str(value or 0)).quantize(CENT)


def _money(value: Any) -> float:
    return float(_q(value))


def _ratio(part: float, total: float) -> float:
    return round(part / total, 4) if total else 0.0


def _parse_date(value: str | None, default: date) -> date:
    if value is None or not str(value).strip():
        return default
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise RuleViolation("日期格式应为 YYYY-MM-DD") from exc


def resolve_range(from_: str | None, to: str | None) -> tuple[date, date]:
    """解析 from/to（YYYY-MM-DD），缺省为近 30 天（含今天）。"""
    end = _parse_date(to, utcnow().date())
    start = _parse_date(from_, end - timedelta(days=29))
    if start > end:
        raise RuleViolation("from 不能晚于 to")
    return start, end


def _month_range(period: str | None) -> tuple[str, datetime, datetime]:
    """把 YYYY-MM 解析为 [start, end)。"""
    text = (period or "").strip() or utcnow().strftime("%Y-%m")
    parts = text.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise RuleViolation("period 格式应为 YYYY-MM")
    try:
        year, month = int(parts[0]), int(parts[1])
        start = datetime(year, month, 1)
    except ValueError as exc:
        raise RuleViolation("period 格式应为 YYYY-MM") from exc
    if not 1 <= month <= 12 or year < 1970:
        raise RuleViolation("period 格式应为 YYYY-MM")
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return f"{year:04d}-{month:02d}", start, end


def _unrestricted(user: User) -> bool:
    role_code = user.role.code if user.role else RoleCode.ELDER.value
    return role_code in (RoleCode.SUPER.value, RoleCode.STREET.value) or (
        effective_scope(user) == DataScope.STREET.value
    )


def _scoped(stmt, model, user: User, db: Session):
    """apply_scope + 明细表按可见老人补过滤（Evaluation/Alert 无组织列）。"""
    stmt = apply_scope(stmt, model, user, db)
    if _unrestricted(user):
        return stmt
    has_org_column = any(
        getattr(model, column, None) is not None
        for column in ("community_org_id", "grid_org_id", "org_id")
    )
    if has_org_column or getattr(model, "elder_id", None) is None:
        return stmt
    if effective_scope(user) in (DataScope.ORG.value, DataScope.GRID.value):
        elder_ids = visible_elder_ids(db, user)
        if elder_ids is not None:
            stmt = stmt.where(model.elder_id.in_(elder_ids))
    return stmt


def _scalar(db: Session, stmt) -> int:
    value = db.execute(stmt).scalar_one_or_none()
    return int(value or 0)


def _ancestor_community_id(db: Session, org_id: int | None) -> int | None:
    org = db.get(Org, org_id) if org_id else None
    hops = 0
    while org is not None and hops < 6:
        if org.level == OrgLevel.COMMUNITY.value:
            return org.id
        if org.parent_id is None:
            return None
        org = db.get(Org, org.parent_id)
        hops += 1
    return None


def _visible_communities(db: Session, user: User) -> list[Org]:
    stmt = (
        select(Org)
        .where(Org.level == OrgLevel.COMMUNITY.value, Org.is_active.is_(True))
        .order_by(Org.sort.asc(), Org.id.asc())
    )
    stmt = apply_scope(stmt, Org, user, db)
    if _unrestricted(user):
        if user.org_id:
            org = db.get(Org, user.org_id)
            if org is not None and org.level == OrgLevel.STREET.value:
                stmt = stmt.where(Org.parent_id == org.id)
    else:
        community_id = _ancestor_community_id(db, user.org_id)
        stmt = stmt.where(Org.id == community_id) if community_id else stmt.where(false())
    return list(db.execute(stmt).scalars().all())


def _staff_names(db: Session, staff_ids: list[int]) -> dict[int, str]:
    if not staff_ids:
        return {}
    rows = db.execute(select(Staff.id, Staff.name).where(Staff.id.in_(staff_ids))).all()
    return {int(row[0]): row[1] for row in rows}


def _org_names(db: Session, org_ids: list[int]) -> dict[int, str]:
    if not org_ids:
        return {}
    rows = db.execute(select(Org.id, Org.name).where(Org.id.in_(org_ids))).all()
    return {int(row[0]): row[1] for row in rows}


# ================================================================ 1) 社区看板
def _response_minutes(seconds: Any, created: Any, resolved: Any) -> float | None:
    if seconds is not None:
        return float(seconds) / 60.0
    if isinstance(created, datetime) and isinstance(resolved, datetime):
        return max(0.0, (resolved - created).total_seconds() / 60.0)
    return None


def community_overview(db: Session, user: User) -> dict[str, Any]:
    """社区看板总览：本月完单、服务覆盖率、待办、预警响应与满意度。

    service_count 只统计本月 completed 工单；coverage_rate = 有 completed 工单的老人数 /
    辖区老人总数；响应时长/满意度无数据时返回 null。
    """
    now = utcnow()
    month_start = datetime(now.year, now.month, 1)
    month_end = datetime(now.year + (1 if now.month == 12 else 0), 1 if now.month == 12 else now.month + 1, 1)

    total_elders = _scalar(
        db,
        _scoped(select(func.count(Elder.id)).where(Elder.is_deleted == 0), Elder, user, db),
    )
    served_elders = _scalar(
        db,
        _scoped(
            select(func.count(func.distinct(WorkOrder.elder_id))).where(
                WorkOrder.status == COMPLETED,
                WorkOrder.completed_at.is_not(None),
                WorkOrder.completed_at >= month_start,
                WorkOrder.completed_at < month_end,
            ),
            WorkOrder,
            user,
            db,
        ),
    )
    service_count = _scalar(
        db,
        _scoped(
            select(func.count(WorkOrder.id)).where(
                WorkOrder.status == COMPLETED,
                WorkOrder.completed_at.is_not(None),
                WorkOrder.completed_at >= month_start,
                WorkOrder.completed_at < month_end,
            ),
            WorkOrder,
            user,
            db,
        ),
    )
    pending_orders = _scalar(
        db,
        _scoped(
            select(func.count(WorkOrder.id)).where(WorkOrder.status.in_(PENDING_ORDER_STATUSES)),
            WorkOrder,
            user,
            db,
        ),
    )
    pending_alerts = _scalar(
        db,
        _scoped(
            select(func.count(Alert.id)).where(Alert.status == AlertStatus.PENDING.value),
            Alert,
            user,
            db,
        ),
    )

    alert_rows = db.execute(
        _scoped(
            select(Alert.response_seconds, Alert.created_at, Alert.resolved_at).where(
                or_(
                    Alert.response_seconds.is_not(None),
                    Alert.resolved_at.is_not(None),
                )
            ),
            Alert,
            user,
            db,
        )
    ).all()
    minutes = [_response_minutes(row[0], row[1], row[2]) for row in alert_rows]
    minutes = [value for value in minutes if value is not None]

    satisfaction = db.execute(
        _scoped(select(func.avg(Evaluation.avg_score)), Evaluation, user, db)
    ).scalar_one_or_none()

    return {
        "service_count": service_count,
        "coverage_rate": _ratio(served_elders, total_elders),
        "pending_orders": pending_orders,
        "pending_alerts": pending_alerts,
        "avg_alert_response_minutes": round(sum(minutes) / len(minutes), 2) if minutes else None,
        "median_alert_response_minutes": round(float(statistics.median(minutes)), 2)
        if minutes
        else None,
        "satisfaction_avg": round(float(satisfaction), 2) if satisfaction is not None else None,
    }


# ================================================================ 2) 机构看板
def org_overview(db: Session, user: User) -> dict[str, Any]:
    """机构看板：护理员数、本月人均单量、平均评分、收入构成。"""
    now = utcnow()
    month_start = datetime(now.year, now.month, 1)

    staff_rows = db.execute(
        _scoped(
            select(Staff.id, Staff.rating_cached).where(Staff.status == "active"),
            Staff,
            user,
            db,
        )
    ).all()
    nurse_ids = [int(row[0]) for row in staff_rows]
    nurse_count = len(nurse_ids)

    completed = 0
    if nurse_ids:
        completed = _scalar(
            db,
            _scoped(
                select(func.count(WorkOrder.id)).where(
                    WorkOrder.status == COMPLETED,
                    WorkOrder.completed_at.is_not(None),
                    WorkOrder.completed_at >= month_start,
                    WorkOrder.nurse_id.in_(nurse_ids),
                ),
                WorkOrder,
                user,
                db,
            ),
        )

    scores = db.execute(
        _scoped(select(Evaluation.avg_score), Evaluation, user, db)
    ).scalars().all()
    scores = [float(score) for score in scores if score is not None]
    if scores:
        avg_rating: float | None = round(sum(scores) / len(scores), 2)
    else:
        cached = [float(row[1]) for row in staff_rows if row[1] is not None]
        avg_rating = round(sum(cached) / len(cached), 2) if cached else None

    income_row = db.execute(
        _scoped(
            select(
                func.coalesce(func.sum(Bill.subsidy_amount), 0),
                func.coalesce(func.sum(Bill.ltc_amount), 0),
                func.coalesce(func.sum(Bill.self_pay_amount), 0),
            ).where(Bill.status != BillStatus.REVERSED.value),
            Bill,
            user,
            db,
        )
    ).one()

    return {
        "nurse_count": nurse_count,
        "avg_orders_per_nurse": round(completed / nurse_count, 2) if nurse_count else 0.0,
        "avg_rating": avg_rating,
        "income": _income_structure(income_row[0], income_row[1], income_row[2]),
    }


def _income_structure(subsidy: Any, ltc: Any, self_pay: Any) -> dict[str, Any]:
    """收入结构：金额 + 占比（合计为 1；总额为 0 时占比为 0）。"""
    subsidy_amount = _money(subsidy)
    ltc_amount = _money(ltc)
    self_pay_amount = _money(self_pay)
    total = round(subsidy_amount + ltc_amount + self_pay_amount, 2)
    return {
        "subsidy": subsidy_amount,
        "ltc": ltc_amount,
        "self_pay": self_pay_amount,
        "total": total,
        "subsidy_ratio": round(subsidy_amount / total, 4) if total else 0.0,
        "ltc_ratio": round(ltc_amount / total, 4) if total else 0.0,
        "self_pay_ratio": round(self_pay_amount / total, 4) if total else 0.0,
    }


# ================================================================ 3) 街道看板
def street_overview(db: Session, user: User) -> dict[str, Any]:
    """街道看板：各社区完单/补贴/满意度 + 升级与红色预警。

    red_alerts 只统计仍处于 pending 的红色预警；alert_escalated 统计已升级（escalated_at 非空）的预警；
    total_subsidy 为各社区 bill.subsidy_amount 之和（排除 reversed）。
    """
    communities = _visible_communities(db, user)
    community_ids = [community.id for community in communities]

    orders: dict[int, int] = {}
    subsidy: dict[int, Decimal] = {}
    satisfaction: dict[int, float] = {}

    if community_ids:
        order_rows = db.execute(
            _scoped(
                select(WorkOrder.community_org_id, func.count(WorkOrder.id))
                .where(
                    WorkOrder.status == COMPLETED,
                    WorkOrder.community_org_id.in_(community_ids),
                )
                .group_by(WorkOrder.community_org_id),
                WorkOrder,
                user,
                db,
            )
        ).all()
        orders = {int(row[0]): int(row[1]) for row in order_rows}

        subsidy_rows = db.execute(
            _scoped(
                select(
                    Bill.community_org_id,
                    func.coalesce(func.sum(Bill.subsidy_amount), 0),
                )
                .where(
                    Bill.community_org_id.in_(community_ids),
                    Bill.status != BillStatus.REVERSED.value,
                )
                .group_by(Bill.community_org_id),
                Bill,
                user,
                db,
            )
        ).all()
        subsidy = {int(row[0]): _q(row[1]) for row in subsidy_rows}

        satisfaction_rows = db.execute(
            _scoped(
                select(WorkOrder.community_org_id, func.avg(Evaluation.avg_score))
                .join(Evaluation, Evaluation.work_order_id == WorkOrder.id)
                .where(WorkOrder.community_org_id.in_(community_ids))
                .group_by(WorkOrder.community_org_id),
                WorkOrder,
                user,
                db,
            )
        ).all()
        satisfaction = {
            int(row[0]): round(float(row[1]), 2) for row in satisfaction_rows if row[1] is not None
        }

    alert_escalated = _scalar(
        db,
        _scoped(
            select(func.count(Alert.id)).where(Alert.escalated_at.is_not(None)),
            Alert,
            user,
            db,
        ),
    )
    red_alerts = _scalar(
        db,
        _scoped(
            select(func.count(Alert.id)).where(
                Alert.level == AlertLevel.RED.value,
                Alert.status == AlertStatus.PENDING.value,
            ),
            Alert,
            user,
            db,
        ),
    )

    sat_values: list[Any] = []
    if community_ids:
        sat_values = list(
            db.execute(
                _scoped(
                    select(Evaluation.avg_score)
                    .join(WorkOrder, WorkOrder.id == Evaluation.work_order_id)
                    .where(WorkOrder.community_org_id.in_(community_ids)),
                    Evaluation,
                    user,
                    db,
                )
            ).scalars().all()
        )
    sat_buckets = [
        (name, sum(1 for score in sat_values if score is not None and low <= float(score) < high))
        for name, low, high in SATISFACTION_BUCKETS
    ]

    items = [
        {
            "org_id": community.id,
            "name": community.name,
            "orders": orders.get(community.id, 0),
            "subsidy": _money(subsidy.get(community.id, Decimal("0"))),
            "satisfaction": satisfaction.get(community.id),
        }
        for community in communities
    ]
    total_subsidy = sum((subsidy.get(community.id, Decimal("0")) for community in communities),
                        Decimal("0"))

    return {
        "communities": items,
        "alert_escalated": alert_escalated,
        "red_alerts": red_alerts,
        "total_subsidy": _money(total_subsidy),
        "satisfaction_distribution": _distribution_rows(sat_buckets),
    }


# ================================================================ 4) 趋势
def _bucket_span(moment: datetime, granularity: str) -> tuple[datetime, datetime]:
    if granularity == "day":
        start = datetime(moment.year, moment.month, moment.day)
        return start, start + timedelta(days=1)
    if granularity == "week":
        start = datetime(moment.year, moment.month, moment.day) - timedelta(days=moment.weekday())
        return start, start + timedelta(days=7)
    start = datetime(moment.year, moment.month, 1)
    end = datetime(moment.year + 1, 1, 1) if moment.month == 12 else datetime(
        moment.year, moment.month + 1, 1
    )
    return start, end


def _bucket_label(start: datetime, granularity: str) -> str:
    if granularity == "day":
        return start.strftime("%Y-%m-%d")
    if granularity == "week":
        iso = start.date().isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return start.strftime("%Y-%m")


def _buckets(start_dt: datetime, end_dt: datetime, granularity: str) -> list[tuple[str, datetime, datetime]]:
    out: list[tuple[str, datetime, datetime]] = []
    start, end = _bucket_span(start_dt, granularity)
    while start < end_dt:
        out.append((_bucket_label(start, granularity), start, end))
        start, end = end, _bucket_span(end, granularity)[1]
    return out


def _shift_year(moment: datetime) -> datetime:
    try:
        return moment.replace(year=moment.year - 1)
    except ValueError:  # 2 月 29 日
        return moment.replace(year=moment.year - 1, day=28)


def _bucket_value(rows: list[Any], start: datetime, end: datetime, metric: str) -> float | int | None:
    if metric == "satisfaction":
        values = [
            float(row[1])
            for row in rows
            if row[0] is not None and start <= row[0] < end and row[1] is not None
        ]
        if not values:
            return None
        return round(sum(values) / len(values), 2)
    return sum(1 for row in rows if row[0] is not None and start <= row[0] < end)


def _trend_number(raw: Any, metric: str, *, last_year: bool = False) -> float | int | None:
    if metric == "satisfaction":
        if raw is None:
            return None if last_year else 0.0
        return round(float(raw), 2)
    count = int(raw or 0)
    if last_year and count == 0:
        return None
    return count


def _mom(value: Any, prev: Any) -> float | None:
    if value is None or prev is None or float(prev) == 0:
        return None
    return round((float(value) - float(prev)) / float(prev), 4)


def _trend_rows(db: Session, user: User, metric: str, start_dt: datetime, end_dt: datetime) -> list[Any]:
    if metric == "orders":
        stmt = select(WorkOrder.completed_at).where(
            WorkOrder.status == COMPLETED,
            WorkOrder.completed_at.is_not(None),
            WorkOrder.completed_at >= start_dt,
            WorkOrder.completed_at < end_dt,
        )
        return db.execute(_scoped(stmt, WorkOrder, user, db)).all()
    if metric == "alerts":
        stmt = select(Alert.created_at).where(
            Alert.created_at >= start_dt, Alert.created_at < end_dt
        )
        return db.execute(_scoped(stmt, Alert, user, db)).all()
    stmt = select(Evaluation.created_at, Evaluation.avg_score).where(
        Evaluation.created_at >= start_dt, Evaluation.created_at < end_dt
    )
    return db.execute(_scoped(stmt, Evaluation, user, db)).all()


def trend(
    db: Session,
    user: User,
    metric: str = "orders",
    from_: str | None = None,
    to: str | None = None,
    granularity: str = "day",
) -> list[dict[str, Any]]:
    """趋势数据：period/value/last_year/mom，缺数据补零，去年无数据返回 null。

    period 格式：day → YYYY-MM-DD，week → YYYY-Www（ISO 周），month → YYYY-MM；
    mom =（本周期 - 上一周期）/ 上一周期，上一周期无基数时为 null。
    """
    metric = (metric or "orders").strip()
    if metric not in TREND_METRICS:
        raise RuleViolation("metric 仅支持 orders / alerts / satisfaction")
    granularity = (granularity or "day").strip()
    if granularity not in GRANULARITIES:
        raise RuleViolation("granularity 仅支持 day / week / month")

    start, end = resolve_range(from_, to)
    start_dt = datetime.combine(start, dt_time.min)
    end_dt = datetime.combine(end, dt_time.min) + timedelta(days=1)
    buckets = _buckets(start_dt, end_dt, granularity)

    prev_start, prev_end = _bucket_span(buckets[0][1] - timedelta(microseconds=1), granularity)
    data_from = min(prev_start, _shift_year(buckets[0][1]))
    data_to = buckets[-1][2]
    rows = _trend_rows(db, user, metric, data_from, data_to)

    previous_raw = _bucket_value(rows, prev_start, prev_end, metric)
    out: list[dict[str, Any]] = []
    for label, bucket_start, bucket_end in buckets:
        raw = _bucket_value(rows, bucket_start, bucket_end, metric)
        value = _trend_number(raw, metric)
        last_year = _trend_number(
            _bucket_value(rows, _shift_year(bucket_start), _shift_year(bucket_end), metric),
            metric,
            last_year=True,
        )
        out.append(
            {
                "period": label,
                "value": value,
                "last_year": last_year,
                "mom": _mom(value, _trend_number(previous_raw, metric)),
            }
        )
        previous_raw = raw
    return out


# ================================================================ 5) 排行榜
def _staff_ranking(db: Session, user: User, start: datetime, end: datetime) -> list[dict[str, Any]]:
    stmt = (
        select(WorkOrder.nurse_id, func.count(WorkOrder.id), func.avg(WorkOrder.rating))
        .where(
            WorkOrder.status == COMPLETED,
            WorkOrder.completed_at.is_not(None),
            WorkOrder.completed_at >= start,
            WorkOrder.completed_at < end,
            WorkOrder.nurse_id.is_not(None),
        )
        .group_by(WorkOrder.nurse_id)
    )
    rows = db.execute(_scoped(stmt, WorkOrder, user, db)).all()
    rows = sorted(
        rows,
        key=lambda row: (
            -int(row[1]),
            -(float(row[2]) if row[2] is not None else 0.0),
            int(row[0]),
        ),
    )[:10]
    names = _staff_names(db, [int(row[0]) for row in rows])
    return [
        {
            "rank": index + 1,
            "nurse_id": int(row[0]),
            "name": names.get(int(row[0]), f"护理员{row[0]}"),
            "orders": int(row[1]),
            "avg_rating": round(float(row[2]), 2) if row[2] is not None else None,
        }
        for index, row in enumerate(rows)
    ]


def _community_ranking(db: Session, user: User, start: datetime, end: datetime) -> list[dict[str, Any]]:
    community_ids = [community.id for community in _visible_communities(db, user)]
    if not community_ids:
        return []
    stmt = (
        select(WorkOrder.community_org_id, func.count(WorkOrder.id))
        .where(
            WorkOrder.status == COMPLETED,
            WorkOrder.completed_at.is_not(None),
            WorkOrder.completed_at >= start,
            WorkOrder.completed_at < end,
            WorkOrder.community_org_id.in_(community_ids),
        )
        .group_by(WorkOrder.community_org_id)
    )
    rows = db.execute(_scoped(stmt, WorkOrder, user, db)).all()
    rows = sorted(rows, key=lambda row: (-int(row[1]), int(row[0])))[:10]
    names = _org_names(db, [int(row[0]) for row in rows])
    return [
        {
            "rank": index + 1,
            "org_id": int(row[0]),
            "name": names.get(int(row[0]), f"社区{row[0]}"),
            "orders": int(row[1]),
        }
        for index, row in enumerate(rows)
    ]


def ranking(db: Session, user: User, type_: str = "nurse", period: str | None = None) -> list[dict[str, Any]]:
    """排行榜：护理员按完单量 / 社区按完单量，取前 10。"""
    ranking_type = (type_ or "nurse").strip()
    if ranking_type not in RANKING_TYPES:
        raise RuleViolation("type 仅支持 nurse / community")
    _, start, end = _month_range(period)
    if ranking_type == "nurse":
        return _staff_ranking(db, user, start, end)
    return _community_ranking(db, user, start, end)


# ================================================================ 6) 分布
def _distribution_rows(buckets: list[tuple[str, int]]) -> list[dict[str, Any]]:
    """分布行：ratio 保留 4 位且合计严格为 1（修正最后一个非零档的舍入误差）。"""
    total = sum(value for _, value in buckets)
    if not total:
        return [{"name": name, "value": int(value), "ratio": 0.0} for name, value in buckets]

    last_index = max((index for index, (_, value) in enumerate(buckets) if value), default=None)
    rows: list[dict[str, Any]] = []
    accumulated = 0.0
    for index, (name, value) in enumerate(buckets):
        if index == last_index:
            ratio = round(1 - accumulated, 4)
        else:
            ratio = _ratio(value, total)
            accumulated += ratio
        rows.append({"name": name, "value": int(value), "ratio": ratio})
    return rows


def distribution(db: Session, user: User, type_: str = "ability") -> list[dict[str, Any]]:
    """分布统计：能力等级 / 满意度 / 服务类别，ratio 合计为 1（无数据为 0）。"""
    dist_type = (type_ or "ability").strip()
    if dist_type not in DISTRIBUTION_TYPES:
        raise RuleViolation("type 仅支持 ability / satisfaction / service_category")

    if dist_type == "ability":
        rows = db.execute(
            _scoped(
                select(Elder.ability_level, func.count(Elder.id))
                .where(Elder.is_deleted == 0)
                .group_by(Elder.ability_level),
                Elder,
                user,
                db,
            )
        ).all()
        counts = {str(row[0]): int(row[1]) for row in rows}
        buckets = [
            (ABILITY_NAMES[level], counts.get(level.value, 0))
            for level in ABILITY_ORDER
        ]
        return _distribution_rows(buckets)

    if dist_type == "satisfaction":
        scores = db.execute(
            _scoped(select(Evaluation.avg_score), Evaluation, user, db)
        ).scalars().all()
        values = [float(score) for score in scores if score is not None]
        buckets = [
            (name, sum(1 for score in values if low <= score < high))
            for name, low, high in SATISFACTION_BUCKETS
        ]
        return _distribution_rows(buckets)

    rows = db.execute(
        _scoped(
            select(ServiceItem.category, func.count(WorkOrder.id))
            .select_from(ServiceItem)
            .join(WorkOrder, WorkOrder.service_item_id == ServiceItem.id)
            .where(WorkOrder.status == COMPLETED)
            .group_by(ServiceItem.category),
            WorkOrder,
            user,
            db,
        )
    ).all()
    buckets = [
        (SERVICE_CATEGORY_NAMES.get(str(row[0]), str(row[0])), int(row[1]))
        for row in rows
        if row[1]
    ]
    buckets.sort(key=lambda item: (-item[1], item[0]))
    return _distribution_rows(buckets)


# ================================================================ 7) 导出
def _orders_export(db: Session, user: User, start_dt: datetime, end_dt: datetime) -> tuple[list[str], list[list[Any]]]:
    headers = [
        "工单号",
        "老人编号",
        "老人姓名",
        "服务项目",
        "护理员",
        "所属社区",
        "状态",
        "计划开始时间",
        "完成时间",
        "金额(元)",
        "评分",
    ]
    stmt = (
        select(WorkOrder, Elder, Org, Staff, ServiceItem)
        .outerjoin(Elder, Elder.id == WorkOrder.elder_id)
        .outerjoin(Org, Org.id == WorkOrder.community_org_id)
        .outerjoin(Staff, Staff.id == WorkOrder.nurse_id)
        .outerjoin(ServiceItem, ServiceItem.id == WorkOrder.service_item_id)
        .where(
            WorkOrder.scheduled_start >= start_dt,
            WorkOrder.scheduled_start < end_dt,
        )
        .order_by(WorkOrder.scheduled_start.asc(), WorkOrder.id.asc())
    )
    rows = db.execute(_scoped(stmt, WorkOrder, user, db)).all()
    data: list[list[Any]] = []
    for order, elder, org, staff, item in rows:
        data.append(
            [
                order.code,
                elder.code if elder is not None else "",
                elder.name if elder is not None else "",
                item.name if item is not None else "",
                staff.name if staff is not None else "",
                org.name if org is not None else "",
                ORDER_STATUS_NAMES.get(order.status, order.status),
                order.scheduled_start.strftime("%Y-%m-%d %H:%M") if order.scheduled_start else "",
                order.completed_at.strftime("%Y-%m-%d %H:%M") if order.completed_at else "",
                f"{_q(order.amount):.2f}",
                "" if order.rating is None else f"{float(order.rating):.2f}",
            ]
        )
    return headers, data


def _billing_export(db: Session, user: User, start: date, end: date) -> tuple[list[str], list[list[Any]]]:
    headers = [
        "账单号",
        "所属社区",
        "账期",
        "总额(元)",
        "补贴(元)",
        "长护险(元)",
        "自付(元)",
        "状态",
    ]
    stmt = (
        select(Bill, Org)
        .outerjoin(Org, Org.id == Bill.community_org_id)
        .where(
            Bill.period >= start.strftime("%Y-%m"),
            Bill.period <= end.strftime("%Y-%m"),
        )
        .order_by(Bill.period.asc(), Bill.code.asc())
    )
    rows = db.execute(_scoped(stmt, Bill, user, db)).all()
    data: list[list[Any]] = []
    for bill, org in rows:
        data.append(
            [
                bill.code,
                org.name if org is not None else "",
                bill.period,
                f"{_q(bill.total_amount):.2f}",
                f"{_q(bill.subsidy_amount):.2f}",
                f"{_q(bill.ltc_amount):.2f}",
                f"{_q(bill.self_pay_amount):.2f}",
                BILL_STATUS_NAMES.get(bill.status, bill.status),
            ]
        )
    return headers, data


def export_report(
    db: Session,
    user: User,
    report: str = "orders",
    from_: str | None = None,
    to: str | None = None,
    request: Request | None = None,
) -> tuple[str, bytes]:
    """导出 CSV（utf-8-sig，含 BOM），并写入 EXPORT 审计。返回 (filename, content)。"""
    report_type = (report or "orders").strip()
    if report_type not in EXPORT_REPORTS:
        raise RuleViolation("report 仅支持 orders / billing")

    start, end = resolve_range(from_, to)
    start_dt = datetime.combine(start, dt_time.min)
    end_dt = datetime.combine(end, dt_time.min) + timedelta(days=1)

    if report_type == "orders":
        headers, rows = _orders_export(db, user, start_dt, end_dt)
    else:
        headers, rows = _billing_export(db, user, start, end)

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows([safe_cell(value) for value in row] for row in rows)
    content = buffer.getvalue().encode("utf-8-sig")
    filename = f"stats_{report_type}_{start:%Y%m%d}_{end:%Y%m%d}.csv"

    write_audit(
        db,
        user=user,
        action=AuditAction.EXPORT.value,
        entity_type="stats",
        detail=f"导出统计报表 {report_type}（{start}~{end}），共 {len(rows)} 行",
        request=request,
    )
    db.commit()
    return filename, content
