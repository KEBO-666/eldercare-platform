"""C 模块单测：评价、回访、投诉与绩效。"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from freezegun import freeze_time

from app.core.enums import CallbackStatus, ComplaintStatus, OrderStatus
from app.core.exceptions import RuleViolation
from app.models import (
    CallbackTask,
    Complaint,
    Evaluation,
    Staff,
    StaffPerformanceMonthly,
    WorkOrder,
)
from app.services import quality_service

BASE_TIME = datetime(2026, 10, 15, 9, 0, 0)
FROZEN = "2026-10-15 09:00:00"


@pytest.fixture()
def make_order(db, accounts):
    counter = {"n": 0}

    def _make(**kwargs) -> WorkOrder:
        counter["n"] += 1
        defaults = dict(
            code=f"QO{counter['n']:04d}",
            elder_id=accounts["elder"].id,
            service_item_id=accounts["item"].id,
            nurse_id=accounts["staff"].id,
            status=OrderStatus.PENDING_EVALUATION,
            scheduled_start=BASE_TIME,
            checkin_at=BASE_TIME + timedelta(minutes=5),
            duration_min=60,
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


def _evaluate(db, accounts, order, scores, comment=None, user_key="elder"):
    punctuality, attitude, professionalism, effect = scores
    return quality_service.create_evaluation(
        db,
        accounts["users"][user_key],
        work_order_id=order.id,
        punctuality=punctuality,
        attitude=attitude,
        professionalism=professionalism,
        effect=effect,
        comment=comment,
    )


# ---------------------------------------------------------------- 评价
def test_evaluation_completes_order_and_caches_rating(db, accounts, make_order):
    order = make_order()
    data = _evaluate(db, accounts, order, (5, 4, 5, 5))
    assert data["evaluation"]["avg_score"] == 4.75
    assert data["order"]["status"] == OrderStatus.COMPLETED
    assert data["order"]["rating"] == 4.75
    db.refresh(order)
    assert order.completed_at is not None
    staff = db.get(Staff, accounts["staff"].id)
    assert staff.rating_cached == 4.75
    assert staff.order_count_cached == 1


def test_evaluation_invalid_star_range(db, accounts, make_order):
    order = make_order()
    for scores in [(6, 5, 5, 5), (0, 5, 5, 5), (5, 5, 5, -1)]:
        with pytest.raises(RuleViolation):
            _evaluate(db, accounts, order, scores)
    assert db.query(Evaluation).count() == 0


def test_evaluation_requires_pending_evaluation_status(db, accounts, make_order):
    order = make_order(status=OrderStatus.IN_SERVICE)
    with pytest.raises(RuleViolation):
        _evaluate(db, accounts, order, (5, 5, 5, 5))


def test_low_score_creates_callback_due_in_48_hours(db, accounts, make_order):
    order = make_order()
    with freeze_time(FROZEN):
        data = _evaluate(db, accounts, order, (3, 3, 3, 3), comment="一般")
    assert data["need_callback"] is True
    assert data["evaluation"]["avg_score"] == 3.0
    callback = db.get(CallbackTask, data["callback_task_id"])
    assert callback is not None
    assert callback.due_at == datetime(2026, 10, 17, 9, 0, 0)
    assert (callback.due_at - datetime(2026, 10, 15, 9, 0, 0)) == timedelta(hours=48)
    assert callback.assigned_to == accounts["users"]["org_admin"].id
    assert callback.status == CallbackStatus.PENDING
    assert callback.elder_id == accounts["elder"].id


def test_high_score_no_callback(db, accounts, make_order):
    order = make_order()
    data = _evaluate(db, accounts, order, (5, 5, 4, 5))
    assert data["need_callback"] is False
    assert data["callback_task_id"] is None
    assert db.query(CallbackTask).count() == 0


def test_rating_cache_uses_recent_30_days(db, accounts, make_order):
    # 历史评价（40 天前，5 分）不计入近 30 天缓存
    old_order = make_order()
    with freeze_time("2026-09-01 09:00:00"):
        _evaluate(db, accounts, old_order, (5, 5, 5, 5))
    new_order = make_order()
    with freeze_time(FROZEN):
        data = _evaluate(db, accounts, new_order, (3, 3, 3, 3))
    assert data["rating_30d"] == 3.0
    assert data["nurse"]["rating_cached"] == 3.0


# ---------------------------------------------------------------- 回访
def test_callback_list_flags_overdue(db, accounts, make_order):
    order = make_order()
    with freeze_time(FROZEN):
        _evaluate(db, accounts, order, (2, 2, 2, 2))
    with freeze_time("2026-10-17 12:00:00"):  # 超期 3 小时
        items, total, overdue_count = quality_service.list_callbacks(db, accounts["users"]["grid"])
    assert total == 1
    assert overdue_count == 1
    assert items[0]["overdue"] is True
    assert items[0]["overdue_minutes"] >= 180
    with freeze_time(FROZEN):
        items, _, _ = quality_service.list_callbacks(db, accounts["users"]["grid"])
    assert items[0]["overdue"] is False
    assert items[0]["remain_minutes"] > 0


def test_finish_callback_to_complaint(db, accounts, make_order):
    order = make_order()
    with freeze_time(FROZEN):
        _evaluate(db, accounts, order, (2, 3, 2, 2))
    callback = db.query(CallbackTask).one()
    data = quality_service.finish_callback(
        db, accounts["users"]["grid"], callback.id, result="to_complaint", note="老人仍不满意"
    )
    assert data["result"] == "to_complaint"
    assert data["status"] == CallbackStatus.DONE
    assert data["complaint"] is not None
    assert data["complaint"]["status"] == ComplaintStatus.NEW
    assert data["complaint"]["nurse_id"] == accounts["staff"].id
    assert data["complaint"]["work_order_id"] == order.id
    db.refresh(callback)
    assert callback.complaint_id == data["complaint"]["id"]
    # 重复完成被拒绝
    with pytest.raises(RuleViolation):
        quality_service.finish_callback(db, accounts["users"]["grid"], callback.id, result="improve")


def test_finish_callback_invalid_result(db, accounts, make_order):
    order = make_order()
    _evaluate(db, accounts, order, (2, 2, 2, 2))
    callback = db.query(CallbackTask).one()
    with pytest.raises(RuleViolation):
        quality_service.finish_callback(db, accounts["users"]["grid"], callback.id, result="unknown")


def test_finish_callback_improve_does_not_create_complaint(db, accounts, make_order):
    order = make_order()
    _evaluate(db, accounts, order, (3, 3, 3, 3))
    callback = db.query(CallbackTask).one()
    data = quality_service.finish_callback(
        db, accounts["users"]["grid"], callback.id, result="improve", note="已致歉并跟踪"
    )
    assert data["complaint"] is None
    assert db.query(Complaint).count() == 0


# ---------------------------------------------------------------- 投诉
def test_complaint_assign_and_conclude_flow(db, accounts, make_order):
    order = make_order(status=OrderStatus.COMPLETED)
    complaint = quality_service.create_complaint(
        db, accounts["users"]["grid"], elder_id=accounts["elder"].id, work_order_id=order.id,
        content="服务态度生硬",
    )
    assert complaint["status"] == ComplaintStatus.NEW
    assigned = quality_service.assign_complaint(
        db, accounts["users"]["grid"], complaint["id"], handler_id=accounts["users"]["org_admin"].id
    )
    assert assigned["status"] == ComplaintStatus.ASSIGNED
    assert assigned["handler_id"] == accounts["users"]["org_admin"].id
    concluded = quality_service.conclude_complaint(
        db,
        accounts["users"]["org_admin"],
        complaint["id"],
        conclusion="经核实服务态度确有问题",
        is_established=True,
        resolution="change_nurse",
    )
    assert concluded["complaint"]["status"] == ComplaintStatus.CONCLUDED
    assert concluded["complaint"]["is_established"] is True
    assert concluded["complaint"]["resolution"] == "change_nurse"
    assert "换人处理" in concluded["complaint"]["institution_statement"]
    # 重复结案被拒绝
    with pytest.raises(RuleViolation):
        quality_service.conclude_complaint(
            db, accounts["users"]["org_admin"], complaint["id"], conclusion="再次", is_established=True
        )


def test_complaint_conclude_refund_falls_back_without_billing_module(
    db, accounts, make_order, monkeypatch
):
    import sys

    monkeypatch.setitem(sys.modules, "app.services.billing_service", None)  # 模拟结算模块未就绪
    order = make_order(status=OrderStatus.COMPLETED)
    complaint = quality_service.create_complaint(
        db, accounts["users"]["grid"], elder_id=accounts["elder"].id, work_order_id=order.id, content="服务不到位"
    )
    result = quality_service.conclude_complaint(
        db,
        accounts["users"]["grid"],
        complaint["id"],
        conclusion="同意退还本次服务费",
        is_established=True,
        resolution="refund",
    )
    assert result["complaint"]["resolution"] == "refund"
    assert result["refund"] is not None
    assert result["refund"]["fallback"] is True
    assert result["refund"]["status"] == "pending_manual"
    assert result["refund"]["amount"] == 120.0


def test_complaint_conclude_refund_uses_billing_module(db, accounts, make_order):
    order = make_order(status=OrderStatus.COMPLETED)
    complaint = quality_service.create_complaint(
        db, accounts["users"]["grid"], elder_id=accounts["elder"].id, work_order_id=order.id, content="服务不到位"
    )
    result = quality_service.conclude_complaint(
        db,
        accounts["users"]["grid"],
        complaint["id"],
        conclusion="同意退还本次服务费",
        is_established=True,
        resolution="refund",
    )
    assert result["complaint"]["resolution"] == "refund"
    assert result["refund"] is not None
    # 结算模块已就绪：真实调用 create_refund（工单未出账时 ok=False，同样不算模块缺失兜底）
    assert result["refund"]["fallback"] is False
    assert result["refund"]["status"] in {"created", "skipped"}


def test_three_established_complaints_marks_nurse_focus(db, accounts, make_order):
    staff = db.get(Staff, accounts["staff"].id)
    order = make_order(status=OrderStatus.COMPLETED)
    assert staff.is_focus is False
    focus_flags = []
    for index in range(3):
        complaint = quality_service.create_complaint(
            db,
            accounts["users"]["grid"],
            elder_id=accounts["elder"].id,
            work_order_id=order.id,
            content=f"投诉 {index + 1}",
        )
        result = quality_service.conclude_complaint(
            db,
            accounts["users"]["grid"],
            complaint["id"],
            conclusion="经核实投诉成立",
            is_established=True,
            resolution="deduct",
        )
        focus_flags.append(result["focus_marked"])
    assert focus_flags == [False, False, True]
    db.refresh(staff)
    assert staff.is_focus is True
    assert result["established_count"] == 3
    assert result["deduct_points"] == quality_service.DEDUCT_POINTS
    assert result["nurse_is_focus"] is True


def test_complaint_requires_content(db, accounts):
    with pytest.raises(RuleViolation):
        quality_service.create_complaint(
            db, accounts["users"]["grid"], elder_id=accounts["elder"].id, content="   "
        )


# ---------------------------------------------------------------- 绩效
def test_performance_board_ranks_by_avg_score(db, accounts, make_order, make_staff):
    nurse_b = make_staff(name="李护理")
    order_a = make_order(
        status=OrderStatus.COMPLETED,
        scheduled_start=BASE_TIME,
        checkin_at=BASE_TIME + timedelta(minutes=5),
        completed_at=BASE_TIME + timedelta(minutes=70),
    )
    order_b = make_order(
        nurse_id=nurse_b.id,
        status=OrderStatus.COMPLETED,
        scheduled_start=BASE_TIME,
        checkin_at=BASE_TIME + timedelta(minutes=30),
        completed_at=BASE_TIME + timedelta(minutes=95),
    )
    order_anomaly = make_order(status=OrderStatus.ANOMALY, scheduled_start=BASE_TIME)
    db.add_all(
        [
            Evaluation(work_order_id=order_a.id, elder_id=accounts["elder"].id,
                       nurse_id=accounts["staff"].id, avg_score=5.0),
            Evaluation(work_order_id=order_b.id, elder_id=accounts["elder"].id,
                       nurse_id=nurse_b.id, avg_score=2.0),
        ]
    )
    db.commit()

    result = quality_service.performance_board(db, accounts["users"]["org_admin"], "2026-10")
    items = result["items"]
    assert result["period"] == "2026-10"
    assert len(items) == 2
    assert items[0]["nurse_id"] == accounts["staff"].id
    assert items[0]["rank_no"] == 1
    assert items[0]["avg_score"] == 5.0
    assert items[0]["punctuality_rate"] == 1.0
    assert items[0]["order_count"] == 1  # 完成单量（completed）
    assert items[0]["scheduled_count"] == 2
    assert items[0]["anomaly_rate"] == 0.5  # 1 异常 / 2 总单
    assert items[1]["nurse_id"] == nurse_b.id
    assert items[1]["rank_no"] == 2
    assert items[1]["avg_score"] == 2.0
    assert items[1]["punctuality_rate"] == 0.0

    cached = db.query(StaffPerformanceMonthly).filter(StaffPerformanceMonthly.period == "2026-10").all()
    assert len(cached) == 2
    assert {row.rank_no for row in cached} == {1, 2}
    assert order_anomaly.nurse_id == accounts["staff"].id


def test_performance_board_invalid_period(db, accounts):
    with pytest.raises(RuleViolation):
        quality_service.performance_board(db, accounts["users"]["org_admin"], "2026/10")
    with pytest.raises(RuleViolation):
        quality_service.performance_board(db, accounts["users"]["org_admin"], "2026-13")
