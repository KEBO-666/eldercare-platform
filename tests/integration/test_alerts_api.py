"""D2 模块集成测试：/api/v1/alerts 列表、详情、处置、规则、扫描与统计。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time

from app.models import Alert, AlertAction, AuditLog, HealthMetric, Notification
from app.services import alert_service

BASE_TIME = datetime(2026, 10, 15, 9, 0, 0)
FROZEN = "2026-10-15 09:00:00"
API = "/api/v1"


@pytest.fixture(autouse=True)
def sms_log(tmp_path, monkeypatch):
    path = tmp_path / "data" / "logs" / "sms_sim.log"
    monkeypatch.setattr(alert_service, "sms_log_path", lambda: path)
    return path


@pytest.fixture()
def make_alert(db, accounts):
    counter = {"n": 0}

    def _make(**kwargs) -> Alert:
        counter["n"] += 1
        created = kwargs.pop("created_at", BASE_TIME)
        defaults = dict(
            elder_id=accounts["elder"].id,
            level="red",
            status="pending",
            metric_code="sbp",
            metric_value=185.0,
            summary=f"测试预警{counter['n']}",
            created_at=created,
            updated_at=created,
            pushed_at=created,
            deadline_at=created + timedelta(minutes=30),
        )
        defaults.update(kwargs)
        alert = Alert(**defaults)
        db.add(alert)
        db.commit()
        db.refresh(alert)
        return alert

    return _make


# ---------------------------------------------------------------- 列表
def test_list_alerts_orders_by_level_then_time(client, headers, make_alert):
    red = make_alert(level="red", created_at=BASE_TIME)
    yellow = make_alert(level="yellow", created_at=BASE_TIME + timedelta(hours=2))
    blue = make_alert(level="blue", created_at=BASE_TIME + timedelta(hours=1))

    resp = client.get(f"{API}/alerts", headers=headers("grid"))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 3
    assert [item["level"] for item in data["items"]] == ["red", "yellow", "blue"]
    assert [item["id"] for item in data["items"]] == [red.id, yellow.id, blue.id]


def test_list_alerts_filters_and_pagination(client, headers, make_alert):
    make_alert(level="red", status="pending")
    make_alert(level="yellow", status="resolved")
    make_alert(level="red", status="resolved")

    resp = client.get(f"{API}/alerts?level=red", headers=headers("grid"))
    assert resp.json()["data"]["total"] == 2

    resp = client.get(f"{API}/alerts?status=resolved", headers=headers("grid"))
    assert resp.json()["data"]["total"] == 2

    resp = client.get(f"{API}/alerts?size=2&page=1", headers=headers("grid"))
    data = resp.json()["data"]
    assert data["total"] == 3
    assert data["pages"] == 2
    assert len(data["items"]) == 2

    resp = client.get(f"{API}/alerts?elder_id=999999", headers=headers("grid"))
    assert resp.json()["data"]["total"] == 0


def test_alert_scope_grid_only_sees_own_grid(client, headers, make_alert):
    make_alert()
    assert client.get(f"{API}/alerts", headers=headers("grid")).json()["data"]["total"] == 1
    assert client.get(f"{API}/alerts", headers=headers("grid2")).json()["data"]["total"] == 0
    assert client.get(f"{API}/alerts", headers=headers("street")).json()["data"]["total"] == 1


# ---------------------------------------------------------------- 详情
def test_get_alert_detail_includes_actions(client, db, headers, make_alert):
    alert = make_alert()
    db.add(
        AlertAction(
            alert_id=alert.id,
            action_type="push",
            channel="inapp",
            note="推送网格员",
            action_at=BASE_TIME,
        )
    )
    db.commit()

    resp = client.get(f"{API}/alerts/{alert.id}", headers=headers("grid"))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["id"] == alert.id
    assert data["level"] == "red"
    assert data["level_name"] == "红色预警"
    assert data["rule_code"] is None
    assert len(data["actions"]) == 1
    assert data["actions"][0]["action_type"] == "push"


def test_get_alert_not_found(client, headers):
    resp = client.get(f"{API}/alerts/999999", headers=headers("grid"))
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- 响应 / 处置
def test_ack_records_response_seconds(client, db, headers, make_alert):
    alert = make_alert(created_at=BASE_TIME, pushed_at=BASE_TIME)
    with freeze_time("2026-10-15 09:02:00"):
        resp = client.post(f"{API}/alerts/{alert.id}/ack", headers=headers("grid"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["response_seconds"] == 120
    action = db.query(AlertAction).filter_by(alert_id=alert.id, action_type="ack").one()
    assert action.action_at == datetime(2026, 10, 15, 9, 2, 0)
    assert action.channel == "inapp"


def test_handle_resolved_updates_alert_and_writes_audit(client, db, headers, make_alert, accounts):
    alert = make_alert(created_at=BASE_TIME)
    with freeze_time("2026-10-15 09:05:00"):
        resp = client.post(
            f"{API}/alerts/{alert.id}/handle",
            json={"resolution_type": "resolved", "measure": "上门复测血压并调整用药", "note": "已稳定"},
            headers=headers("grid"),
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "resolved"
    assert data["measure"] == "上门复测血压并调整用药"
    assert data["resolution_note"] == "已稳定"
    assert data["handling_seconds"] == 300
    assert data["resolved_by"] == accounts["users"]["grid"].id
    assert data["resolved_at"] is not None

    db.refresh(alert)
    assert alert.status == "resolved"
    assert db.query(AlertAction).filter_by(alert_id=alert.id, action_type="resolve").count() == 1
    assert db.query(AuditLog).filter_by(entity_type="alert", entity_id=alert.id).count() >= 1


@pytest.mark.parametrize("resolution", ["false_alarm", "referred"])
def test_handle_other_resolution_types(client, headers, make_alert, resolution):
    alert = make_alert()
    resp = client.post(
        f"{API}/alerts/{alert.id}/handle",
        json={"resolution_type": resolution, "note": "转诊或误报"},
        headers=headers("street"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == resolution


def test_handle_invalid_resolution_type_returns_400(client, headers, make_alert):
    alert = make_alert()
    resp = client.post(
        f"{API}/alerts/{alert.id}/handle",
        json={"resolution_type": "closed"},
        headers=headers("grid"),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "RULE_VIOLATION"


def test_handle_closed_alert_conflicts(client, headers, make_alert):
    alert = make_alert(status="resolved")
    resp = client.post(
        f"{API}/alerts/{alert.id}/handle",
        json={"resolution_type": "resolved", "measure": "再处置"},
        headers=headers("grid"),
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "INVALID_TRANSITION"


def test_handle_requires_permission(client, headers, make_alert):
    alert = make_alert()
    resp = client.post(
        f"{API}/alerts/{alert.id}/handle",
        json={"resolution_type": "resolved"},
        headers=headers("elder"),
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------- 规则
def test_rules_defaults_listed(client, headers):
    resp = client.get(f"{API}/alerts/rules", headers=headers("grid"))
    assert resp.status_code == 200, resp.text
    rules = {rule["code"]: rule for rule in resp.json()["data"]}
    assert set(rules) == {"SBP_HIGH", "SBP_LOW", "SPO2_LOW", "HR_CONSEC", "SILENCE"}
    assert rules["HR_CONSEC"]["window_size"] == 3
    # 再次访问不重复写入
    again = client.get(f"{API}/alerts/rules", headers=headers("grid")).json()["data"]
    assert len(again) == 5


def test_create_and_update_rule(client, headers):
    payload = {
        "code": "DBP_HIGH",
        "name": "舒张压过高",
        "rule_type": "threshold",
        "metric_code": "dbp",
        "operator": ">=",
        "threshold": 110,
        "level": "yellow",
        "cooldown_minutes": 20,
        "escalate_minutes": 15,
    }
    resp = client.post(f"{API}/alerts/rules", json=payload, headers=headers("super"))
    assert resp.status_code == 200, resp.text
    rule_id = resp.json()["data"]["id"]
    assert resp.json()["data"]["threshold"] == 110

    duplicated = client.post(f"{API}/alerts/rules", json=payload, headers=headers("super"))
    assert duplicated.status_code == 409

    updated = client.put(
        f"{API}/alerts/rules/{rule_id}",
        json={"threshold": 115, "level": "red"},
        headers=headers("super"),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["threshold"] == 115
    assert updated.json()["data"]["level"] == "red"

    invalid = client.put(
        f"{API}/alerts/rules/{rule_id}", json={"operator": "=="}, headers=headers("super")
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "RULE_VIOLATION"


def test_create_rule_validation_and_permission(client, headers):
    bad_level = client.post(
        f"{API}/alerts/rules",
        json={"code": "X1", "name": "非法级别", "rule_type": "threshold", "metric_code": "sbp",
              "operator": ">=", "threshold": 200, "level": "purple"},
        headers=headers("super"),
    )
    assert bad_level.status_code == 400

    missing_metric = client.post(
        f"{API}/alerts/rules",
        json={"code": "X2", "name": "缺指标", "rule_type": "threshold", "operator": ">=", "threshold": 200},
        headers=headers("super"),
    )
    assert missing_metric.status_code == 400

    forbidden = client.post(
        f"{API}/alerts/rules",
        json={"code": "X3", "name": "无权限", "rule_type": "silence", "silence_hours": 24},
        headers=headers("grid"),
    )
    assert forbidden.status_code == 403


# ---------------------------------------------------------------- 扫描
def test_scan_endpoint_creates_silence_alert(client, headers, accounts):
    with freeze_time("2026-10-17 09:00:00"):
        resp = client.post(f"{API}/alerts/scan", headers=headers("super"))
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["silence_created"] == 1
        assert data["escalated"] == 0
        # 未闭环静默预警不重复生成
        assert client.post(f"{API}/alerts/scan", headers=headers("super")).json()["data"]["silence_created"] == 0

    listing = client.get(f"{API}/alerts?level=blue", headers=headers("street")).json()["data"]
    assert listing["total"] == 1
    assert listing["items"][0]["rule_code"] == "SILENCE"


def test_scan_endpoint_requires_rule_permission(client, headers):
    resp = client.post(f"{API}/alerts/scan", headers=headers("grid"))
    assert resp.status_code == 403


# ---------------------------------------------------------------- 统计
def test_stats_medians_and_false_alarm_rate(client, headers, make_alert):
    make_alert(level="red", status="resolved", response_seconds=60, handling_seconds=120)
    make_alert(level="red", status="resolved", response_seconds=180, handling_seconds=240)
    make_alert(level="yellow", status="false_alarm", response_seconds=120)
    make_alert(level="blue", status="pending", escalated_at=BASE_TIME)

    resp = client.get(f"{API}/alerts/stats", headers=headers("street"))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["red_total"] == 2
    assert data["pending"] == 1
    assert data["closed"] == 3
    assert data["response_median_seconds"] == 120
    assert data["handling_median_seconds"] == 180
    assert data["false_alarm_rate"] == 0.3333
    assert data["escalated"] == 1


def test_stats_scope_for_grid(client, headers, orgs, make_alert, make_elder):
    other = make_elder(grid_org_id=orgs["grid2"].id)
    make_alert()
    make_alert(elder_id=other.id)
    data = client.get(f"{API}/alerts/stats", headers=headers("grid")).json()["data"]
    assert data["total"] == 1
    data = client.get(f"{API}/alerts/stats", headers=headers("street")).json()["data"]
    assert data["total"] == 2


# ---------------------------------------------------------------- 指标 → 预警联动
def test_metric_alert_visible_via_api(client, db, headers, accounts, sms_log):
    with freeze_time(FROZEN):
        metric = HealthMetric(
            elder_id=accounts["elder"].id,
            metric_code="spo2",
            value=88.0,
            unit="%",
            source="visit",
            measured_at=BASE_TIME,
        )
        db.add(metric)
        db.commit()
        db.refresh(metric)
        generated = alert_service.evaluate_metric(db, metric)
    assert len(generated) == 1

    resp = client.get(f"{API}/alerts?level=red", headers=headers("grid"))
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["rule_code"] == "SPO2_LOW"
    assert items[0]["metric_value"] == 88.0
    assert sms_log.exists()
    assert db.query(Notification).filter(Notification.related_id == items[0]["id"]).count() >= 1
