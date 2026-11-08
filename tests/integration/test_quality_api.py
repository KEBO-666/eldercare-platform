"""C 模块集成测试：评价 / 回访 / 投诉 / 绩效接口。"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from freezegun import freeze_time

from app.core.enums import CallbackStatus, ComplaintStatus, OrderStatus
from app.core.exceptions import RuleViolation
from app.models import CallbackTask, Evaluation, Staff, WorkOrder
from app.services import quality_service

BASE_TIME = datetime(2026, 10, 15, 9, 0, 0)
FROZEN = "2026-10-15 09:00:00"
API = "/api/v1"


@pytest.fixture()
def make_order(db, accounts):
    counter = {"n": 0}

    def _make(**kwargs) -> WorkOrder:
        counter["n"] += 1
        defaults = dict(
            code=f"QA{counter['n']:04d}",
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


def _evaluation_payload(order_id: int, scores: tuple[int, int, int, int], comment: str | None = None) -> dict:
    punctuality, attitude, professionalism, effect = scores
    payload = {
        "work_order_id": order_id,
        "punctuality": punctuality,
        "attitude": attitude,
        "professionalism": professionalism,
        "effect": effect,
    }
    if comment is not None:
        payload["comment"] = comment
    return payload


# ---------------------------------------------------------------- 评价
def test_elder_submit_evaluation_low_score_creates_callback(client, db, headers, accounts, make_order):
    order = make_order()
    with freeze_time(FROZEN):
        resp = client.post(
            f"{API}/quality/evaluations",
            json=_evaluation_payload(order.id, (3, 3, 3, 3), "服务一般"),
            headers=headers("elder"),
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["evaluation"]["avg_score"] == 3.0
    assert data["need_callback"] is True
    assert data["callback_task_id"] is not None
    assert data["order"]["status"] == OrderStatus.COMPLETED
    callback = db.get(CallbackTask, data["callback_task_id"])
    assert callback.due_at == datetime(2026, 10, 17, 9, 0, 0)
    assert callback.assigned_to == accounts["users"]["org_admin"].id
    assert callback.status == CallbackStatus.PENDING

    db.refresh(order)
    assert order.status == OrderStatus.COMPLETED
    assert order.rating == 3.0
    staff = db.get(Staff, accounts["staff"].id)
    assert staff.rating_cached == 3.0


def test_elder_submit_high_score_no_callback(client, db, headers, make_order):
    order = make_order()
    resp = client.post(
        f"{API}/quality/evaluations",
        json=_evaluation_payload(order.id, (5, 5, 4, 5), "很满意"),
        headers=headers("elder"),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["need_callback"] is False
    assert data["callback_task_id"] is None
    assert db.query(CallbackTask).count() == 0


def test_evaluation_requires_permission(client, db, headers, make_order):
    order = make_order()
    resp = client.post(
        f"{API}/quality/evaluations",
        json=_evaluation_payload(order.id, (5, 5, 5, 5)),
        headers=headers("nurse"),
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "FORBIDDEN"


def test_evaluation_rejects_wrong_status_and_out_of_range(client, db, headers, make_order):
    accepted_order = make_order(status=OrderStatus.ACCEPTED)
    resp = client.post(
        f"{API}/quality/evaluations",
        json=_evaluation_payload(accepted_order.id, (5, 5, 5, 5)),
        headers=headers("elder"),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "RULE_VIOLATION"

    order = make_order()
    resp = client.post(
        f"{API}/quality/evaluations",
        json=_evaluation_payload(order.id, (6, 5, 5, 5)),
        headers=headers("elder"),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "RULE_VIOLATION"
    assert db.query(Evaluation).count() == 0


# ---------------------------------------------------------------- 回访
def test_callbacks_list_and_finish_to_complaint(client, db, headers, accounts, make_order):
    order = make_order()
    with freeze_time(FROZEN):
        resp = client.post(
            f"{API}/quality/evaluations",
            json=_evaluation_payload(order.id, (2, 3, 2, 2)),
            headers=headers("elder"),
        )
    assert resp.status_code == 200, resp.text
    callback_id = resp.json()["data"]["callback_task_id"]

    with freeze_time(FROZEN):
        listing = client.get(f"{API}/quality/callbacks", headers=headers("grid"))
    assert listing.status_code == 200, listing.text
    data = listing.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["overdue"] is False
    assert data["items"][0]["remain_minutes"] > 0
    assert data["overdue_count"] == 0

    with freeze_time("2026-10-18 09:00:00"):  # 超过 48 小时 → 超时
        overdue_listing = client.get(f"{API}/quality/callbacks", headers=headers("grid"))
    overdue_data = overdue_listing.json()["data"]
    assert overdue_data["items"][0]["overdue"] is True
    assert overdue_data["items"][0]["overdue_minutes"] >= 1440
    assert overdue_data["overdue_count"] == 1

    finished = client.post(
        f"{API}/quality/callbacks/{callback_id}/finish",
        json={"result": "to_complaint", "note": "老人仍不满意，转投诉"},
        headers=headers("grid"),
    )
    assert finished.status_code == 200, finished.text
    finished_data = finished.json()["data"]
    assert finished_data["status"] == CallbackStatus.DONE
    assert finished_data["complaint"] is not None
    assert finished_data["complaint"]["status"] == ComplaintStatus.NEW

    complaints = client.get(f"{API}/quality/complaints", headers=headers("grid")).json()["data"]
    assert complaints["total"] == 1
    assert complaints["items"][0]["nurse_id"] == accounts["staff"].id

    repeated = client.post(
        f"{API}/quality/callbacks/{callback_id}/finish",
        json={"result": "improve"},
        headers=headers("grid"),
    )
    assert repeated.status_code == 400
    assert repeated.json()["code"] == "RULE_VIOLATION"


# ---------------------------------------------------------------- 投诉
def test_complaint_assign_and_conclude_refund(client, db, headers, accounts, make_order):
    order = make_order(status=OrderStatus.COMPLETED)
    created = client.post(
        f"{API}/quality/complaints",
        json={"elder_id": accounts["elder"].id, "work_order_id": order.id, "content": "服务时间不足，要求退费"},
        headers=headers("grid"),
    )
    assert created.status_code == 200, created.text
    complaint = created.json()["data"]
    assert complaint["status"] == ComplaintStatus.NEW

    assigned = client.post(
        f"{API}/quality/complaints/{complaint['id']}/assign",
        json={"handler_id": accounts["users"]["org_admin"].id},
        headers=headers("grid"),
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["data"]["status"] == ComplaintStatus.ASSIGNED

    concluded = client.post(
        f"{API}/quality/complaints/{complaint['id']}/conclude",
        json={
            "conclusion": "核实服务时长不足，投诉成立",
            "is_established": True,
            "resolution": "refund",
        },
        headers=headers("org_admin"),
    )
    assert concluded.status_code == 200, concluded.text
    data = concluded.json()["data"]
    assert data["complaint"]["status"] == ComplaintStatus.CONCLUDED
    assert data["complaint"]["is_established"] is True
    assert data["complaint"]["resolution"] == "refund"
    assert data["is_established"] is True
    assert data["refund"] is not None
    # 结算模块已就绪 → 真实调用 create_refund；工单未出账时返回 skipped，但不属于模块缺失兜底
    assert data["refund"]["fallback"] is False
    assert data["refund"]["status"] in {"created", "skipped"}
    assert data["refund"]["amount"] == 120.0

    # 重复结案被拒绝
    repeated = client.post(
        f"{API}/quality/complaints/{complaint['id']}/conclude",
        json={"conclusion": "再次结案", "is_established": True, "resolution": "none"},
        headers=headers("org_admin"),
    )
    assert repeated.status_code == 400


def test_three_established_complaints_marks_focus(client, db, headers, accounts, make_order):
    order = make_order(status=OrderStatus.COMPLETED)
    focus_flags = []
    for index in range(3):
        created = client.post(
            f"{API}/quality/complaints",
            json={
                "elder_id": accounts["elder"].id,
                "work_order_id": order.id,
                "nurse_id": accounts["staff"].id,
                "content": f"投诉内容 {index + 1}",
            },
            headers=headers("grid"),
        )
        assert created.status_code == 200, created.text
        complaint_id = created.json()["data"]["id"]
        concluded = client.post(
            f"{API}/quality/complaints/{complaint_id}/conclude",
            json={"conclusion": "投诉成立，扣减服务分", "is_established": True, "resolution": "deduct"},
            headers=headers("grid"),
        )
        assert concluded.status_code == 200, concluded.text
        focus_flags.append(concluded.json()["data"]["focus_marked"])
    assert focus_flags == [False, False, True]
    staff = db.get(Staff, accounts["staff"].id)
    assert staff.is_focus is True

    listing = client.get(
        f"{API}/quality/complaints",
        params={"nurse_id": accounts["staff"].id, "status": "concluded"},
        headers=headers("grid"),
    )
    assert listing.status_code == 200, listing.text
    assert listing.json()["data"]["total"] == 3


def test_complaint_requires_content(client, db, headers, accounts):
    resp = client.post(
        f"{API}/quality/complaints",
        json={"elder_id": accounts["elder"].id, "content": "   "},
        headers=headers("grid"),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "RULE_VIOLATION"


# ---------------------------------------------------------------- 绩效
def test_performance_api_ranking(client, db, headers, accounts, make_order, make_staff):
    nurse_b = make_staff(name="李护理")
    order_a = make_order(scheduled_start=BASE_TIME, checkin_at=BASE_TIME + timedelta(minutes=5))
    order_b = make_order(
        nurse_id=nurse_b.id,
        scheduled_start=BASE_TIME,
        checkin_at=BASE_TIME + timedelta(minutes=40),
    )
    resp_a = client.post(
        f"{API}/quality/evaluations",
        json=_evaluation_payload(order_a.id, (5, 5, 5, 5)),
        headers=headers("elder"),
    )
    assert resp_a.status_code == 200, resp_a.text
    resp_b = client.post(
        f"{API}/quality/evaluations",
        json=_evaluation_payload(order_b.id, (2, 2, 2, 2)),
        headers=headers("elder"),
    )
    assert resp_b.status_code == 200, resp_b.text

    resp = client.get(f"{API}/quality/performance", params={"period": "2026-10"}, headers=headers("org_admin"))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["period"] == "2026-10"
    items = data["items"]
    assert len(items) == 2
    assert items[0]["nurse_id"] == accounts["staff"].id
    assert items[0]["rank_no"] == 1
    assert items[0]["avg_score"] == 5.0
    assert items[0]["punctuality_rate"] == 1.0
    assert items[0]["order_count"] == 1
    assert items[1]["nurse_id"] == nurse_b.id
    assert items[1]["rank_no"] == 2
    assert items[1]["avg_score"] == 2.0
    assert items[1]["punctuality_rate"] == 0.0

    bad = client.get(f"{API}/quality/performance", params={"period": "2026-10-01"}, headers=headers("org_admin"))
    assert bad.status_code == 400, bad.text
    assert bad.json()["code"] == "RULE_VIOLATION"


def test_evaluation_list_filters(client, db, headers, accounts, make_order):
    order = make_order()
    client.post(
        f"{API}/quality/evaluations",
        json=_evaluation_payload(order.id, (5, 4, 5, 5)),
        headers=headers("elder"),
    )
    resp = client.get(
        f"{API}/quality/evaluations",
        params={"work_order_id": order.id},
        headers=headers("org_admin"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["total"] == 1
    assert body["items"][0]["work_order_id"] == order.id
    assert body["items"][0]["avg_score"] == 4.75

    filtered = client.get(
        f"{API}/quality/evaluations", params={"work_order_id": 999999}, headers=headers("org_admin")
    )
    assert filtered.json()["data"]["total"] == 0


# ---------------------------------------------------------------- 统计口径单测（走服务层）
def test_period_range_helper():
    assert quality_service.period_range("2026-02")[0] == "2026-02"
    start, end = quality_service.period_range("2026-12")[1:]
    assert start == datetime(2026, 12, 1)
    assert end == datetime(2027, 1, 1)
    with pytest.raises(RuleViolation):
        quality_service.period_range("2026-1")
    with pytest.raises(RuleViolation):
        quality_service.period_range("2026-13")
