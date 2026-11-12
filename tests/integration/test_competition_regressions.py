"""比赛验收回归：周期预约、调度边界、跨网格读写和照片授权。"""
from __future__ import annotations

import io
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time
from PIL import Image
from sqlalchemy import func, select

from app.core.config import settings
from app.core.security import create_access_token
from app.models import (
    AnomalyReport,
    Appointment,
    AuditLog,
    Bill,
    BillDetail,
    CallbackTask,
    Complaint,
    Evaluation,
    Notification,
    User,
    WorkOrder,
)
from app.services import appointment_service, dispatch_service

START = datetime(2026, 12, 1, 10)


def prepare(db, accounts, start=START):
    grid = accounts["users"]["grid"]
    appt = appointment_service.create_appointment(
        db, service_item_id=accounts["item"].id, desired_start=start,
        user=accounts["users"]["elder"],
    )
    appointment_service.accept_appointment(db, appt["id"], user=grid)
    appointment_service.to_dispatch_appointment(db, appt["id"], user=grid)
    return appt["id"]


def test_weekly_appointments_create_four_real_occurrences(client, headers, accounts, db):
    response = client.post("/api/v1/appointments", headers=headers("elder"), json={
        "service_item_id": accounts["item"].id, "desired_start": START.isoformat(), "cycle_rule": "weekly",
    })
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["occurrence_count"] == 4
    assert [r["desired_start"] for r in data["occurrences"]] == [
        (START + timedelta(weeks=i)).isoformat() for i in range(4)
    ]
    assert db.scalar(select(func.count(Appointment.id))) == 4
    assert all(row["cycle_rule"] == "weekly" for row in data["occurrences"])


def test_weekly_conflict_rolls_back_entire_group(client, headers, accounts, db):
    prepare(db, accounts, START + timedelta(weeks=2))
    audit_before = db.scalar(select(func.count(AuditLog.id)))
    response = client.post("/api/v1/appointments", headers=headers("elder"), json={
        "service_item_id": accounts["item"].id, "desired_start": START.isoformat(), "cycle_rule": "weekly",
    })
    assert response.status_code == 422, response.text
    assert db.scalar(select(func.count(Appointment.id))) == 1
    assert db.scalar(select(func.count(AuditLog.id))) == audit_before


def test_unknown_cycle_rule_is_rejected(client, headers, accounts):
    response = client.post("/api/v1/appointments", headers=headers("elder"), json={
        "service_item_id": accounts["item"].id, "desired_start": START.isoformat(), "cycle_rule": "daily",
    })
    assert response.status_code == 400


@pytest.mark.parametrize("invalid", ["skills", "distance", "radius", "off"])
def test_manual_assign_cannot_bypass_hard_constraints(db, accounts, invalid):
    appointment_id = prepare(db, accounts)
    staff = accounts["staff"]
    if invalid == "skills":
        staff.skills = ["cleaning"]
    elif invalid == "distance":
        staff.base_lat = 40.0
    elif invalid == "radius":
        staff.service_radius_km = 0
    else:
        accounts["item"].status = "off"
    db.commit()
    from app.core.exceptions import RuleViolation

    with pytest.raises(RuleViolation):
        dispatch_service.assign_order(db, appointment_id, staff.id, user=accounts["users"]["grid"])
    assert db.scalar(select(func.count(WorkOrder.id))) == 0
    assert db.get(Appointment, appointment_id).status == "pending_dispatch"


def test_load_uses_service_day_and_limits_manual_assign(db, accounts):
    appointment_id = prepare(db, accounts)
    for i in range(6):
        db.add(WorkOrder(code=f"FULL-{i}", elder_id=accounts["elder"].id,
                         service_item_id=accounts["item"].id, nurse_id=accounts["staff"].id,
                         status="completed", scheduled_start=START.replace(hour=0) + timedelta(hours=i),
                         community_org_id=accounts["elder"].community_org_id))
    db.commit()
    assert dispatch_service.get_candidates(db, appointment_id, accounts["users"]["grid"]) == []
    from app.core.exceptions import Conflict

    with pytest.raises(Conflict):
        dispatch_service.assign_order(db, appointment_id, accounts["staff"].id, user=accounts["users"]["grid"])


