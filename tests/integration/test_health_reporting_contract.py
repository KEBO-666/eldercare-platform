"""健康上报：逐条保存、时区、关联归属及预警评估反馈的可观察契约。"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app.models import Alert, Device, HealthMetric, Notification, WorkOrder
from app.services import alert_service, health_service

BASE_URL = "/api/v1/health/metrics"


@pytest.fixture(autouse=True)
def isolated_sms_log(tmp_path, monkeypatch):
    monkeypatch.setattr(alert_service, "sms_log_path", lambda: tmp_path / "sms_sim.log")


def _post(client, headers, accounts, **changes):
    payload = {"elder_id": accounts["elder"].id, "metric_code": "hr", "value": 75, **changes}
    response = client.post(BASE_URL, headers=headers("super"), json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_offset_times_represent_same_beijing_instant_and_query(client, headers, accounts, db):
    first = _post(client, headers, accounts, measured_at="2026-10-15T01:30:00Z")
    second = _post(client, headers, accounts, measured_at="2026-10-15T09:30:00+08:00")
    assert first["metric"]["measured_at"] == second["metric"]["measured_at"] == "2026-10-15T09:30:00"
    assert db.get(HealthMetric, first["metric_id"]).measured_at == datetime(2026, 10, 15, 9, 30)
    response = client.get(
        f"/api/v1/health/elders/{accounts['elder'].id}/metrics",
        headers=headers("super"),
        params={"from_time": "2026-10-15T01:30:00Z", "to_time": "2026-10-15T01:30:00Z"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["total"] == 2


def test_batch_bad_fields_do_not_reject_valid_rows(client, headers, accounts, db):
    valid = {"elder_id": accounts["elder"].id, "metric_code": "hr", "value": 75}
    response = client.post(
        f"{BASE_URL}/batch",
        headers=headers("super"),
        json={"items": [
            valid,
            {**valid, "measured_at": "not-a-date"},
            {**valid, "value": "NaN"},
            {**valid, "source": "unknown"},
            {**valid, "value": "Infinity"},
            {**valid, "value": True},
            None,
            {**valid, "value": "not-a-number"},
            {**valid, "measured_at": 12345},
            {**valid, "measured_at": "2026-10-15T09:30:00+08:00", "source": "wearable"},
        ]},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["success"] == 2
    assert [row["index"] for row in data["results"]] == [0, 9]
    assert [row["index"] for row in data["failed"]] == list(range(1, 9))
    assert all(row["code"] == "RULE_VIOLATION" for row in data["failed"])
    assert all(row["alert_evaluation"]["status"] == "no_match" for row in data["results"])
    assert db.query(HealthMetric).count() == 2


@pytest.mark.parametrize("change", [
    {"value": "NaN"}, {"value": "Infinity"}, {"value": True},
    {"measured_at": "invalid-date"}, {"measured_at": 12345},
    {"source": "unknown"},
])
def test_single_invalid_value_time_or_source_never_persists(client, headers, accounts, db, change):
    response = client.post(
        BASE_URL, headers=headers("super"),
        json={"elder_id": accounts["elder"].id, "metric_code": "hr", "value": 75, **change},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "RULE_VIOLATION"
    assert db.query(HealthMetric).count() == 0


def test_report_distinguishes_created_cooldown_and_no_rules(client, headers, accounts, db):
    first = _post(client, headers, accounts, metric_code="sbp", value=180)
    assert first["alert_evaluation"]["status"] == "created"
    assert first["alert_evaluation"]["matched_rule_count"] >= 1
    assert first["alerts"] and db.get(Alert, first["alerts"][0]["id"]) is not None
    second = _post(client, headers, accounts, metric_code="sbp", value=185)
    assert second["alerts"] == []
    assert second["alert_evaluation"]["status"] == "cooldown"
    assert second["alert_evaluation"]["suppressed"][0]["reason"] == "cooldown"
    assert second["alert_evaluation"]["suppressed"][0]["existing_alert_id"] == first["alerts"][0]["id"]
    assert db.query(Alert).count() == len(first["alerts"])
    no_rules = _post(client, headers, accounts, metric_code="motion", value=50)
    assert no_rules["alert_evaluation"]["status"] == "no_rules"
    assert no_rules["alert_evaluation"]["evaluated_rule_count"] == 0


def test_evaluation_failure_retains_metric_rolls_back_partial_alert_and_continues(
    client, headers, accounts, db, monkeypatch,
):
    original = alert_service.evaluate_metric_report
    calls = 0

    def fail_after_partial_write(session, metric, operator_id=None):
        nonlocal calls
        calls += 1
        if calls != 1:
            return original(session, metric, operator_id)
        partial = Alert(elder_id=metric.elder_id, level="red", summary="partial must roll back")
        session.add(partial)
        session.flush()
        session.add(Notification(elder_id=metric.elder_id, title="partial notification"))
        session.flush()
        raise RuntimeError("internal evaluator error with private SQL parameters")

    monkeypatch.setattr(alert_service, "evaluate_metric_report", fail_after_partial_write)
    valid = {"elder_id": accounts["elder"].id, "metric_code": "hr", "value": 75}
    response = client.post(f"{BASE_URL}/batch", headers=headers("super"), json={"items": [valid, valid]})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["success"] == 2 and data["failed"] == []
    assert db.query(HealthMetric).count() == 2
    assert db.query(Alert).count() == db.query(Notification).count() == 0
    assert [row["alert_evaluation"]["status"] for row in data["results"]] == ["error", "no_match"]
    failed_evaluation = data["results"][0]["alert_evaluation"]
    assert "已保存" in failed_evaluation["message"] and "勿重复录入" in failed_evaluation["message"]
    assert "private SQL" not in response.text


def test_database_failure_isolated_and_does_not_expose_parameters(client, headers, accounts, db):
    def reject_middle_row(session, flush_context, instances):
        if any(isinstance(row, HealthMetric) and row.value == 121 for row in session.new):
            raise IntegrityError("private INSERT", "secret-parameter", RuntimeError("synthetic failure"))

    event.listen(db, "before_flush", reject_middle_row)
    try:
        valid = {"elder_id": accounts["elder"].id, "metric_code": "sbp"}
        response = client.post(
            f"{BASE_URL}/batch", headers=headers("super"),
            json={"items": [{**valid, "value": value} for value in (120, 121, 122)]},
        )
    finally:
        event.remove(db, "before_flush", reject_middle_row)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["success"] == 2 and data["failed"][0]["index"] == 1
    assert data["failed"][0]["code"] == "SAVE_FAILED"
    assert [row["index"] for row in data["results"]] == [0, 2]
    assert sorted(row.value for row in db.query(HealthMetric).all()) == [120, 122]
    assert "secret-parameter" not in response.text and "private INSERT" not in response.text


def test_metric_references_must_belong_to_elder(client, headers, accounts, make_elder, db):
    other = make_elder()
    device = Device(elder_id=other.id, device_code="OTHER-DEVICE")
    order = WorkOrder(
        code="OTHER-ORDER", elder_id=other.id, service_item_id=accounts["item"].id,
        scheduled_start=datetime(2026, 10, 15, 9), community_org_id=other.community_org_id,
    )
    db.add_all([device, order])
    db.commit()
    for reference in ({"device_id": device.id}, {"work_order_id": order.id}, {"device_id": 999999}):
        response = client.post(
            BASE_URL, headers=headers("super"),
            json={"elder_id": accounts["elder"].id, "metric_code": "hr", "value": 75, **reference},
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "RULE_VIOLATION"
    assert db.query(HealthMetric).count() == 0
    order.elder_id = device.elder_id = accounts["elder"].id
    db.commit()
    data = _post(client, headers, accounts, device_id=device.id, work_order_id=order.id)
    assert data["metric"]["device_id"] == device.id
    assert data["metric"]["work_order_id"] == order.id


def test_batch_preserves_per_item_data_scope(accounts, make_elder, orgs, db):
    other = make_elder(grid_org_id=orgs["grid2"].id)
    db.commit()
    data = health_service.create_metrics_batch(
        db,
        [{"elder_id": elder.id, "metric_code": "hr", "value": 75} for elder in (accounts["elder"], other)],
        operator_id=accounts["users"]["grid"].id,
        user=accounts["users"]["grid"],
    )
    assert data["success"] == 1
    assert data["failed"][0]["index"] == 1 and data["failed"][0]["code"] == "FORBIDDEN"
    assert db.query(HealthMetric).count() == 1
