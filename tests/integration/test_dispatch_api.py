"""智能派单接口集成测试（B 模块）：候选 TopN、派单快照、改派、超时顺延、权重覆盖。"""
from __future__ import annotations

from datetime import datetime, time

import pytest
from freezegun import freeze_time

from app.core.enums import AbilityLevel
from app.models import AuditLog, ConfigItem, Evaluation, WorkOrder


def _create_appointment(client, headers, accounts, desired_start: str, elder_id: int | None = None) -> int:
    payload = {"service_item_id": accounts["item"].id, "desired_start": desired_start}
    if elder_id is not None:
        payload["elder_id"] = elder_id
    resp = client.post("/api/v1/appointments", json=payload, headers=headers("super" if elder_id is not None else "elder"))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


def _to_pending_dispatch(client, headers, appointment_id: int) -> None:
    accepted = client.post(f"/api/v1/appointments/{appointment_id}/accept", headers=headers("grid"))
    assert accepted.status_code == 200, accepted.text
    pending = client.post(f"/api/v1/appointments/{appointment_id}/to-dispatch", headers=headers("grid"))
    assert pending.status_code == 200, pending.text


def test_candidates_top3_with_factor_details(client, headers, accounts, make_staff):
    make_staff(name="近距护理", base_lat=31.2310, base_lng=121.4740, rating_cached=4.9)
    make_staff(name="中距护理", base_lat=31.2400, base_lng=121.4740, rating_cached=4.2)
    make_staff(name="远距护理", base_lat=31.2500, base_lng=121.4740, rating_cached=3.9)
    appointment_id = _create_appointment(client, headers, accounts, "2026-10-16T10:00:00")
    _to_pending_dispatch(client, headers, appointment_id)

    resp = client.get(
        f"/api/v1/dispatch/{appointment_id}/candidates",
        params={"limit": 3},
        headers=headers("grid"),
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    assert isinstance(rows, list)
    assert 1 <= len(rows) <= 3
    scores = [row["score"] for row in rows]
    assert scores == sorted(scores, reverse=True)
    for row in rows:
        assert {"staff_id", "name", "distance_km", "score", "factors", "reason"} <= set(row)
        assert set(row["factors"]) == {"skill", "distance", "load", "rating"}
        assert row["distance_km"] <= 3.0
        assert row["reason"]


def test_candidates_exclude_staff_missing_required_skill(client, headers, accounts, make_staff):
    skilled = make_staff(name="会助浴", skills=["shower"], base_lat=31.2311, base_lng=121.4741)
    unskilled = make_staff(name="只会保洁", skills=["cleaning"], base_lat=31.2311, base_lng=121.4741)
    appointment_id = _create_appointment(client, headers, accounts, "2026-10-16T10:00:00")
    _to_pending_dispatch(client, headers, appointment_id)

    rows = client.get(f"/api/v1/dispatch/{appointment_id}/candidates", headers=headers("grid")).json()["data"]
    staff_ids = [row["staff_id"] for row in rows]
    assert skilled.id in staff_ids
    assert unskilled.id not in staff_ids


def test_candidates_exclude_staff_over_daily_limit(client, headers, accounts, make_staff, db):
    busy_staff = make_staff(name="满负荷护理", base_lat=31.2311, base_lng=121.4741)
    today = datetime(2026, 10, 16).date()
    for index in range(6):
        db.add(
            WorkOrder(
                code=f"WO{today:%Y%m%d}{index:04d}",
                elder_id=accounts["elder"].id,
                service_item_id=accounts["item"].id,
                nurse_id=busy_staff.id,
                scheduled_start=datetime.combine(today, time(hour=index)),
                status="pending_accept",
                community_org_id=accounts["elder"].community_org_id,
            )
        )
    db.commit()

    appointment_id = _create_appointment(client, headers, accounts, "2026-10-16T10:00:00")
    _to_pending_dispatch(client, headers, appointment_id)
    rows = client.get(f"/api/v1/dispatch/{appointment_id}/candidates", headers=headers("grid")).json()["data"]
    assert busy_staff.id not in [row["staff_id"] for row in rows]


def test_candidates_exclude_staff_with_time_conflict(client, headers, accounts, make_staff, make_elder, db):
    conflicted = make_staff(name="时段占用护理", base_lat=31.2311, base_lng=121.4741)
    other_elder = make_elder(ability_level=AbilityLevel.MODERATE.value)
    db.add(
        WorkOrder(
            code="WO202610169001",
            elder_id=other_elder.id,
            service_item_id=accounts["item"].id,
            nurse_id=conflicted.id,
            scheduled_start=datetime(2026, 10, 16, 10, 30),
            scheduled_end=datetime(2026, 10, 16, 11, 30),
            status="accepted",
            community_org_id=other_elder.community_org_id,
        )
    )
    db.commit()

    appointment_id = _create_appointment(client, headers, accounts, "2026-10-16T10:00:00")
    _to_pending_dispatch(client, headers, appointment_id)
    rows = client.get(f"/api/v1/dispatch/{appointment_id}/candidates", headers=headers("grid")).json()["data"]
    assert conflicted.id not in [row["staff_id"] for row in rows]


def test_config_weights_override_changes_ranking(client, headers, accounts, make_staff, db):
    near_staff = make_staff(name="近距离", base_lat=31.2310, base_lng=121.4740, rating_cached=3.0)
    far_staff = make_staff(name="远距离高评分", base_lat=31.2561, base_lng=121.4740, rating_cached=5.0)
    for staff, rating in ((near_staff, 3.0), (far_staff, 5.0)):
        rated_order = WorkOrder(
            code=f"RATED-{staff.id}", elder_id=accounts["elder"].id,
            service_item_id=accounts["item"].id, nurse_id=staff.id,
            scheduled_start=datetime(2026, 9, 1), status="completed",
            community_org_id=accounts["elder"].community_org_id,
        )
        db.add(rated_order)
        db.flush()
        db.add(Evaluation(work_order_id=rated_order.id, elder_id=accounts["elder"].id,
                          nurse_id=staff.id, avg_score=rating))
    db.commit()
    appointment_id = _create_appointment(client, headers, accounts, "2026-10-16T10:00:00")
    _to_pending_dispatch(client, headers, appointment_id)

    default_rows = client.get(
        f"/api/v1/dispatch/{appointment_id}/candidates", params={"limit": 5}, headers=headers("grid")
    ).json()["data"]
    default_ids = [row["staff_id"] for row in default_rows]
    assert default_ids.index(near_staff.id) < default_ids.index(far_staff.id)

    db.add(ConfigItem(key="dispatch.weights", value="0.1,0.1,0.4,0.4", description="评分优先"))
    db.commit()

    weighted_rows = client.get(
        f"/api/v1/dispatch/{appointment_id}/candidates", params={"limit": 5}, headers=headers("grid")
    ).json()["data"]
    weighted_ids = [row["staff_id"] for row in weighted_rows]
    assert weighted_ids.index(far_staff.id) < weighted_ids.index(near_staff.id)
    assert weighted_rows[0]["weights"] == [0.1, 0.1, 0.4, 0.4]


def test_assign_creates_work_order_with_price_snapshot(client, headers, accounts, db):
    appointment_id = _create_appointment(client, headers, accounts, "2026-10-16T10:00:00")
    _to_pending_dispatch(client, headers, appointment_id)

    resp = client.post(
        f"/api/v1/dispatch/{appointment_id}/assign",
        json={"nurse_id": accounts["staff"].id},
        headers=headers("grid"),
    )
    assert resp.status_code == 200, resp.text
    order = resp.json()["data"]
    assert order["code"].startswith("WO")
    assert order["status"] == "pending_accept"
    assert order["unit_price"] == 120.0
    assert order["amount"] == 120.0
    assert order["quantity"] == 1.0
    assert order["nurse_id"] == accounts["staff"].id
    assert order["price_version_id"] == accounts["price"].id
    assert order["scheduled_start"] == "2026-10-16T10:00:00"

    detail = client.get(f"/api/v1/appointments/{appointment_id}", headers=headers("grid")).json()["data"]
    assert detail["status"] == "dispatched"

    stored = db.get(WorkOrder, order["id"])
    db.refresh(stored)
    assert stored.price_version_id == accounts["price"].id
    audit = db.query(AuditLog).filter(AuditLog.action == "dispatch").all()
    assert audit


def test_price_change_only_affects_new_work_orders(client, headers, accounts, make_elder, db):
    admin = headers("org_admin")
    item = accounts["item"]

    first_appointment = _create_appointment(client, headers, accounts, "2026-10-16T09:00:00")
    _to_pending_dispatch(client, headers, first_appointment)
    first_order = client.post(
        f"/api/v1/dispatch/{first_appointment}/assign",
        json={"nurse_id": accounts["staff"].id},
        headers=headers("grid"),
    ).json()["data"]
    assert first_order["unit_price"] == 120.0

    changed = client.post(
        f"/api/v1/catalog/items/{item.id}/prices",
        json={"unit_price": "150.00"},
        headers=admin,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["data"]["item"]["unit_price"] == 150.0

    elder2 = make_elder(ability_level=AbilityLevel.MODERATE.value)
    second_appointment = _create_appointment(client, headers, accounts, "2026-10-16T14:00:00", elder_id=elder2.id)
    _to_pending_dispatch(client, headers, second_appointment)
    second_order = client.post(
        f"/api/v1/dispatch/{second_appointment}/assign",
        json={"nurse_id": accounts["staff"].id},
        headers=headers("grid"),
    ).json()["data"]
    assert second_order["unit_price"] == 150.0
    assert second_order["amount"] == 150.0

    old_order = db.get(WorkOrder, first_order["id"])
    db.refresh(old_order)
    assert old_order.unit_price == 120.0
    assert old_order.amount == 120.0


def test_assign_conflict_when_nurse_busy(client, headers, accounts, make_elder):
    first_appointment = _create_appointment(client, headers, accounts, "2026-10-16T10:00:00")
    _to_pending_dispatch(client, headers, first_appointment)
    first = client.post(
        f"/api/v1/dispatch/{first_appointment}/assign",
        json={"nurse_id": accounts["staff"].id},
        headers=headers("grid"),
    )
    assert first.status_code == 200, first.text

    elder2 = make_elder(ability_level=AbilityLevel.MODERATE.value)
    second_appointment = _create_appointment(client, headers, accounts, "2026-10-16T10:30:00", elder_id=elder2.id)
    _to_pending_dispatch(client, headers, second_appointment)
    second = client.post(
        f"/api/v1/dispatch/{second_appointment}/assign",
        json={"nurse_id": accounts["staff"].id},
        headers=headers("grid"),
    )
    assert second.status_code == 409
    assert second.json()["code"] == "CONFLICT"


def test_reassign_requires_reason_and_keeps_history(client, headers, accounts, make_staff, db):
    replacement = make_staff(name="改派护理员", base_lat=31.2312, base_lng=121.4742)
    appointment_id = _create_appointment(client, headers, accounts, "2026-10-16T10:00:00")
    _to_pending_dispatch(client, headers, appointment_id)
    order = client.post(
        f"/api/v1/dispatch/{appointment_id}/assign",
        json={"nurse_id": accounts["staff"].id},
        headers=headers("grid"),
    ).json()["data"]

    missing_reason = client.post(
        f"/api/v1/dispatch/orders/{order['id']}/reassign",
        json={"nurse_id": replacement.id, "reason": "  "},
        headers=headers("grid"),
    )
    assert missing_reason.status_code == 400
    assert missing_reason.json()["code"] == "RULE_VIOLATION"

    resp = client.post(
        f"/api/v1/dispatch/orders/{order['id']}/reassign",
        json={"nurse_id": replacement.id, "reason": "原护理员临时请假"},
        headers=headers("grid"),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["old_order_id"] == order["id"]
    assert data["order"]["nurse_id"] == replacement.id
    assert data["order"]["status"] == "pending_accept"
    assert data["order"]["unit_price"] == 120.0

    old_order = db.get(WorkOrder, order["id"])
    db.refresh(old_order)
    assert old_order.status == "cancelled"
    audit = db.query(AuditLog).filter(AuditLog.action == "reassign").all()
    assert audit and audit[-1].entity_id == data["order"]["id"]

    detail = client.get(f"/api/v1/appointments/{appointment_id}", headers=headers("grid")).json()["data"]
    assert detail["status"] == "dispatched"


def test_scan_timeout_requeues_appointment(client, headers, accounts):
    with freeze_time("2026-10-15 09:00:00"):
        appointment_id = _create_appointment(client, headers, accounts, "2026-10-15T09:40:00")
        _to_pending_dispatch(client, headers, appointment_id)
        order = client.post(
            f"/api/v1/dispatch/{appointment_id}/assign",
            json={"nurse_id": accounts["staff"].id},
            headers=headers("grid"),
        ).json()["data"]

        not_yet = client.post("/api/v1/dispatch/scan-timeout", headers=headers("grid"))
        assert not_yet.status_code == 200, not_yet.text
        assert not_yet.json()["data"]["scanned"] == 0

    with freeze_time("2026-10-15 09:30:00"):
        scanned = client.post("/api/v1/dispatch/scan-timeout", headers=headers("grid"))
    assert scanned.status_code == 200, scanned.text
    data = scanned.json()["data"]
    assert data["scanned"] == 1
    assert data["requeued"] == [appointment_id]
    assert data["requeued_orders"][0]["order_id"] == order["id"]

    detail = client.get(f"/api/v1/appointments/{appointment_id}", headers=headers("grid")).json()["data"]
    assert detail["status"] == "pending_dispatch"


def test_dispatch_permission_denied(client, headers, accounts):
    appointment_id = _create_appointment(client, headers, accounts, "2026-10-16T10:00:00")
    denied = client.get(f"/api/v1/dispatch/{appointment_id}/candidates", headers=headers("elder"))
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN"


def test_assign_unknown_nurse_returns_404(client, headers, accounts):
    appointment_id = _create_appointment(client, headers, accounts, "2026-10-16T10:00:00")
    _to_pending_dispatch(client, headers, appointment_id)
    resp = client.post(
        f"/api/v1/dispatch/{appointment_id}/assign",
        json={"nurse_id": 999999},
        headers=headers("grid"),
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_candidates_requires_pending_status(client, headers, accounts):
    appointment_id = _create_appointment(client, headers, accounts, "2026-10-16T10:00:00")
    cancelled = client.post(
        f"/api/v1/appointments/{appointment_id}/cancel",
        json={"reason": "不需要了"},
        headers=headers("elder"),
    )
    assert cancelled.status_code == 200, cancelled.text
    resp = client.get(f"/api/v1/dispatch/{appointment_id}/candidates", headers=headers("grid"))
    assert resp.status_code == 409
    assert resp.json()["code"] == "CONFLICT"


@pytest.mark.parametrize("desired_start", ["2026-10-16T11:00:00"])
def test_candidates_without_weights_config_returns_list(client, headers, accounts, desired_start):
    appointment_id = _create_appointment(client, headers, accounts, desired_start)
    _to_pending_dispatch(client, headers, appointment_id)
    resp = client.get(f"/api/v1/dispatch/{appointment_id}/candidates", headers=headers("grid"))
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert isinstance(rows, list)
    assert all(row["weights"] == pytest.approx([0.4, 0.3, 0.2, 0.1]) for row in rows)
