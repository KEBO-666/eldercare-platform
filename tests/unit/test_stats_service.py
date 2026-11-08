"""E2 统计服务单元测试（聚合口径、趋势、排行、分布、导出）。"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.enums import (
    AbilityLevel,
    AlertLevel,
    AlertStatus,
    BillStatus,
    OrderStatus,
)
from app.core.exceptions import RuleViolation
from app.models import (
    Alert,
    AuditLog,
    Bill,
    Evaluation,
    ServiceItem,
    WorkOrder,
)
from app.services import stats_service

TODAY = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
MONTH = TODAY.strftime("%Y-%m")
DAY = "%Y-%m-%d"
_SEQ = {"n": 0}


def _code(prefix: str) -> str:
    _SEQ["n"] += 1
    return f"{prefix}{_SEQ['n']:04d}"


def _shift_year(moment: datetime) -> datetime:
    try:
        return moment.replace(year=moment.year - 1)
    except ValueError:  # 2 月 29 日
        return moment.replace(year=moment.year - 1, day=28)


def _add_order(
    db,
    elder,
    item,
    *,
    status=OrderStatus.COMPLETED.value,
    completed_at=TODAY,
    nurse_id=None,
    rating=None,
    community_org_id=None,
    scheduled_start=None,
    amount=Decimal("120.00"),
    code=None,
):
    order = WorkOrder(
        code=code or _code("WO"),
        elder_id=elder.id,
        service_item_id=item.id,
        nurse_id=nurse_id,
        status=status,
        scheduled_start=scheduled_start or completed_at or TODAY,
        completed_at=completed_at,
        community_org_id=community_org_id or elder.community_org_id,
        unit_price=amount,
        amount=amount,
        rating=rating,
    )
    db.add(order)
    db.flush()
    return order


def _add_alert(
    db,
    elder,
    *,
    level=AlertLevel.BLUE.value,
    status=AlertStatus.PENDING.value,
    response_seconds=None,
    created_at=None,
    resolved_at=None,
    escalated_at=None,
):
    alert = Alert(
        elder_id=elder.id,
        level=level,
        status=status,
        response_seconds=response_seconds,
        created_at=created_at or TODAY,
        resolved_at=resolved_at,
        escalated_at=escalated_at,
        summary="测试预警",
    )
    db.add(alert)
    db.flush()
    return alert


def _add_evaluation(db, order, elder, *, score=5.0, nurse_id=None, created_at=None):
    evaluation = Evaluation(
        work_order_id=order.id,
        elder_id=elder.id,
        nurse_id=nurse_id or order.nurse_id,
        avg_score=score,
        created_at=created_at or TODAY,
    )
    db.add(evaluation)
    db.flush()
    return evaluation


def _add_bill(
    db,
    org_id,
    *,
    subsidy="0.00",
    ltc="0.00",
    self_pay="0.00",
    total=None,
    period=MONTH,
    status=BillStatus.CONFIRMED.value,
):
    total_amount = (
        Decimal(total)
        if total is not None
        else Decimal(subsidy) + Decimal(ltc) + Decimal(self_pay)
    )
    bill = Bill(
        code=_code("BL"),
        community_org_id=org_id,
        period=period,
        total_amount=total_amount,
        subsidy_amount=Decimal(subsidy),
        ltc_amount=Decimal(ltc),
        self_pay_amount=Decimal(self_pay),
        status=status,
    )
    db.add(bill)
    db.flush()
    return bill


# ---------------------------------------------------------------- 1) 社区看板
def test_community_overview_only_completed_and_coverage(db, orgs, accounts, make_elder):
    elder = accounts["elder"]
    item = accounts["item"]
    user = accounts["users"]["street"]
    make_elder()  # 辖区老人 2
    make_elder()  # 辖区老人 3

    empty = stats_service.community_overview(db, user)
    assert empty["service_count"] == 0
    assert empty["coverage_rate"] == 0.0
    assert empty["pending_orders"] == 0
    assert empty["pending_alerts"] == 0
    assert empty["avg_alert_response_minutes"] is None
    assert empty["median_alert_response_minutes"] is None
    assert empty["satisfaction_avg"] is None

    completed = _add_order(db, elder, item)
    _add_order(db, elder, item, status=OrderStatus.IN_SERVICE.value, completed_at=None)
    _add_order(db, elder, item, status=OrderStatus.PENDING_ACCEPT.value, completed_at=None)
    _add_alert(db, elder)
    _add_alert(db, elder, status=AlertStatus.RESOLVED.value, response_seconds=60, resolved_at=TODAY)
    _add_alert(db, elder, status=AlertStatus.RESOLVED.value, response_seconds=180, resolved_at=TODAY)
    _add_alert(db, elder, status=AlertStatus.RESOLVED.value, response_seconds=600, resolved_at=TODAY)
    _add_evaluation(db, completed, elder, score=4.5)
    db.commit()

    data = stats_service.community_overview(db, user)
    assert data["service_count"] == 1  # 3 张不同状态工单只算 completed
    assert data["coverage_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert data["pending_orders"] == 2
    assert data["pending_alerts"] == 1
    assert data["avg_alert_response_minutes"] == pytest.approx(4.67, abs=0.01)
    assert data["median_alert_response_minutes"] == pytest.approx(3.0)
    assert data["satisfaction_avg"] == pytest.approx(4.5)


# ---------------------------------------------------------------- 2) 机构看板
def test_org_overview_nurses_rating_and_income(db, orgs, accounts, make_staff):
    street = accounts["users"]["street"]
    org_admin = accounts["users"]["org_admin"]
    staff1 = accounts["staff"]
    staff2 = make_staff()
    elder = accounts["elder"]
    item = accounts["item"]

    empty = stats_service.org_overview(db, org_admin)
    assert empty["nurse_count"] == 2
    assert empty["avg_orders_per_nurse"] == 0.0
    assert empty["avg_rating"] is None
    assert empty["income"]["subsidy"] == 0.0
    assert empty["income"]["total"] == 0.0
    assert empty["income"]["subsidy_ratio"] == 0.0

    order1 = _add_order(db, elder, item, nurse_id=staff1.id, rating=5.0)
    order2 = _add_order(db, elder, item, nurse_id=staff1.id, rating=4.0)
    _add_order(db, elder, item, nurse_id=staff2.id, rating=5.0)
    _add_evaluation(db, order1, elder, score=4.0)
    _add_evaluation(db, order2, elder, score=5.0)
    _add_bill(db, orgs["community1"].id, subsidy="100.00", ltc="50.00", self_pay="30.00")
    _add_bill(db, orgs["community2"].id, subsidy="10.00", ltc="5.00", self_pay="5.00")
    db.commit()

    own = stats_service.org_overview(db, org_admin)
    assert own["nurse_count"] == 2
    assert own["avg_orders_per_nurse"] == pytest.approx(1.5)
    assert own["avg_rating"] == pytest.approx(4.5)
    assert own["income"]["subsidy"] == 100.0
    assert own["income"]["ltc"] == 50.0
    assert own["income"]["self_pay"] == 30.0
    assert own["income"]["total"] == 180.0
    assert own["income"]["subsidy_ratio"] == pytest.approx(0.5556, abs=1e-4)

    all_data = stats_service.org_overview(db, street)
    assert all_data["income"]["subsidy"] == 110.0
    assert all_data["income"]["ltc"] == 55.0
    assert all_data["income"]["self_pay"] == 35.0
    assert all_data["income"]["total"] == 200.0


# ---------------------------------------------------------------- 3) 街道看板
def test_street_overview_communities_alerts_and_subsidy(db, orgs, accounts, make_elder):
    elder = accounts["elder"]
    item = accounts["item"]
    user = accounts["users"]["street"]

    order1 = _add_order(db, elder, item)
    _add_order(db, elder, item, community_org_id=orgs["community2"].id)
    _add_bill(db, orgs["community1"].id, subsidy="100.00")
    _add_bill(db, orgs["community2"].id, subsidy="20.00")
    _add_evaluation(db, order1, elder, score=4.0)
    _add_alert(
        db,
        elder,
        level=AlertLevel.RED.value,
        status=AlertStatus.PENDING.value,
        escalated_at=TODAY,
    )
    _add_alert(
        db,
        elder,
        level=AlertLevel.RED.value,
        status=AlertStatus.RESOLVED.value,
        resolved_at=TODAY,
    )
    _add_alert(db, elder, level=AlertLevel.YELLOW.value, status=AlertStatus.PENDING.value)
    db.commit()

    data = stats_service.street_overview(db, user)
    assert [row["name"] for row in data["communities"]] == ["幸福社区", "安康社区"]
    first, second = data["communities"]
    assert first["org_id"] == orgs["community1"].id
    assert first["orders"] == 1
    assert first["subsidy"] == pytest.approx(100.0)
    assert first["satisfaction"] == pytest.approx(4.0)
    assert second["orders"] == 1
    assert second["subsidy"] == pytest.approx(20.0)
    assert second["satisfaction"] is None
    assert data["alert_escalated"] == 1
    assert data["red_alerts"] == 1  # 只统计待处置的红色预警
    assert data["total_subsidy"] == pytest.approx(120.0)

    own = stats_service.street_overview(db, accounts["users"]["org_admin"])
    assert [row["org_id"] for row in own["communities"]] == [orgs["community1"].id]


# ---------------------------------------------------------------- 4) 趋势
def test_trend_daily_zero_fill_and_year_over_year(db, orgs, accounts):
    elder = accounts["elder"]
    item = accounts["item"]
    user = accounts["users"]["street"]
    day1 = TODAY - timedelta(days=2)
    day2 = TODAY - timedelta(days=1)
    day3 = TODAY

    order_a = _add_order(db, elder, item, completed_at=day1)
    _add_order(db, elder, item, completed_at=day1)
    order_c = _add_order(db, elder, item, completed_at=day3)
    _add_order(db, elder, item, completed_at=_shift_year(day3))
    _add_alert(db, elder, created_at=day1)
    _add_alert(db, elder, created_at=day1)
    _add_alert(db, elder, created_at=day3)
    _add_evaluation(db, order_a, elder, score=4.0, created_at=day1)
    _add_evaluation(db, order_c, elder, score=5.0, created_at=day1)
    _add_evaluation(db, order_c, elder, score=3.0, created_at=day3)
    db.commit()

    from_text, to_text = day1.strftime(DAY), day3.strftime(DAY)

    orders = stats_service.trend(db, user, "orders", from_text, to_text, "day")
    assert [row["period"] for row in orders] == [
        day1.strftime(DAY),
        day2.strftime(DAY),
        day3.strftime(DAY),
    ]
    assert [row["value"] for row in orders] == [2, 0, 1]
    assert [row["last_year"] for row in orders] == [None, None, 1]
    assert orders[0]["mom"] is None
    assert orders[1]["mom"] == pytest.approx(-1.0)
    assert orders[2]["mom"] is None

    alerts = stats_service.trend(db, user, "alerts", from_text, to_text, "day")
    assert [row["value"] for row in alerts] == [2, 0, 1]
    assert alerts[1]["mom"] == pytest.approx(-1.0)

    satisfaction = stats_service.trend(db, user, "satisfaction", from_text, to_text, "day")
    assert [row["value"] for row in satisfaction] == [4.5, 0.0, 3.0]
    assert satisfaction[0]["last_year"] is None
    assert satisfaction[1]["mom"] == pytest.approx(-1.0)
    assert satisfaction[2]["mom"] is None

    weekly = stats_service.trend(db, user, "orders", from_text, to_text, "week")
    assert weekly
    assert len(weekly) <= 2
    assert all("W" in row["period"] for row in weekly)

    with pytest.raises(RuleViolation):
        stats_service.trend(db, user, "bogus", from_text, to_text)
    with pytest.raises(RuleViolation):
        stats_service.trend(db, user, "orders", from_text, to_text, "hour")
    with pytest.raises(RuleViolation):
        stats_service.trend(db, user, "orders", to_text, from_text)


# ---------------------------------------------------------------- 5) 排行榜
def test_ranking_sorted_and_scoped(db, orgs, accounts, make_staff):
    elder = accounts["elder"]
    item = accounts["item"]
    staff1 = accounts["staff"]
    staff2 = make_staff()
    user = accounts["users"]["street"]

    _add_order(db, elder, item, nurse_id=staff1.id, rating=5.0)
    _add_order(db, elder, item, nurse_id=staff1.id, rating=4.0)
    _add_order(
        db,
        elder,
        item,
        nurse_id=staff2.id,
        rating=5.0,
        community_org_id=orgs["community2"].id,
    )
    db.commit()

    nurses = stats_service.ranking(db, user, "nurse", MONTH)
    assert [row["nurse_id"] for row in nurses] == [staff1.id, staff2.id]
    assert [row["orders"] for row in nurses] == [2, 1]
    assert [row["rank"] for row in nurses] == [1, 2]
    assert nurses[0]["avg_rating"] == pytest.approx(4.5)

    communities = stats_service.ranking(db, user, "community", MONTH)
    assert communities[0]["org_id"] == orgs["community1"].id
    assert communities[0]["orders"] == 2
    assert communities[1]["org_id"] == orgs["community2"].id
    assert communities[1]["orders"] == 1

    scoped = stats_service.ranking(db, accounts["users"]["org_admin"], "community", MONTH)
    assert [row["org_id"] for row in scoped] == [orgs["community1"].id]

    with pytest.raises(RuleViolation):
        stats_service.ranking(db, user, "bogus")
    with pytest.raises(RuleViolation):
        stats_service.ranking(db, user, "nurse", "2026-13")


# ---------------------------------------------------------------- 6) 分布
def test_distribution_ratios_sum_to_one(db, orgs, accounts, make_elder):
    elder = accounts["elder"]
    item = accounts["item"]
    user = accounts["users"]["street"]
    make_elder(ability_level=AbilityLevel.INTACT.value)
    make_elder(ability_level=AbilityLevel.MILD.value)

    cleaning_item = ServiceItem(code="S9101", name="保洁", category="cleaning")
    db.add(cleaning_item)
    db.flush()

    order_bath = _add_order(db, elder, item)
    _add_order(db, elder, item)
    _add_order(db, elder, cleaning_item)
    _add_order(
        db,
        elder,
        cleaning_item,
        status=OrderStatus.IN_SERVICE.value,
        completed_at=None,
    )
    _add_evaluation(db, order_bath, elder, score=5.0)
    _add_evaluation(db, order_bath, elder, score=3.0)
    db.commit()

    ability = stats_service.distribution(db, user, "ability")
    assert [row["name"] for row in ability] == ["能力完好", "轻度失能", "中度失能", "重度失能"]
    assert [row["value"] for row in ability] == [1, 1, 1, 0]
    assert sum(row["ratio"] for row in ability) == pytest.approx(1.0, abs=1e-3)

    category = stats_service.distribution(db, user, "service_category")
    assert {row["name"]: row["value"] for row in category} == {"助浴": 2, "保洁": 1}
    assert sum(row["ratio"] for row in category) == pytest.approx(1.0, abs=1e-3)

    satisfaction = stats_service.distribution(db, user, "satisfaction")
    scores = {row["name"]: row["value"] for row in satisfaction}
    assert scores["5星"] == 1
    assert scores["3星"] == 1
    assert sum(row["ratio"] for row in satisfaction) == pytest.approx(1.0, abs=1e-3)

    with pytest.raises(RuleViolation):
        stats_service.distribution(db, user, "bogus")


# ---------------------------------------------------------------- 7) 导出
def test_export_csv_bom_rows_and_audit(db, orgs, accounts):
    elder = accounts["elder"]
    item = accounts["item"]
    user = accounts["users"]["street"]
    _add_order(db, elder, item, code="WO-EXPORT-1")
    bill = _add_bill(
        db,
        orgs["community1"].id,
        subsidy="88.00",
        ltc="0.00",
        self_pay="12.00",
        total="100.00",
    )
    db.commit()

    filename, content = stats_service.export_report(
        db, user, "orders", TODAY.strftime(DAY), TODAY.strftime(DAY)
    )
    assert filename.startswith("stats_orders_")
    assert filename.endswith(".csv")
    assert content.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    text = content.decode("utf-8-sig")
    assert "工单号" in text
    assert "WO-EXPORT-1" in text
    assert "已完成" in text

    bill_name, bill_content = stats_service.export_report(
        db, user, "billing", TODAY.strftime(DAY), TODAY.strftime(DAY)
    )
    assert bill_name.startswith("stats_billing_")
    bill_text = bill_content.decode("utf-8-sig")
    assert bill.code in bill_text
    assert "补贴(元)" in bill_text
    assert "88.00" in bill_text

    audit_count = db.execute(
        select(func.count(AuditLog.id)).where(AuditLog.action == "export")
    ).scalar_one()
    assert audit_count == 2

    with pytest.raises(RuleViolation):
        stats_service.export_report(db, user, "bogus")
