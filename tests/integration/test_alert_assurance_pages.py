"""审计展示、精确时间及自动升级的可追溯合同。"""
from datetime import datetime, timedelta

from freezegun import freeze_time
from sqlalchemy import select

from app.core.audit import verify_chain
from app.core.security import create_access_token
from app.models import Alert, AuditLog, HealthMetric
from app.services import alert_service


def page_as(client, user, path):
    client.cookies.clear()
    client.cookies.set("access_token", create_access_token(user.id))
    return client.get(path)


def test_audit_page_filters_exact_entity_and_keeps_chinese_readable_but_escaped(client, headers, accounts, db):
    rows = []
    for summary in ("应显示的预警", "不应出现的另一条预警"):
        row = alert_service.report_alert(db, accounts["users"]["super"], elder_id=accounts["elder"].id,
                                         summary=summary, note="已经电话核实")
        rows.append(row)
    measure = '完整中文措施 <script>alert("禁止执行")</script>'
    alert_service.handle_alert(db, accounts["users"]["grid"], rows[0]["id"], resolution_type="resolved", measure=measure)
    response = page_as(client, accounts["users"]["super"], f'/admin/audit?entity_type=alert&entity_id={rows[0]["id"]}')
    assert response.status_code == 200
    assert "完整中文措施" in response.text
    assert "不应出现的另一条预警" not in response.text
    assert "&lt;script&gt;" in response.text
    assert '<script>alert(' not in response.text
    assert r"\u5b8c" not in response.text
    assert "操作前" in response.text and "操作后" in response.text
    assert verify_chain(db)["valid"]


def test_audit_links_are_visible_only_to_auditors(client, headers, accounts):
    response = client.post('/api/v1/alerts/report', headers=headers("super"), json={
        "elder_id": accounts["elder"].id, "summary": "审计入口测试", "note": "已核实",
    })
    alert_id = response.json()["data"]["id"]
    path = f"/alerts/{alert_id}"
    assert "关联审计记录" in page_as(client, accounts["users"]["super"], path).text
    assert "关联审计记录" not in page_as(client, accounts["users"]["grid"], path).text
    assert page_as(client, accounts["users"]["grid"], f"/admin/audit?entity_type=alert&entity_id={alert_id}").status_code == 403


def test_ack_audit_preserves_the_exact_first_timestamp(client, headers, accounts, db):
    now = datetime(2026, 11, 12, 10, 0, 0, 123456)
    row = Alert(elder_id=accounts["elder"].id, level="red", status="pending", created_at=now, pushed_at=now)
    db.add(row)
    db.commit()
    with freeze_time(now + timedelta(seconds=61, microseconds=320000)):
        response = client.post(f"/api/v1/alerts/{row.id}/ack", headers=headers("grid"))
    assert response.status_code == 200
    data = response.json()["data"]
    audit = db.scalar(select(AuditLog).where(AuditLog.entity_id == row.id, AuditLog.entity_type == "alert", AuditLog.action == "ack"))
    assert datetime.fromisoformat(audit.after_json["first_responded_at"]) == datetime.fromisoformat(data["first_responded_at"])
    assert data["first_responded_at"].endswith("443456")
    assert verify_chain(db)["valid"]


def test_escalation_writes_linked_state_audit_once_and_scan_origin_is_separate(db, accounts):
    now = datetime(2026, 11, 12, 10)
    db.add(HealthMetric(elder_id=accounts["elder"].id, metric_code="hr", value=70, measured_at=now))
    row = Alert(elder_id=accounts["elder"].id, level="red", status="pending", created_at=now,
                pushed_at=now, deadline_at=now + timedelta(minutes=30))
    db.add(row)
    db.commit()
    result = alert_service.scan_alerts(db, now + timedelta(minutes=30))
    assert result["escalated"] == 1
    audit = db.scalar(select(AuditLog).where(AuditLog.entity_type == "alert", AuditLog.entity_id == row.id, AuditLog.action == "escalate"))
    assert audit.before_json["escalated_at"] is None
    assert audit.after_json["escalated_at"] == "2026-11-12 10:30:00"
    assert audit.user_id is None
    assert alert_service.scan_alerts(db, now + timedelta(minutes=31))["escalated"] == 0
    audits = db.scalars(select(AuditLog).where(AuditLog.entity_type == "alert", AuditLog.entity_id == row.id, AuditLog.action == "escalate")).all()
    assert len(audits) == 1
    assert verify_chain(db)["valid"]


def test_direct_disposition_never_precedes_its_automatic_first_response(db, accounts, monkeypatch):
    now = datetime(2026, 11, 12, 10)
    row = Alert(elder_id=accounts["elder"].id, level="red", status="pending", created_at=now, pushed_at=now)
    db.add(row)
    db.commit()
    elapsed = 0

    def advancing_clock():
        nonlocal elapsed
        elapsed += 1
        return now + timedelta(milliseconds=elapsed)

    monkeypatch.setattr(alert_service, "utcnow", advancing_clock)
    data = alert_service.handle_alert(db, accounts["users"]["grid"], row.id, resolution_type="resolved", measure="已完成核实处置")
    assert data["first_responded_at"] <= data["resolved_at"] == data["closed_at"]
    audit = db.scalar(select(AuditLog).where(AuditLog.entity_type == "alert", AuditLog.entity_id == row.id, AuditLog.action == "handle"))
    assert datetime.fromisoformat(audit.after_json["first_responded_at"]) <= datetime.fromisoformat(audit.after_json["closed_at"])