def test_today_load_does_not_block_future_service(db, accounts):
    with freeze_time("2026-11-12 09:00:00"):
        appointment_id = prepare(db, accounts)
        for i in range(6):
            db.add(WorkOrder(code=f"TODAY-{i}", elder_id=accounts["elder"].id,
                             service_item_id=accounts["item"].id, nurse_id=accounts["staff"].id,
                             scheduled_start=datetime(2026, 11, 12, i), status="completed",
                             community_org_id=accounts["elder"].community_org_id))
        db.commit()
        rows = dispatch_service.get_candidates(db, appointment_id, accounts["users"]["grid"])
        assert rows[0]["today_count"] == 0
        assert dispatch_service.assign_order(
            db, appointment_id, accounts["staff"].id, user=accounts["users"]["grid"],
        )["status"] == "pending_accept"


def test_timeout_advances_once_then_requires_manual_coordination(db, accounts, make_staff):
    replacement = make_staff()
    db.commit()
    with freeze_time("2026-11-12 09:00:00"):
        appointment_id = prepare(db, accounts)
        first = dispatch_service.assign_order(
            db, appointment_id, accounts["staff"].id, user=accounts["users"]["grid"],
        )
    with freeze_time("2026-11-12 09:29:59"):
        assert dispatch_service.scan_accept_timeout(db)["scanned"] == 0
    with freeze_time("2026-11-12 09:30:00"):
        result = dispatch_service.scan_accept_timeout(db)
        assert result["scanned"] == 1
        assert result["reassigned_orders"][0]["nurse_id"] == replacement.id
        assert db.get(WorkOrder, first["id"]).status == "cancelled"
        assert db.get(Appointment, appointment_id).status == "dispatched"
        assert dispatch_service.scan_accept_timeout(db)["scanned"] == 0
    with freeze_time("2026-11-12 10:00:00"):
        result = dispatch_service.scan_accept_timeout(db)
        assert result["reassigned_orders"] == []
        assert db.get(Appointment, appointment_id).status == "pending_dispatch"
        assert db.scalar(select(func.count(Notification.id)).where(Notification.title == "派单需人工协调")) == 2


@pytest.mark.parametrize("action", ["cancel", "no-show"])
def test_ending_appointment_releases_nurse_slot(client, headers, db, accounts, action):
    appointment_id = prepare(db, accounts)
    order = dispatch_service.assign_order(db, appointment_id, accounts["staff"].id, user=accounts["users"]["grid"])
    response = client.post(f"/api/v1/appointments/{appointment_id}/{action}",
                           json={"reason": "老人临时住院"}, headers=headers("grid"))
    assert response.status_code == 200, response.text
    db.refresh(db.get(WorkOrder, order["id"]))
    assert db.get(WorkOrder, order["id"]).status == "cancelled"
    assert dispatch_service.find_busy_order(db, accounts["staff"].id, START, START + timedelta(hours=1)) is None


def test_cancel_uses_price_snapshot(db, accounts):
    with freeze_time("2026-12-01 09:00:00"):
        appointment_id = prepare(db, accounts)
        dispatch_service.assign_order(db, appointment_id, accounts["staff"].id, user=accounts["users"]["grid"])
        from app.services import catalog_service

        catalog_service.add_price_version(db, accounts["item"].id, unit_price="999.00", user=accounts["users"]["super"])
        result = appointment_service.cancel_appointment(
            db, appointment_id, reason="取消", user=accounts["users"]["elder"],
        )
        assert result["cancellation_fee"] == 120


@pytest.fixture()
def other_grid_records(db, accounts, make_elder, orgs):
    elder = make_elder(grid_org_id=orgs["grid2"].id)
    appt = Appointment(code="OTHER-AP", elder_id=elder.id, service_item_id=accounts["item"].id,
                       desired_start=START, community_org_id=elder.community_org_id, status="dispatched")
    db.add(appt)
    db.flush()
    order = WorkOrder(code="OTHER-WO", appointment_id=appt.id, elder_id=elder.id,
                      service_item_id=accounts["item"].id, nurse_id=accounts["staff"].id,
                      scheduled_start=START, community_org_id=elder.community_org_id, status="anomaly")
    db.add(order)
    db.flush()
    complaint = Complaint(code="OTHER-CP", elder_id=elder.id, work_order_id=order.id, content="待核实")
    callback = CallbackTask(elder_id=elder.id, due_at=START)
    anomaly = AnomalyReport(elder_id=elder.id, work_order_id=order.id, description="老人不在家")
    db.add_all([complaint, callback, anomaly, Evaluation(work_order_id=order.id, elder_id=elder.id)])
    db.commit()
    return dict(elder=elder, appt=appt, order=order, complaint=complaint, callback=callback, anomaly=anomaly)


