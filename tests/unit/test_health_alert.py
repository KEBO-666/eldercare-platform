"""D 模块验收单测：健康数据接入 + 三级预警规则引擎（边界矩阵）。

覆盖任务书要求的核心边界：
- 阈值：收缩压 179 不触发 / 180 触发红色；91 不触发 / 90、89 触发红色；血氧 <90 触发；
- 连续：心率连续 2 次不触发、第 3 次触发黄色；窗口外的旧数据不参与；
- 静默：47.9 小时不触发、48.1 小时触发蓝色（freezegun）；
- 冷却：30 分钟内同规则重复指标只生成 1 条预警；
- 升级：红色 31 分钟后 scan → escalated_at 非空且写 escalate 动作 + 推送街道；
- 闭环：非法 resolution_type 被拒；handle 后状态正确且 handling_seconds>0；
- 推送：红色写 Notification（网格员/家属/签约医生）与 data/logs/sms_sim.log；
- 数据范围：跨网格列表为空、详情不可见；
- 契约字段：/health/metrics 返回 metric + alerts、/metrics/batch 返回 created_alerts、
  /alerts/scan 返回 silenced + escalated、metrics 列表支持 from/to 别名。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time

from app.core.exceptions import NotFound, RuleViolation
from app.models import Alert, AlertAction, AlertRule, AuditLog, Notification
from app.services import alert_service, health_service

FROZEN = "2026-10-15 09:00:00"
BASE_TIME = datetime(2026, 10, 15, 9, 0, 0)


# ---------------------------------------------------------------- 夹具
@pytest.fixture(autouse=True)
def sms_log(tmp_path, monkeypatch):
    """把模拟短信日志重定向到临时目录，避免污染 data/logs。"""
    path = tmp_path / "data" / "logs" / "sms_sim.log"
    monkeypatch.setattr(alert_service, "sms_log_path", lambda: path)
    return path


def _record(db, elder_id: int, metric_code: str, value: float, measured_at: datetime | None = None):
    """通过健康服务写入指标（内部会调用 evaluate_metric），返回新生成的预警简要列表。"""
    _, alerts = health_service.create_metric(
        db,
        elder_id=elder_id,
        metric_code=metric_code,
        value=value,
        measured_at=measured_at,
    )
    return alerts


def _make_alert(db, elder_id: int, **kwargs) -> Alert:
    created = kwargs.pop("created_at", BASE_TIME)
    defaults = dict(
        elder_id=elder_id,
        level="red",
        status="pending",
        metric_code="sbp",
        metric_value=185.0,
        summary="测试预警",
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


# ---------------------------------------------------------------- 阈值边界
def test_sbp_high_boundary_179_not_triggered_180_red(db, accounts):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        assert _record(db, elder.id, "sbp", 179) == []
    with freeze_time("2026-10-15 09:10:00"):
        alerts = _record(db, elder.id, "sbp", 180)
    assert len(alerts) == 1
    assert alerts[0]["level"] == "red"
    assert alerts[0]["status"] == "pending"
    alert = db.get(Alert, alerts[0]["id"])
    assert alert.rule_id is not None
    assert str(db.get(AlertRule, alert.rule_id).code) == "SBP_HIGH"
    assert alert.metric_value == pytest.approx(180.0)
    assert alert.deadline_at == alert.created_at + timedelta(minutes=30)


def test_sbp_low_boundary_91_ok_90_and_89_red(db, accounts):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        assert _record(db, elder.id, "sbp", 91) == []
    with freeze_time("2026-10-15 09:05:00"):
        boundary = _record(db, elder.id, "sbp", 90)
    assert len(boundary) == 1 and boundary[0]["level"] == "red"
    # 冷却 30 分钟内同规则不重复；超过冷却后 89 再次触发
    with freeze_time("2026-10-15 09:20:00"):
        assert _record(db, elder.id, "sbp", 89) == []
    with freeze_time("2026-10-15 09:40:00"):
        below = _record(db, elder.id, "sbp", 89)
    assert len(below) == 1 and below[0]["level"] == "red"


def test_spo2_low_triggers_red_and_pushes(db, accounts, sms_log):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        alerts = _record(db, elder.id, "spo2", 88)
    assert len(alerts) == 1 and alerts[0]["level"] == "red"
    alert = db.get(Alert, alerts[0]["id"])
    assert alert.pushed_at is not None

    # 站内通知：网格员 + 家属；签约医生走短信通道（Notification.user_id 为空）
    notifications = db.query(Notification).filter(Notification.related_id == alert.id).all()
    notified_users = {item.user_id for item in notifications if item.user_id is not None}
    assert accounts["users"]["grid"].id in notified_users
    assert accounts["users"]["elder"].id in notified_users
    assert any(item.channel == "sms" for item in notifications)

    # 推送流水 + 模拟短信日志
    assert db.query(AlertAction).filter_by(alert_id=alert.id, action_type="push").count() >= 1
    assert sms_log.exists()
    assert f"alert_id={alert.id}" in sms_log.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 连续异常
def test_hr_consecutive_third_time_triggers_yellow(db, accounts):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        assert _record(db, elder.id, "hr", 105) == []
    with freeze_time("2026-10-15 09:05:00"):
        assert _record(db, elder.id, "hr", 108) == []
    with freeze_time("2026-10-15 09:10:00"):
        alerts = _record(db, elder.id, "hr", 112)
    assert len(alerts) == 1
    assert alerts[0]["level"] == "yellow"
    alert = db.get(Alert, alerts[0]["id"])
    assert str(db.get(AlertRule, alert.rule_id).code) == "HR_CONSEC"
    assert alert.detail["window_size"] == 3
    assert alert.detail["recent_values"] == pytest.approx([105.0, 108.0])


def test_hr_consecutive_ignores_out_of_window_history(db, accounts):
    """窗口外的旧数据不参与：高-高-正常-高-高 始终未凑满连续 3 次，不触发。"""
    elder = accounts["elder"]
    sequence = [
        ("09:00", 105),
        ("09:05", 106),
        ("09:10", 75),
        ("09:15", 108),
        ("09:20", 110),
    ]
    generated = []
    for clock, value in sequence:
        with freeze_time(f"2026-10-15 {clock}:00"):
            generated.extend(_record(db, elder.id, "hr", value))
    assert generated == []
    assert db.query(Alert).count() == 0


# ---------------------------------------------------------------- 静默超时
def test_silence_boundary_47_9_hours_ok_48_1_hours_blue(db, accounts):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        _record(db, elder.id, "hr", 80)

    with freeze_time("2026-10-17 08:54:00"):  # 距最近上报 47.9 小时
        result = alert_service.scan_alerts(db)
    assert result["silenced"] == 0
    assert result["escalated"] == 0

    with freeze_time("2026-10-17 09:12:00"):  # 距最近上报 48.2 小时
        result = alert_service.scan_alerts(db)
    assert result["silenced"] == 1

    blue = db.query(Alert).filter(Alert.level == "blue").all()
    assert len(blue) == 1
    assert blue[0].rule_id is not None
    assert str(db.get(AlertRule, blue[0].rule_id).code) == "SILENCE"
    # 未闭环静默预警不重复生成
    with freeze_time("2026-10-17 09:20:00"):
        assert alert_service.scan_alerts(db)["silenced"] == 0


# ---------------------------------------------------------------- 冷却去重
def test_cooldown_suppresses_duplicate_within_30_minutes(db, accounts):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        assert len(_record(db, elder.id, "sbp", 200)) == 1
    with freeze_time("2026-10-15 09:20:00"):
        assert _record(db, elder.id, "sbp", 200) == []
    with freeze_time("2026-10-15 09:31:00"):  # 超过 30 分钟冷却，重新生成
        assert len(_record(db, elder.id, "sbp", 200)) == 1
    assert db.query(Alert).count() == 2


# ---------------------------------------------------------------- 超时升级
def test_red_alert_escalates_after_31_minutes(db, accounts, orgs):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        alerts = _record(db, elder.id, "spo2", 87)
    assert len(alerts) == 1
    alert = db.get(Alert, alerts[0]["id"])
    assert alert.escalated_at is None

    with freeze_time("2026-10-15 09:31:00"):
        result = alert_service.scan_alerts(db)
    assert result["escalated"] == 1
    assert result["silenced"] == 0

    db.refresh(alert)
    assert alert.escalated_at is not None
    assert alert.escalated_to_org_id == orgs["street"].id
    assert db.query(AlertAction).filter_by(alert_id=alert.id, action_type="escalate").count() == 1
    # 街道监管收到升级通知
    titles = [n.title or "" for n in db.query(Notification).filter(Notification.related_id == alert.id)]
    assert any("升级" in title for title in titles)


# ---------------------------------------------------------------- 闭环处置
def test_handle_rejects_invalid_resolution_and_closes_with_duration(db, accounts):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        alerts = _record(db, elder.id, "spo2", 88)
    alert = db.get(Alert, alerts[0]["id"])
    grid = accounts["users"]["grid"]

    with freeze_time("2026-10-15 09:05:00"):
        with pytest.raises(RuleViolation) as exc_info:
            alert_service.handle_alert(db, grid, alert.id, resolution_type="closed")
        assert exc_info.value.code == "RULE_VIOLATION"
        # 非法类型不得改变状态
        db.refresh(alert)
        assert str(alert.status) == "pending"

        data = alert_service.handle_alert(
            db,
            grid,
            alert.id,
            resolution_type="resolved",
            measure="上门复测血氧并吸氧",
            note="已恢复正常",
        )
    assert data["status"] == "resolved"
    assert data["resolved_by"] == grid.id
    assert data["handling_seconds"] == 300
    assert data["measure"] == "上门复测血氧并吸氧"

    db.refresh(alert)
    assert alert.resolved_at is not None
    assert alert.handling_seconds == 300
    assert db.query(AlertAction).filter_by(alert_id=alert.id, action_type="resolve").count() == 1
    assert db.query(AuditLog).filter_by(entity_type="alert", entity_id=alert.id).count() >= 1


@pytest.mark.parametrize(("code", "status", "action"), [
    ("false_alarm", "false_alarm", "false_alarm"),
    ("referred", "referred", "refer"),
])
def test_handle_other_resolution_types(db, accounts, code, status, action):
    elder = accounts["elder"]
    alert = _make_alert(db, elder.id)
    data = alert_service.handle_alert(db, accounts["users"]["grid"], alert.id, resolution_type=code, measure="已核实并记录处置结果")
    assert data["status"] == status
    db.refresh(alert)
    assert str(alert.status) == status


def test_ack_records_response_seconds(db, accounts):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        alerts = _record(db, elder.id, "spo2", 88)
    alert = db.get(Alert, alerts[0]["id"])

    with freeze_time("2026-10-15 09:02:00"):
        data = alert_service.ack_alert(db, accounts["users"]["grid"], alert.id)
    assert data["response_seconds"] == 120
    assert db.query(AlertAction).filter_by(alert_id=alert.id, action_type="ack").count() == 1

    # 已闭环预警不可再确认
    alert_service.handle_alert(db, accounts["users"]["grid"], alert.id, resolution_type="resolved", measure="已完成电话核实与处置")
    with pytest.raises(Exception) as exc_info:
        alert_service.ack_alert(db, accounts["users"]["grid"], alert.id)
    assert getattr(exc_info.value, "code", "") == "CONFLICT"


# ---------------------------------------------------------------- 统计 / 数据范围
def test_alert_stats_medians_and_false_alarm_rate(db, accounts):
    elder = accounts["elder"]
    _make_alert(db, elder.id, level="red", status="resolved", response_seconds=60, handling_seconds=120)
    _make_alert(db, elder.id, level="red", status="resolved", response_seconds=180, handling_seconds=240)
    _make_alert(db, elder.id, level="yellow", status="false_alarm", response_seconds=120)
    _make_alert(db, elder.id, level="blue", status="pending", escalated_at=BASE_TIME)

    stats = alert_service.alert_stats(db, accounts["users"]["street"])
    assert stats["red_total"] == 2
    assert stats["pending"] == 1
    assert stats["closed"] == 3
    assert stats["response_median_seconds"] == pytest.approx(120.0)
    assert stats["handling_median_seconds"] == pytest.approx(180.0)
    assert stats["false_alarm_rate"] == pytest.approx(0.3333)
    assert stats["escalated"] == 1


def test_alert_scope_cross_grid_hidden(db, accounts, orgs, make_elder):
    own = _make_alert(db, accounts["elder"].id, level="red", summary="本网格")
    other_elder = make_elder(grid_org_id=orgs["grid2"].id)
    other = _make_alert(db, other_elder.id, level="red", summary="他网格")

    items, total = alert_service.list_alerts(db, accounts["users"]["grid"])
    assert total == 1 and items[0]["id"] == own.id
    items2, total2 = alert_service.list_alerts(db, accounts["users"]["grid2"])
    assert total2 == 1 and items2[0]["id"] == other.id
    _, total3 = alert_service.list_alerts(db, accounts["users"]["street"])
    assert total3 == 2

    with pytest.raises(NotFound):
        alert_service.get_alert(db, accounts["users"]["grid"], other.id)


# ---------------------------------------------------------------- 默认规则幂等
def test_ensure_default_rules_idempotent(db):
    assert alert_service.ensure_default_rules(db) == 5
    assert alert_service.ensure_default_rules(db) == 0
    rules = {rule.code: rule for rule in db.query(AlertRule).all()}
    assert set(rules) == {"SBP_HIGH", "SBP_LOW", "SPO2_LOW", "HR_CONSEC", "SILENCE"}
    assert str(rules["SBP_HIGH"].level) == "red" and rules["SBP_HIGH"].threshold == pytest.approx(180.0)
    assert str(rules["SBP_LOW"].level) == "red" and rules["SBP_LOW"].threshold == pytest.approx(90.0)
    assert str(rules["SPO2_LOW"].level) == "red" and rules["SPO2_LOW"].threshold == pytest.approx(90.0)
    assert str(rules["HR_CONSEC"].level) == "yellow" and rules["HR_CONSEC"].window_size == 3
    assert str(rules["SILENCE"].level) == "blue" and rules["SILENCE"].silence_hours == 48


# ---------------------------------------------------------------- 契约字段（接口层）
def test_api_contract_aliases(client, headers, accounts):
    elder = accounts["elder"]
    created = client.post(
        "/api/v1/health/metrics",
        headers=headers("super"),
        json={"elder_id": elder.id, "metric_code": "sbp", "value": 120},
    )
    assert created.status_code == 200, created.text
    data = created.json()["data"]
    assert data["metric"]["id"] == data["metric_id"]
    assert data["metric"]["metric_code"] == "sbp"
    assert data["alerts"] == []

    batch = client.post(
        "/api/v1/health/metrics/batch",
        headers=headers("super"),
        json={"items": [{"elder_id": elder.id, "metric_code": "hr", "value": 72}]},
    )
    assert batch.status_code == 200, batch.text
    batch_data = batch.json()["data"]
    assert batch_data["success"] == 1
    assert batch_data["created_alerts"] == batch_data["alerts_count"] == 0

    # 时间范围同时支持 from/to 别名与 from_time/to_time
    for params in (
        {"from": "2026-01-01T00:00:00", "to": "2026-12-31T23:59:59"},
        {"from_time": "2026-01-01T00:00:00", "to_time": "2026-12-31T23:59:59"},
    ):
        resp = client.get(
            f"/api/v1/health/elders/{elder.id}/metrics",
            headers=headers("super"),
            params=params,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["total"] == 2

    scan = client.post("/api/v1/alerts/scan", headers=headers("super"))
    assert scan.status_code == 200, scan.text
    scan_data = scan.json()["data"]
    assert scan_data["silenced"] == 0
    assert "escalated" in scan_data
