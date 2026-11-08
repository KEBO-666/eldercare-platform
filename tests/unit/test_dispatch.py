"""智能派单单元测试（B 模块）：四因子纯函数 + 超时顺延。"""
from __future__ import annotations

from datetime import datetime

import pytest
from freezegun import freeze_time

from app.algorithms.dispatch import (
    DEFAULT_WEIGHTS,
    distance_factor,
    haversine,
    load_factor,
    parse_weights,
    rating_factor,
    score_candidates,
    skill_factor,
    weighted_score,
)
from app.models import Appointment, WorkOrder
from app.services import appointment_service, dispatch_service

BASE = datetime(2026, 10, 15, 9, 0, 0)


def _candidate(**kwargs) -> dict:
    base = {
        "staff_id": 1,
        "name": "护理A",
        "distance_km": 0.5,
        "today_count": 0,
        "skills": ["shower"],
        "rating": 4.0,
        "is_focus": False,
    }
    base.update(kwargs)
    return base


# ---------------- 纯函数 ----------------
def test_haversine_distance():
    distance = haversine(31.2304, 121.4737, 31.2310, 121.4740)
    assert 0.05 < distance < 0.10
    assert haversine(31.2304, 121.4737, 31.2304, 121.4737) == pytest.approx(0.0, abs=1e-9)
    assert haversine(None, 121.4737, 31.2310, 121.4740) == float("inf")


def test_factor_bounds_and_values():
    assert distance_factor(0.0, 3.0) == 1.0
    assert distance_factor(1.5, 3.0) == pytest.approx(0.5)
    assert distance_factor(3.0, 3.0) == 0.0
    assert distance_factor(6.0, 3.0) == 0.0
    assert distance_factor(0.0, 0.0) == 1.0

    assert load_factor(0, 6) == 1.0
    assert load_factor(3, 6) == pytest.approx(0.5)
    assert load_factor(6, 6) == 0.0
    assert load_factor(9, 6) == 0.0
    assert load_factor(0, 0) == 0.0

    assert rating_factor(None, 3.5) == pytest.approx(0.7)
    assert rating_factor(5.0, 3.5) == 1.0
    assert rating_factor(6.0, 3.5) == 1.0

    assert skill_factor([], []) == 1.0
    assert skill_factor(["shower"], ["shower", "bedcare"]) == 1.0
    assert skill_factor(["shower", "bedcare"], ["shower"]) == 0.5
    assert skill_factor(["shower", "bedcare"], []) == 0.0


def test_weighted_score_and_parse_weights():
    factors = {"skill": 1.0, "distance": 0.0, "load": 0.0, "rating": 0.0}
    assert weighted_score(factors, (0.4, 0.3, 0.2, 0.1)) == pytest.approx(0.4)
    full = {"skill": 1.0, "distance": 1.0, "load": 1.0, "rating": 1.0}
    assert weighted_score(full) == pytest.approx(1.0)

    assert parse_weights("0.5,0.3,0.1,0.1") == pytest.approx((0.5, 0.3, 0.1, 0.1))
    assert parse_weights("[2, 1, 1, 0]") == pytest.approx((0.5, 0.25, 0.25, 0.0))
    assert parse_weights('{"skill":0.4,"distance":0.3,"load":0.2,"rating":0.1}') == pytest.approx(DEFAULT_WEIGHTS)
    assert parse_weights("bad-config") == pytest.approx(DEFAULT_WEIGHTS)
    assert parse_weights(None) == pytest.approx(DEFAULT_WEIGHTS)


def test_score_candidates_filters_by_skill_radius_load_and_status():
    candidates = [
        _candidate(staff_id=1),
        _candidate(staff_id=2, skills=["bedcare"]),
        _candidate(staff_id=3, distance_km=5.0),
        _candidate(staff_id=4, today_count=6),
        _candidate(staff_id=5, status="inactive"),
    ]
    result = score_candidates(candidates, required_skills=["shower"], radius_km=3.0, daily_limit=6)
    assert [row["staff_id"] for row in result] == [1]


def test_score_candidates_top3_sorted_with_factor_details():
    candidates = [
        _candidate(staff_id=index, distance_km=0.1 * index, today_count=index, rating=3.0 + 0.2 * index)
        for index in range(1, 5)
    ]
    result = score_candidates(candidates, top_n=3)
    assert len(result) == 3
    assert result[0]["score"] >= result[1]["score"] >= result[2]["score"]
    for row in result:
        assert set(row["factors"]) == {"skill", "distance", "load", "rating"}
        assert row["reason"]
        assert row["score"] <= 1.0


def test_focus_staff_is_penalized():
    normal = score_candidates([_candidate(staff_id=1)], top_n=1)[0]["score"]
    focus = score_candidates([_candidate(staff_id=1, is_focus=True)], top_n=1)[0]["score"]
    assert focus == pytest.approx(normal * 0.8, abs=1e-3)


def test_weights_change_candidate_order():
    near_low_rating = _candidate(staff_id=1, distance_km=0.1, today_count=5, rating=3.0)
    far_high_rating = _candidate(staff_id=2, distance_km=2.8, today_count=0, rating=5.0)

    default_order = [row["staff_id"] for row in score_candidates([near_low_rating, far_high_rating], DEFAULT_WEIGHTS)]
    assert default_order == [1, 2]

    rating_heavy = [row["staff_id"] for row in score_candidates(
        [near_low_rating, far_high_rating], (0.1, 0.1, 0.4, 0.4)
    )]
    assert rating_heavy == [2, 1]


# ---------------- 派单服务：接单超时顺延 ----------------
def _prepare_dispatched_order(db, accounts, desired_start: datetime):
    grid = accounts["users"]["grid"]
    created = appointment_service.create_appointment(
        db,
        service_item_id=accounts["item"].id,
        desired_start=desired_start,
        user=accounts["users"]["elder"],
    )
    appointment_service.accept_appointment(db, created["id"], user=grid)
    appointment_service.to_dispatch_appointment(db, created["id"], user=grid)
    order = dispatch_service.assign_order(db, created["id"], accounts["staff"].id, user=grid)
    return created["id"], order["id"]


def test_scan_accept_timeout_requeues(db, accounts):
    with freeze_time("2026-10-15 09:00:00"):
        appointment_id, order_id = _prepare_dispatched_order(db, accounts, datetime(2026, 10, 15, 9, 40))
        # 距服务开始 40 分钟 > 30 分钟阈值：不触发
        assert dispatch_service.scan_accept_timeout(db)["scanned"] == 0

    with freeze_time("2026-10-15 09:15:00"):
        result = dispatch_service.scan_accept_timeout(db)
    assert result["scanned"] == 1
    assert result["requeued"] == [appointment_id]
    assert result["requeued_orders"][0]["order_code"].startswith("WO")

    order = db.get(WorkOrder, order_id)
    appointment = db.get(Appointment, appointment_id)
    db.refresh(order)
    db.refresh(appointment)
    assert order.status == "cancelled"
    assert appointment.status == "pending_dispatch"


def test_scan_accept_timeout_reports_logged(db, accounts):
    with freeze_time("2026-10-15 09:00:00"):
        appointment_id, _ = _prepare_dispatched_order(db, accounts, datetime(2026, 10, 15, 9, 20))
        result = dispatch_service.scan_accept_timeout(db)
    assert result["scanned"] == 1
    detail = appointment_service.get_appointment_detail(db, appointment_id, accounts["users"]["grid"])
    assert detail["status"] == "pending_dispatch"
    assert detail["logs"][-1]["action"] == "requeue"
