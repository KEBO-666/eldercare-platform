"""Traceability contracts use real HTTP requests and a fresh synthetic database."""
from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time
from sqlalchemy import select

from app.core.audit import verify_chain
from app.models import Alert, AuditLog, HealthMetric
from app.services import alert_service, health_service

NOW = datetime(2026, 11, 12, 10)
UA = "Eldercare-traceability-test/1.0"


@pytest.fixture(autouse=True)
def isolated_sms(tmp_path, monkeypatch):
    monkeypatch.setattr(alert_service, "sms_log_path", lambda: tmp_path / "sms.log")


def request_headers(headers, role="super"):
    return {**headers(role), "User-Agent": UA}


def make_alert(db, accounts):
    row = Alert(elder_id=accounts["elder"].id, level="red", status="pending", summary="合成追溯异常",
                created_at=NOW, pushed_at=NOW, deadline_at=NOW + timedelta(minutes=30))
    db.add(row)
    db.commit()
    return row


def post(client, headers, alert_id, action, payload=None):
    response = client.post(f"/api/v1/alerts/{alert_id}/{action}",
                           headers=request_headers(headers, "grid"), json=payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def audit_row(db, entity_type, entity_id, action):
    rows = db.scalars(select(AuditLog).where(
        AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id, AuditLog.action == action,
    ).order_by(AuditLog.id)).all()
    assert len(rows) == 1, f"Expected exactly one {entity_type}/{entity_id}/{action} audit, got {len(rows)}"
    return rows[0]


def assert_actor(row, user):
    assert row.user_id == user.id
    assert row.username == user.username
    assert row.ip == "testclient"
    assert row.ua == UA


def assert_transition(row, before, after):
    assert row.before_json is not None, "State-changing audit must preserve the prior snapshot"
    assert row.after_json is not None
    assert row.before_json["lifecycle_status"] == before
    assert row.after_json["lifecycle_status"] == after
    assert "status" in row.before_json and "status" in row.after_json


def test_metric_save_audit_contains_actor_request_and_exact_input(client, headers, accounts, db):
    payload = {"elder_id": accounts["elder"].id, "metric_code": "hr", "value": 72,
               "measured_at": "2026-11-12T09:58:00+08:00", "source": "visit"}
    response = client.post("/api/v1/health/metrics", headers=request_headers(headers), json=payload)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    row = audit_row(db, "health_metric", data["metric_id"], "create")
    assert_actor(row, accounts["users"]["super"])
    assert row.before_json is None
    for key in ("elder_id", "metric_code", "value", "source"):
        assert row.after_json[key] == payload[key]
    assert row.after_json["recorded_by"] == accounts["users"]["super"].id
    assert row.after_json["measured_at"].startswith("2026-11-12T09:58:00")
    assert db.get(HealthMetric, data["metric_id"]).recorded_by == row.user_id
    assert verify_chain(db)["valid"]


def test_batch_audits_only_saved_metrics_and_preserves_request_attribution(client, headers, accounts, db):
    valid = {"elder_id": accounts["elder"].id, "metric_code": "hr", "value": 74}
    response = client.post("/api/v1/health/metrics/batch", headers=request_headers(headers),
                           json={"items": [valid, {**valid, "value": "invalid"}, {**valid, "value": 76}]})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["success"] == 2
    rows = db.scalars(select(AuditLog).where(
        AuditLog.entity_type == "health_metric", AuditLog.action == "create",
    )).all()
    assert len(rows) == 2
    assert {row.entity_id for row in rows} == {result["metric_id"] for result in data["results"]}
    for row in rows:
        assert_actor(row, accounts["users"]["super"])
        evaluation = audit_row(db, "health_metric", row.entity_id, "evaluate")
        assert_actor(evaluation, accounts["users"]["super"])
        assert evaluation.after_json["status"] == "no_match"
    assert verify_chain(db)["valid"]


def test_metric_evaluation_audit_matches_created_cooldown_and_no_rules(client, headers, accounts, db):
    for code, value, status in (("sbp", 180, "created"), ("sbp", 185, "cooldown"), ("motion", 50, "no_rules")):
        response = client.post("/api/v1/health/metrics", headers=request_headers(headers), json={
            "elder_id": accounts["elder"].id, "metric_code": code, "value": value,
        })
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        row = audit_row(db, "health_metric", data["metric_id"], "evaluate")
        assert_actor(row, accounts["users"]["super"])
        assert row.after_json["status"] == data["alert_evaluation"]["status"] == status
        assert row.after_json["alert_ids"] == [alert["id"] for alert in data["alerts"]]
        for key in ("evaluated_rule_count", "matched_rule_count", "suppressed"):
            assert row.after_json[key] == data["alert_evaluation"][key]
    assert verify_chain(db)["valid"]


def test_evaluation_error_audit_preserves_saved_metric_without_internal_details(
    client, headers, accounts, db, monkeypatch,
):
    def fail(session, metric, operator_id=None):
        raise RuntimeError("private evaluator parameters must not escape")

    monkeypatch.setattr(alert_service, "evaluate_metric_report", fail)
    response = client.post("/api/v1/health/metrics", headers=request_headers(headers), json={
        "elder_id": accounts["elder"].id, "metric_code": "hr", "value": 75,
    })
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["alert_evaluation"]["status"] == "error"
    assert db.get(HealthMetric, data["metric_id"]) is not None
    row = audit_row(db, "health_metric", data["metric_id"], "evaluate")
    assert_actor(row, accounts["users"]["super"])
    assert row.after_json["status"] == "error" and row.after_json["alert_ids"] == []
    assert "private evaluator" not in response.text
    assert "private evaluator" not in str(row.after_json)
    assert "private evaluator" not in row.detail
    assert verify_chain(db)["valid"]


def test_metric_create_audit_failure_cannot_be_committed_without_its_audit(db, accounts, monkeypatch):
    original = health_service.write_audit

    def fail_after_write(session, **kwargs):
        original(session, **kwargs)
        raise RuntimeError("synthetic audit storage failure")

    monkeypatch.setattr(health_service, "write_audit", fail_after_write)
    with pytest.raises(RuntimeError, match="synthetic audit storage failure"):
        health_service.create_metric_report(
            db, elder_id=accounts["elder"].id, metric_code="hr", value=75,
            recorded_by=accounts["users"]["super"].id,
        )
    db.commit()
    assert db.scalars(select(HealthMetric)).all() == []
    assert db.scalars(select(AuditLog)).all() == []
    assert verify_chain(db)["valid"]


def test_manual_report_audit_identifies_new_alert_and_full_report(client, headers, accounts, db):
    note = "现场情况：" + "家属已经确认并联系社区医生。" * 15
    response = client.post("/api/v1/alerts/report", headers=request_headers(headers), json={
        "elder_id": accounts["elder"].id, "summary": "人工发现异常", "note": note, "level": "yellow",
    })
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    row = audit_row(db, "alert", data["id"], "report")
    assert_actor(row, accounts["users"]["super"])
    creation = audit_row(db, "alert", data["id"], "create")
    assert_actor(creation, accounts["users"]["super"])
    assert creation.before_json is None
    assert creation.after_json["source"]["source"] == "manual_report"
    assert creation.after_json["source"]["note"] == note
    assert_transition(row, "unacknowledged", "unacknowledged")
    assert row.after_json["elder_id"] == accounts["elder"].id
    assert row.after_json["summary"] == "人工发现异常"
    assert row.after_json["level"] == "yellow"
    assert row.after_json["note"] == note
    assert row.after_json["lifecycle_status"] == "unacknowledged"


def test_ack_audit_has_before_after_state_and_no_duplicate_entry(client, headers, accounts, db):
    alert = make_alert(db, accounts)
    with freeze_time(NOW + timedelta(minutes=2)):
        first = post(client, headers, alert.id, "ack")
    row = audit_row(db, "alert", alert.id, "ack")
    assert_actor(row, accounts["users"]["grid"])
    assert_transition(row, "unacknowledged", "responded")
    assert row.before_json["response_seconds"] is None
    assert row.after_json["response_seconds"] == first["response_seconds"] == 120
    with freeze_time(NOW + timedelta(minutes=5)):
        post(client, headers, alert.id, "ack")
    assert audit_row(db, "alert", alert.id, "ack").id == row.id
    assert verify_chain(db)["valid"]


def test_progress_and_referral_followups_audit_actual_lifecycle_transitions(client, headers, accounts, db):
    alert = make_alert(db, accounts)
    with freeze_time(NOW + timedelta(minutes=1)):
        post(client, headers, alert.id, "ack")
    with freeze_time(NOW + timedelta(minutes=2)):
        post(client, headers, alert.id, "progress", {"note": "已复核，正在联系接收医生"})
    progress = audit_row(db, "alert", alert.id, "progress")
    assert_actor(progress, accounts["users"]["grid"])
    assert_transition(progress, "responded", "handling")
    assert progress.after_json["note"] == "已复核，正在联系接收医生"
    with freeze_time(NOW + timedelta(minutes=3)):
        post(client, headers, alert.id, "handle", {"resolution_type": "referred", "measure": "已交接社区医生", "note": "家属陪同"})
    handle = audit_row(db, "alert", alert.id, "handle")
    assert_transition(handle, "handling", "awaiting_followup")
    with freeze_time(NOW + timedelta(minutes=4)):
        post(client, headers, alert.id, "follow-up", {"note": "已接诊，等待复查", "close": False})
    followup = audit_row(db, "alert", alert.id, "followup")
    assert_actor(followup, accounts["users"]["grid"])
    assert_transition(followup, "awaiting_followup", "awaiting_followup")
    assert followup.after_json["note"] == "已接诊，等待复查"
    with freeze_time(NOW + timedelta(minutes=8)):
        post(client, headers, alert.id, "follow-up", {"note": "已完成诊疗，社区接续观察", "close": True})
    closure = audit_row(db, "alert", alert.id, "followup_close")
    assert_actor(closure, accounts["users"]["grid"])
    assert_transition(closure, "awaiting_followup", "closed")
    assert closure.after_json["closed"] is True
    assert closure.after_json["closed_at"]
    assert closure.after_json["note"] == "已完成诊疗，社区接续观察"
    assert verify_chain(db)["valid"]


def test_handle_audit_preserves_long_measure_and_entire_supplemental_note(client, headers, accounts, db):
    alert = make_alert(db, accounts)
    measure = "实际处置记录：" + "逐项核实检查并向家属解释，安排后续观察。" * 60
    note = "补充说明：" + "家属已确认理解处置结果。" * 15
    result = post(client, headers, alert.id, "handle", {"resolution_type": "resolved", "measure": measure, "note": note})
    assert result["measure"] == measure and result["resolution_note"] == note
    row = audit_row(db, "alert", alert.id, "handle")
    assert_actor(row, accounts["users"]["grid"])
    assert row.after_json["measure"] == measure
    assert row.after_json["resolution_note"] == note
    assert row.after_json["closed"] is True
    assert verify_chain(db)["valid"]


def test_tampering_with_protected_handle_measure_breaks_audit_hash(client, headers, accounts, db):
    alert = make_alert(db, accounts)
    post(client, headers, alert.id, "handle", {"resolution_type": "resolved", "measure": "实际处置记录"})
    row = audit_row(db, "alert", alert.id, "handle")
    assert verify_chain(db)["valid"]
    row.after_json = {**row.after_json, "measure": "被篡改的处置记录"}
    db.commit()
    result = verify_chain(db)
    assert result["valid"] is False
    assert result["broken_id"] == row.id and result["reason"] == "hash 不匹配"


def test_manual_scan_is_audited_as_request_actor(client, headers, accounts, db):
    response = client.post("/api/v1/alerts/scan", headers=request_headers(headers), json={})
    assert response.status_code == 200, response.text
    rows = db.scalars(select(AuditLog).where(AuditLog.action == "scan", AuditLog.entity_type == "alert_scan")).all()
    assert len(rows) == 1, "Manual scan must be distinguishable from scheduler execution"
    row = rows[0]
    assert_actor(row, accounts["users"]["super"])
    assert row.after_json["trigger"] == "manual"
    for key, value in response.json()["data"].items():
        assert row.after_json[key] == value
    assert verify_chain(db)["valid"]


def test_automatic_scan_is_audited_as_system_execution(db, accounts):
    result = alert_service.scan_alerts(db, now=NOW)
    rows = db.scalars(select(AuditLog).where(AuditLog.action == "scan", AuditLog.entity_type == "alert_scan")).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id is None and row.ip is None and row.ua is None
    assert row.after_json["trigger"] == "scheduled"
    for key, value in result.items():
        assert row.after_json[key] == value
    assert verify_chain(db)["valid"]


def test_admin_can_query_audit_for_one_exact_alert_and_regular_handlers_cannot(client, headers, accounts, db):
    first = make_alert(db, accounts)
    second = make_alert(db, accounts)
    post(client, headers, first.id, "progress", {"note": "第一条预警记录"})
    post(client, headers, second.id, "progress", {"note": "第二条预警记录"})
    params = {"entity_type": "alert", "entity_id": first.id, "size": 100}
    response = client.get("/api/v1/admin/audit-logs", headers=headers("super"), params=params)
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert items and {item["entity_id"] for item in items} == {first.id}
    assert all(item["before"] is not None and item["after"] is not None for item in items)
    assert client.get("/api/v1/admin/audit-logs", headers=headers("grid"), params=params).status_code == 403
    assert verify_chain(db)["valid"]