@pytest.mark.parametrize("path", ["appointments", "orders", "quality/evaluations", "quality/complaints"])
def test_grid_lists_do_not_expose_sibling_grid(client, headers, other_grid_records, path):
    response = client.get(f"/api/v1/{path}", headers=headers("grid"))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["total"] == 0


@pytest.mark.parametrize("action", ["callback", "complaint", "assign", "anomaly"])
def test_cross_grid_writes_are_denied(client, headers, accounts, other_grid_records, action):
    records = other_grid_records
    requests = {
        "callback": (f"quality/callbacks/{records['callback'].id}/finish", {"result": "improve", "note": "已回访"}),
        "complaint": (f"quality/complaints/{records['complaint'].id}/conclude", {"conclusion": "不成立", "is_established": False}),
        "assign": (f"quality/complaints/{records['complaint'].id}/assign", {"handler_id": accounts["users"]["grid"].id}),
        "anomaly": (f"orders/anomalies/{records['anomaly'].id}/handle", {"handle_note": "已联系"}),
    }
    path, body = requests[action]
    response = client.post(f"/api/v1/{path}", json=body, headers=headers("grid"))
    assert response.status_code == 403, response.text
    assert records["complaint"].status == "new"
    assert records["anomaly"].status == "pending"


@pytest.mark.parametrize("role", ["elder", "nurse"])
def test_unbound_accounts_have_no_archive_access(client, headers, db, accounts, role):
    bound_user = accounts["users"][role]
    user = User(username="unbound", real_name="未绑定用户", role_id=bound_user.role_id, org_id=bound_user.org_id,
                password_hash=bound_user.password_hash, data_scope="self")
    db.add(user)
    db.commit()
    auth = {"Authorization": f"Bearer {create_access_token(user.id)}"}
    response = client.get("/api/v1/elders", headers=auth)
    assert response.status_code == 200
    assert response.json()["data"]["total"] == 0
    assert client.get(f"/api/v1/elders/{accounts['elder'].id}", headers=auth).status_code == 403


def test_timeout_scan_is_limited_to_operator_grid(client, headers, db, other_grid_records):
    order = other_grid_records["order"]
    order.status = "pending_accept"
    order.created_at = datetime.now() - timedelta(hours=1)
    db.commit()
    response = client.post("/api/v1/dispatch/scan-timeout", headers=headers("grid"))
    assert response.status_code == 200
    assert response.json()["data"]["scanned"] == 0
    assert order.status == "pending_accept"


