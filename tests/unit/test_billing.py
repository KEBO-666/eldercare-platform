"""E1 模块单测：补贴政策匹配、三方分账、月度结算、四类差异、红冲与定时任务。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import sessionmaker

from app.algorithms import subsidy
from app.core.enums import AbilityLevel, BillStatus, DiffStatus, OrderStatus
from app.core.exceptions import BadRequest, Conflict, RuleViolation
from app.models import (
    AuditLog,
    Bill,
    BillDetail,
    ReconciliationDiff,
    ServiceItem,
    SubsidyPolicy,
    WorkOrder,
)
from app.services import billing_service
from app.tasks import billing_monthly

BASE_TIME = datetime(2026, 10, 15, 9, 0, 0)
PERIOD = "2026-10"


@pytest.fixture()
def make_order(db, accounts):
    counter = {"n": 0}

    def _make(**kwargs) -> WorkOrder:
        counter["n"] += 1
        defaults = dict(
            code=f"BO{counter['n']:04d}",
            elder_id=accounts["elder"].id,
            service_item_id=accounts["item"].id,
            nurse_id=accounts["staff"].id,
            status=OrderStatus.COMPLETED,
            scheduled_start=BASE_TIME,
            scheduled_end=BASE_TIME + timedelta(hours=1),
            price_version_id=accounts["price"].id,
            unit_price=Decimal("120.00"),
            quantity=1,
            amount=Decimal("120.00"),
            community_org_id=accounts["elder"].community_org_id,
        )
        defaults.update(kwargs)
        order = WorkOrder(**defaults)
        db.add(order)
        db.flush()
        return order

    return _make


@pytest.fixture()
def make_free_item(db):
    """非补贴项目：用于构造不含补贴差异的账单，精确验证差异率阈值。"""

    def _make(code: str = "S9001") -> ServiceItem:
        item = ServiceItem(
            code=code,
            name="自费陪诊",
            category="other",
            pricing_mode="per_time",
            standard_duration_min=60,
            required_skills=[],
            applicable_levels=[],
            is_subsidized=False,
            subsidy_ratio=0.0,
        )
        db.add(item)
        db.flush()
        return item

    return _make


def _details(db, bill_id: int) -> list[BillDetail]:
    return list(
        db.query(BillDetail)
        .filter(BillDetail.bill_id == bill_id)
        .order_by(BillDetail.id.asc())
        .all()
    )


def _diffs(db, rule_code: str) -> list[ReconciliationDiff]:
    return list(
        db.query(ReconciliationDiff)
        .filter(ReconciliationDiff.rule_code == rule_code)
        .order_by(ReconciliationDiff.id.asc())
        .all()
    )


# ---------------------------------------------------------------- 政策与分账
def test_pick_policy_exact_then_degrade(db):
    policies = billing_service.ensure_default_policies(db)

    exact = subsidy.pick_policy(policies, AbilityLevel.MODERATE, "low_income")
    assert exact.ability_level == "moderate" and exact.difficulty_type == "low_income"

    severe_extreme = subsidy.pick_policy(policies, AbilityLevel.SEVERE, "extreme")
    assert severe_extreme.subsidy_ratio == pytest.approx(0.7)
    assert float(severe_extreme.monthly_limit) == 500.0

    # 轻度失能没有专属政策 → 退化到「任意能力 + 原困难类型」，取更低等级（中度）
    degraded = subsidy.pick_policy(policies, AbilityLevel.MILD, "extreme")
    assert degraded.ability_level == "moderate" and degraded.difficulty_type == "extreme"

    # 困难类型缺失 → 退化到普通
    normal = subsidy.pick_policy(policies, AbilityLevel.MODERATE, "normal")
    assert normal.difficulty_type == "normal"

    assert subsidy.pick_policy([], AbilityLevel.MODERATE, "normal") is None
    assert subsidy.pick_policy([p for p in policies if not p.enabled], AbilityLevel.MODERATE, "normal") is None


def test_split_amount_limit_and_ltc(db):
    policy = subsidy.pick_policy(billing_service.ensure_default_policies(db), AbilityLevel.MODERATE, "low_income")
    split = subsidy.split_amount(Decimal("120.00"), policy, Decimal("280.00"))
    assert split["subsidy"] == Decimal("20.00")  # min(60, 300-280)
    assert split["ltc"] == Decimal("24.00")
    assert split["self_pay"] == Decimal("76.00")
    assert sum(split.values()) == Decimal("120.00")


def test_split_amount_rounding_to_cent(db):
    policy = subsidy.pick_policy(billing_service.ensure_default_policies(db), AbilityLevel.MODERATE, "normal")
    split = subsidy.split_amount(Decimal("99.99"), policy, Decimal("0"))
    assert split["subsidy"] == Decimal("50.00")  # 49.995 四舍五入
    assert split["ltc"] == Decimal("20.00")      # 19.998 四舍五入
    assert split["self_pay"] == Decimal("29.99")
    assert sum(split.values()) == Decimal("99.99")


def test_split_amount_without_policy(db):
    split = subsidy.split_amount(Decimal("120.00"), None, Decimal("0"))
    assert split == {"subsidy": Decimal("0.00"), "ltc": Decimal("0.00"), "self_pay": Decimal("120.00")}


def test_ensure_default_policies_is_idempotent(db):
    first = billing_service.ensure_default_policies(db)
    db.flush()
    second = billing_service.ensure_default_policies(db)
    db.flush()
    assert len(first) == 6 and len(second) == 6
    assert db.query(SubsidyPolicy).count() == 6
    combos = {(p.ability_level, p.difficulty_type) for p in second}
    assert combos == {
        ("moderate", "normal"), ("moderate", "low_income"), ("moderate", "extreme"),
        ("severe", "normal"), ("severe", "low_income"), ("severe", "extreme"),
    }


def test_month_range_rejects_bad_period():
    assert billing_service.month_range("2026-01") == (
        datetime(2026, 1, 1),
        datetime(2026, 2, 1),
    )
    assert billing_service.month_range("2026-12")[1] == datetime(2027, 1, 1)
    for bad in ("2026-13", "2026-1", "202610", ""):
        with pytest.raises(BadRequest):
            billing_service.month_range(bad)


# ---------------------------------------------------------------- 月度结算
def test_subsidy_monthly_limit(db, accounts, make_order):
    """补贴上限跨明细累计：前 5 条各补 60，第 6 条被截断为 0。"""
    for index in range(6):
        start = BASE_TIME + timedelta(hours=index * 2)
        make_order(scheduled_start=start, scheduled_end=start + timedelta(hours=1))

    data = billing_service.generate_bills(db, period=PERIOD, user=accounts["users"]["super"])

    assert data["details_count"] == 6
    bill = db.get(Bill, data["bill"]["id"])
    details = _details(db, bill.id)
    assert [float(d.subsidy_amount) for d in details] == [60.0, 60.0, 60.0, 60.0, 60.0, 0.0]
    assert float(bill.total_amount) == 720.0
    assert float(bill.subsidy_amount) == 300.0
    assert float(bill.ltc_amount) == 144.0
    assert float(bill.self_pay_amount) == 276.0
    assert float(bill.total_amount) == pytest.approx(
        float(bill.subsidy_amount) + float(bill.ltc_amount) + float(bill.self_pay_amount)
    )

    limit_diffs = _diffs(db, billing_service.RULE_SUBSIDY_LIMIT)
    assert len(limit_diffs) == 1
    assert float(limit_diffs[0].amount) == 60.0
    assert limit_diffs[0].status == DiffStatus.OPEN.value
    assert bill.needs_review is True  # 60/720 ≈ 8.3% > 1%
    assert bill.diff_ratio == pytest.approx(60 / 720, rel=1e-4)

    # 幂等重算：草稿账单被替换，不会重复计费
    bill_id = bill.id
    db.expunge_all()
    again = billing_service.generate_bills(db, period=PERIOD, user=accounts["users"]["super"])
    assert again["details_count"] == 6
    assert len(_details(db, bill_id)) == 6
    assert db.query(ReconciliationDiff).count() == 1


def test_unfinished_order_not_billed(db, accounts, make_order):
    completed = make_order()
    unfinished = make_order(
        status=OrderStatus.PENDING_EVALUATION,
        scheduled_start=BASE_TIME + timedelta(hours=3),
        scheduled_end=BASE_TIME + timedelta(hours=4),
    )

    data = billing_service.generate_bills(db, period=PERIOD, user=accounts["users"]["super"])

    assert data["details_count"] == 1
    bill = db.get(Bill, data["bill"]["id"])
    details = _details(db, bill.id)
    assert [d.work_order_id for d in details] == [completed.id]

    unfinished_diffs = _diffs(db, billing_service.RULE_UNFINISHED)
    assert len(unfinished_diffs) == 1
    assert unfinished_diffs[0].work_order_id == unfinished.id
    assert float(unfinished_diffs[0].amount) == 120.0
    assert "未完成" in unfinished_diffs[0].description


def test_duration_three_times_boundary(db, accounts, make_elder, make_order):
    """恰好 3 倍不拦截，超过 3 倍（3.01 倍）拦截。"""
    elder_a = make_elder()
    elder_b = make_elder()
    exactly = make_order(
        elder_id=elder_a.id,
        duration_min=180,
        scheduled_start=BASE_TIME,
        scheduled_end=BASE_TIME + timedelta(hours=1),
    )
    over = make_order(
        elder_id=elder_b.id,
        duration_min=181,
        scheduled_start=BASE_TIME + timedelta(hours=2),
        scheduled_end=BASE_TIME + timedelta(hours=3),
    )

    booking = billing_service.generate_bills(db, period=PERIOD, user=accounts["users"]["super"])

    assert booking["details_count"] == 2
    duration_diffs = _diffs(db, billing_service.RULE_DURATION)
    assert len(duration_diffs) == 1
    assert duration_diffs[0].work_order_id == over.id
    assert duration_diffs[0].work_order_id != exactly.id
    assert float(duration_diffs[0].amount) == 120.0


def test_duplicate_overlap_diff(db, accounts, make_order):
    first = make_order(scheduled_start=BASE_TIME, scheduled_end=BASE_TIME + timedelta(hours=1))
    second = make_order(
        scheduled_start=BASE_TIME + timedelta(minutes=30),
        scheduled_end=BASE_TIME + timedelta(minutes=90),
    )
    make_order(
        scheduled_start=BASE_TIME + timedelta(hours=5),
        scheduled_end=BASE_TIME + timedelta(hours=6),
    )

    data = billing_service.generate_bills(db, period=PERIOD, user=accounts["users"]["super"])

    assert data["details_count"] == 3
    duplicate_diffs = _diffs(db, billing_service.RULE_DUPLICATE)
    assert len(duplicate_diffs) == 1
    assert duplicate_diffs[0].work_order_id == second.id
    assert duplicate_diffs[0].elder_id == first.elder_id
    assert float(duplicate_diffs[0].amount) == 120.0


def _ratio_scenario(db, accounts, make_order, make_free_item, diff_amount: str):
    item = make_free_item(f"S9{int(Decimal(diff_amount)):03d}")
    make_order(
        service_item_id=item.id,
        unit_price=Decimal("1000.00"),
        quantity=10,
        amount=Decimal("10000.00"),
        scheduled_start=BASE_TIME,
        scheduled_end=BASE_TIME + timedelta(hours=1),
    )
    make_order(
        status=OrderStatus.IN_SERVICE,
        service_item_id=item.id,
        unit_price=Decimal(diff_amount),
        quantity=1,
        amount=Decimal(diff_amount),
        scheduled_start=BASE_TIME + timedelta(hours=3),
        scheduled_end=BASE_TIME + timedelta(hours=4),
    )
    data = billing_service.generate_bills(db, period=PERIOD, user=accounts["users"]["super"])
    return db.get(Bill, data["bill"]["id"])


def test_diff_ratio_under_threshold_no_review(db, accounts, make_order, make_free_item):
    bill = _ratio_scenario(db, accounts, make_order, make_free_item, "90.00")
    assert float(bill.total_amount) == 10000.0
    assert float(bill.diff_amount) == 90.0
    assert bill.diff_ratio == pytest.approx(0.009, rel=1e-4)
    assert bill.needs_review is False

    super_user = accounts["users"]["super"]
    street_user = accounts["users"]["street"]
    billing_service.submit_bill(db, bill.id, user=super_user)
    confirmed = billing_service.confirm_bill(db, bill.id, user=street_user)
    assert confirmed["status"] == BillStatus.CONFIRMED.value


def test_diff_ratio_over_threshold_requires_review_note(db, accounts, make_order, make_free_item):
    bill = _ratio_scenario(db, accounts, make_order, make_free_item, "110.00")
    assert float(bill.diff_amount) == 110.0
    assert bill.diff_ratio == pytest.approx(0.011, rel=1e-4)
    assert bill.needs_review is True

    super_user = accounts["users"]["super"]
    street_user = accounts["users"]["street"]
    billing_service.submit_bill(db, bill.id, user=super_user)

    with pytest.raises(RuleViolation):
        billing_service.confirm_bill(db, bill.id, user=street_user)

    confirmed = billing_service.confirm_bill(
        db, bill.id, review_note="差异已核实：未完成工单不计费", user=street_user
    )
    assert confirmed["status"] == BillStatus.CONFIRMED.value
    assert confirmed["reviewed_by"] == street_user.id
    audit = db.query(AuditLog).filter(AuditLog.action == "update").order_by(AuditLog.id.desc()).first()
    assert "差异已核实" in (audit.detail or "")


def test_bill_state_machine_rejects_illegal_transition(db, accounts, make_order):
    make_order()
    data = billing_service.generate_bills(db, period=PERIOD, user=accounts["users"]["super"])
    bill_id = data["bill"]["id"]

    with pytest.raises(Conflict):
        billing_service.confirm_bill(db, bill_id, user=accounts["users"]["street"])
    with pytest.raises(Conflict):
        billing_service.pay_bill(
            db,
            bill_id,
            government=Decimal("60.00"),
            insurance=Decimal("24.00"),
            self_pay=Decimal("36.00"),
            user=accounts["users"]["super"],
        )


# ---------------------------------------------------------------- 缴费 / 红冲
def _confirmed_bill(db, accounts, make_order) -> Bill:
    make_order()
    super_user = accounts["users"]["super"]
    data = billing_service.generate_bills(db, period=PERIOD, user=super_user)
    bill_id = data["bill"]["id"]
    billing_service.submit_bill(db, bill_id, user=super_user)
    billing_service.confirm_bill(db, bill_id, user=accounts["users"]["street"])
    return db.get(Bill, bill_id)


def test_pay_amount_mismatch_rejected(db, accounts, make_order):
    bill = _confirmed_bill(db, accounts, make_order)
    with pytest.raises(RuleViolation):
        billing_service.pay_bill(
            db,
            bill.id,
            government=Decimal("61.00"),
            insurance=Decimal("24.00"),
            self_pay=Decimal("36.00"),
            user=accounts["users"]["super"],
        )
    with pytest.raises(BadRequest):
        billing_service.pay_bill(db, bill.id, government=Decimal("60.00"), user=accounts["users"]["super"])


def test_pay_three_way_then_reverse_keeps_original(db, accounts, make_order):
    bill = _confirmed_bill(db, accounts, make_order)
    super_user = accounts["users"]["super"]
    original_total = bill.total_amount

    paid = billing_service.pay_bill(
        db,
        bill.id,
        government=Decimal("60.00"),
        insurance=Decimal("24.00"),
        self_pay=Decimal("36.00"),
        remark="街道统一代缴",
        user=super_user,
    )
    assert paid["status"] == BillStatus.PAID.value
    assert len(paid["payments"]) == 3
    assert sum(p["amount"] for p in paid["payments"]) == pytest.approx(120.0)

    result = billing_service.reverse_bill(db, bill.id, reason="重复计费退费", user=super_user)
    assert result["status"] == BillStatus.REVERSED.value
    assert result["total_amount"] == pytest.approx(float(original_total))  # 原单金额不变

    red = db.get(Bill, result["reverse_bill_id"])
    assert red.total_amount == Decimal("-120.00")
    assert red.subsidy_amount == Decimal("-60.00")
    assert red.ltc_amount == Decimal("-24.00")
    assert red.self_pay_amount == Decimal("-36.00")
    assert red.code.endswith("-R")
    assert sum(float(d.amount) for d in _details(db, red.id)) == pytest.approx(-120.0)

    db.refresh(bill)
    assert bill.total_amount == original_total
    assert db.query(AuditLog).filter(AuditLog.action == "refund").count() == 1


def test_create_refund_by_work_order(db, accounts, make_order):
    order = make_order()
    data = billing_service.generate_bills(db, period=PERIOD, user=accounts["users"]["super"])
    original_id = data["bill"]["id"]

    result = billing_service.create_refund(
        db, order.id, Decimal("50.00"), "投诉退费：C0001 服务不达标", accounts["users"]["super"].id
    )

    assert result["ok"] is True
    assert result["bill_id"] and result["bill"]["total_amount"] == pytest.approx(-50.0)
    red = db.get(Bill, result["bill_id"])
    assert red.total_amount == Decimal("-50.00")
    components = red.subsidy_amount + red.ltc_amount + red.self_pay_amount
    assert components == red.total_amount
    assert red.period == PERIOD
    assert db.get(Bill, original_id).total_amount == Decimal("120.00")  # 原单不变


def test_create_refund_without_bill_detail_never_raises(db):
    result = billing_service.create_refund(db, 999999, Decimal("10.00"), "无明细", None)
    assert result["ok"] is False
    assert result["bill_id"] is None
    assert "未生成账单明细" in result["reason"]


def test_repeated_refunds_never_exceed_original_or_any_payer(db, accounts, make_order):
    order = make_order()
    data = billing_service.generate_bills(db, period=PERIOD, user=accounts["users"]["super"])
    original_id = data["bill"]["id"]
    for amount in ("0.01", "0.01", "19.99", "50.00", "999.00"):
        result = billing_service.create_refund(db, order.id, Decimal(amount), "分次退费")
        assert result["ok"], result
        red = db.get(Bill, result["bill_id"])
        assert red.subsidy_amount + red.ltc_amount + red.self_pay_amount == red.total_amount
    again = billing_service.create_refund(db, order.id, Decimal("1.00"), "重复点击")
    assert again["ok"] is False
    assert "已全额退费" in again["reason"]
    details = db.query(BillDetail).filter(BillDetail.work_order_id == order.id).all()
    for attr in ("amount", "subsidy_amount", "ltc_amount", "self_pay_amount"):
        assert sum(getattr(detail, attr) for detail in details) == Decimal("0.00")
    assert db.get(Bill, original_id).total_amount == Decimal("120.00")


def test_partial_refund_blocks_double_full_reversal(db, accounts, make_order):
    order = make_order()
    data = billing_service.generate_bills(db, period=PERIOD, user=accounts["users"]["super"])
    bill_id = data["bill"]["id"]
    bill = db.get(Bill, bill_id)
    bill.status = "confirmed"
    db.commit()
    result = billing_service.create_refund(db, order.id, Decimal("20.00"), "先退一部分")
    assert result["ok"] is True
    with pytest.raises(Conflict):
        billing_service.reverse_bill(db, bill_id, user=accounts["users"]["super"])
    with pytest.raises(RuleViolation):
        billing_service.reverse_bill(db, result["bill_id"], user=accounts["users"]["super"])
    assert billing_service.create_refund(db, order.id)["amount"] == -100.0


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-10")])
def test_nonpositive_refund_is_rejected(db, accounts, make_order, amount):
    order = make_order()
    billing_service.generate_bills(db, period=PERIOD, user=accounts["users"]["super"])
    assert billing_service.create_refund(db, order.id, amount)["ok"] is False
    assert db.query(Bill).count() == 1


# ---------------------------------------------------------------- 差异处理
def test_handle_diff_updates_status(db, accounts, make_order):
    make_order(status=OrderStatus.IN_SERVICE)
    billing_service.generate_bills(db, period=PERIOD, user=accounts["users"]["super"])
    diff = _diffs(db, billing_service.RULE_UNFINISHED)[0]

    with pytest.raises(BadRequest):
        billing_service.handle_diff(
            db, diff.id, status="unknown", explanation="x", user=accounts["users"]["street"]
        )
    with pytest.raises(RuleViolation):
        billing_service.handle_diff(
            db, diff.id, status="explained", explanation="  ", user=accounts["users"]["street"]
        )

    handled = billing_service.handle_diff(
        db,
        diff.id,
        status=DiffStatus.EXPLAINED.value,
        explanation="工单实际未完成，已通知护理员补录",
        user=accounts["users"]["street"],
    )
    assert handled["status"] == DiffStatus.EXPLAINED.value
    assert handled["explanation"].startswith("工单实际未完成")
    assert handled["handler_id"] == accounts["users"]["street"].id


def test_list_bills_and_diffs_pagination(db, accounts, make_order):
    for index in range(3):
        make_order(scheduled_start=BASE_TIME + timedelta(hours=index * 3))
    billing_service.generate_bills(db, period=PERIOD, user=accounts["users"]["super"])

    rows, total = billing_service.list_bills(db, accounts["users"]["super"], period=PERIOD, page=1, size=2)
    assert total == 1 and len(rows) == 1
    assert rows[0]["period"] == PERIOD

    detail = billing_service.get_bill_detail(db, rows[0]["id"], accounts["users"]["super"])
    assert detail["details_count"] == 3
    assert detail["diffs_count"] == 0

    diff_rows, diff_total = billing_service.list_diffs(db, accounts["users"]["street"], page=1, size=10)
    assert diff_total == 0 and diff_rows == []


# ---------------------------------------------------------------- 导出 / 定时任务
def test_export_workbook_is_valid_xlsx(db, accounts, make_order):
    order = make_order()
    data = billing_service.generate_bills(db, period=PERIOD, user=accounts["users"]["super"])
    content, filename = billing_service.export_bill_xlsx(
        db, data["bill"]["id"], user=accounts["users"]["super"]
    )
    assert content[:2] == b"PK"
    assert filename.endswith(".xlsx")
    assert len(content) > 2000

    import io

    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(content))
    sheet = workbook.active
    values = [
        str(cell.value)
        for row in sheet.iter_rows()
        for cell in row
        if cell.value is not None
    ]
    assert any(order.code in value for value in values)
    assert db.query(AuditLog).filter(AuditLog.action == "export").count() == 1


def test_last_month_period():
    assert billing_monthly.last_month_period(date(2026, 1, 5)) == "2025-12"
    assert billing_monthly.last_month_period(date(2026, 11, 8)) == "2026-10"
    assert billing_monthly.last_month_period(date(2026, 3, 1)) == "2026-02"


def test_run_monthly_billing_uses_own_session(db, engine, accounts, make_order, monkeypatch):
    make_order(scheduled_start=datetime(2026, 9, 10, 9, 0, 0),
               scheduled_end=datetime(2026, 9, 10, 10, 0, 0))
    db.commit()

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.SessionLocal", factory, raising=True)

    result = billing_monthly.run_monthly_billing("2026-09")
    assert result["ok"] is True
    assert result["period"] == "2026-09"
    assert result["bills"] == 1
    assert result["details"] == 1

    checker = factory()
    try:
        assert checker.query(Bill).count() == 1
        assert checker.query(Bill).one().period == "2026-09"
    finally:
        checker.close()


def test_run_monthly_billing_swallows_errors(db, engine, monkeypatch):
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.SessionLocal", factory, raising=True)

    result = billing_monthly.run_monthly_billing("2026/09")
    assert result["ok"] is False
    assert "2026/09" in result["period"]
    assert "账期" in result["error"]