def test_photo_preview_reads_actual_file_and_checks_scope(client, headers, db, accounts, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    appointment_id = prepare(db, accounts)
    order = dispatch_service.assign_order(db, appointment_id, accounts["staff"].id, user=accounts["users"]["grid"])
    db.get(WorkOrder, order["id"]).status = "in_service"
    db.commit()
    image = io.BytesIO()
    Image.new("RGB", (20, 20)).save(image, format="PNG")
    response = client.post(f"/api/v1/orders/{order['id']}/photos", headers=headers("nurse"),
                           files={"file": ("visit.png", image.getvalue(), "image/png")})
    assert response.status_code == 200, response.text
    filename = Path(response.json()["data"]["photo"]["path"]).name
    client.cookies.set("access_token", create_access_token(accounts["users"]["elder"].id))
    preview = client.get(f"/files/{filename}")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/jpeg"
    assert preview.headers["cache-control"] == "private, no-store"
    client.cookies.set("access_token", create_access_token(accounts["users"]["grid2"].id))
    assert client.get(f"/files/{filename}").status_code == 403


def test_org_admin_cannot_elevate_data_scope_or_role(client, headers, accounts):
    base = {"username": "elevated", "password": "Passw0rd!", "org_id": accounts["elder"].community_org_id}
    for payload in ({**base, "role": "street"}, {**base, "role": "elder", "data_scope": "street"}):
        response = client.post("/api/v1/admin/users", json=payload, headers=headers("org_admin"))
        assert response.status_code == 403, response.text


def test_elder_cannot_book_an_unrelated_elder_in_same_grid(client, headers, accounts, make_elder):
    other = make_elder(ability_level="moderate")
    response = client.post("/api/v1/appointments", headers=headers("elder"), json={
        "elder_id": other.id, "service_item_id": accounts["item"].id, "desired_start": START.isoformat(),
    })
    assert response.status_code == 403, response.text


def test_shared_bill_only_exposes_visible_elders(client, headers, accounts, db, make_elder, orgs):
    other = make_elder(name="不应出现的老人", grid_org_id=orgs["grid2"].id)
    bill = Bill(code="SHARED", period="2026-11", community_org_id=other.community_org_id,
                total_amount=999, subsidy_amount=0, ltc_amount=0, self_pay_amount=999)
    db.add(bill)
    db.flush()
    for elder_id, amount in ((accounts["elder"].id, 120), (other.id, 879)):
        db.add(BillDetail(bill_id=bill.id, elder_id=elder_id, amount=amount,
                          subsidy_amount=0, ltc_amount=0, self_pay_amount=amount))
    db.commit()
    for role in ("elder", "grid"):
        response = client.get(f"/api/v1/billing/bills/{bill.id}", headers=headers(role))
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["total_amount"] == 120
        assert data["details_count"] == 1
        assert data["details"][0]["elder_id"] == accounts["elder"].id
        content = client.get(f"/api/v1/billing/bills/{bill.id}/export", headers=headers(role))
        from openpyxl import load_workbook

        sheet = load_workbook(io.BytesIO(content.content)).active
        assert not any(cell.value == other.name for row in sheet for cell in row)
        client.cookies.set("access_token", create_access_token(accounts["users"][role].id))
        page = client.get("/billing")
        assert page.status_code == 200
        assert other.name not in page.text
        assert "879.00" not in page.text


def test_wearable_simulator_triggers_yellow_and_red_alerts(client, headers, accounts):
    from scripts.simulate_wearable import build_metrics

    response = client.post("/api/v1/health/metrics/batch", headers=headers("super"),
                           json={"items": build_metrics(accounts["elder"].id, "abnormal")})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["success"] == 6
    alerts = client.get("/api/v1/alerts", headers=headers("grid")).json()["data"]["items"]
    assert {row["level"] for row in alerts} >= {"yellow", "red"}


def test_old_ratings_and_future_ratings_do_not_influence_dispatch(db, accounts):
    with freeze_time("2026-11-12 09:00:00"):
        appointment_id = prepare(db, accounts)
        staff = accounts["staff"]
        staff.rating_cached = 1.0
        for i, (days, rating) in enumerate(((-31, 1.0), (-1, 4.0), (1, 1.0))):
            order = WorkOrder(code=f"RATING-{i}", elder_id=accounts["elder"].id,
                              service_item_id=accounts["item"].id, nurse_id=staff.id,
                              scheduled_start=datetime.now() + timedelta(days=days), status="completed",
                              community_org_id=accounts["elder"].community_org_id)
            db.add(order)
            db.flush()
            db.add(Evaluation(work_order_id=order.id, elder_id=order.elder_id, nurse_id=staff.id,
                              avg_score=rating, created_at=datetime.now() + timedelta(days=days)))
        db.commit()
        candidate = dispatch_service.get_candidates(db, appointment_id, accounts["users"]["grid"])[0]
        assert candidate["rating"] == 4.0
        assert candidate["factors"]["rating"] == 0.8


def test_reassign_cannot_bypass_required_skills(db, accounts, make_staff):
    replacement = make_staff(skills=["cleaning"])
    db.commit()
    appointment_id = prepare(db, accounts)
    order = dispatch_service.assign_order(db, appointment_id, accounts["staff"].id, user=accounts["users"]["grid"])
    from app.core.exceptions import RuleViolation

    with pytest.raises(RuleViolation, match="技能"):
        dispatch_service.reassign_order(db, order["id"], replacement.id, reason="临时请假", user=accounts["users"]["grid"])
    assert db.get(WorkOrder, order["id"]).status == "pending_accept"


def test_terminal_appointment_cannot_be_reopened_by_requeue(db, accounts):
    appointment_id = prepare(db, accounts)
    appointment_service.cancel_appointment(db, appointment_id, reason="取消", user=accounts["users"]["elder"])
    from app.core.exceptions import Conflict

    with pytest.raises(Conflict):
        appointment_service.requeue_appointment(db, db.get(Appointment, appointment_id), reason="非法重开")
    assert db.get(Appointment, appointment_id).status == "cancelled"
