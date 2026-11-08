"""D2 模块单测：预警规则纯函数、默认规则、指标评估、冷却、静默与升级。"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from app.algorithms.alert_rules import is_consecutive, is_silent, match_threshold, rule_matches
from app.models import Alert, AlertAction, AlertRule, HealthMetric, Notification
from app.services import alert_service
from app.tasks import alert_timeout

BASE_TIME = datetime(2026, 10, 15, 9, 0, 0)
FROZEN = "2026-10-15 09:00:00"


@pytest.fixture(autouse=True)
def sms_log(tmp_path, monkeypatch):
    """隔离短信模拟日志，避免污染仓库 data 目录。"""
    path = tmp_path / "data" / "logs" / "sms_sim.log"
    monkeypatch.setattr(alert_service, "sms_log_path", lambda: path)
    return path


def _metric(db, elder, code: str, value: float, measured_at: datetime) -> HealthMetric:
    metric = HealthMetric(
        elder_id=elder.id,
        metric_code=code,
        value=float(value),
        unit=None,
        source="visit",
        measured_at=measured_at,
    )
    db.add(metric)
    db.commit()
    db.refresh(metric)
    return metric


# ---------------------------------------------------------------- 纯函数
def test_match_threshold_operators_and_boundaries():
    assert match_threshold(179, ">=", 180) is False
    assert match_threshold(180, ">=", 180) is True
    assert match_threshold(91, "<=", 90) is False
    assert match_threshold(90, "<=", 90) is True
    assert match_threshold(89, "<=", 90) is True
    assert match_threshold(100, ">", 100) is False
    assert match_threshold(100.1, ">", 100) is True
    assert match_threshold(90, "<", 90) is False
    assert match_threshold(89.9, "<", 90) is True
    assert match_threshold(None, ">=", 180) is False
    # threshold2 视为闭区间上下限
    assert match_threshold(85, ">=", 60, 120) is True
    assert match_threshold(120, ">=", 60, 120) is True
    assert match_threshold(125, ">=", 60, 120) is False


def test_is_consecutive_uses_recent_window():
    assert is_consecutive([101, 102, 103], ">", 100, 3) is True
    assert is_consecutive([101, 99, 103], ">", 100, 3) is False
    assert is_consecutive([101, 102], ">", 100, 3) is False  # 仅 2 次不触发
    assert is_consecutive([120, 101, 102, 103], ">", 100, 3) is True  # 只看最近 3 次
    assert is_consecutive([101, 102, 103], ">", 100, None) is False


def test_is_silent_boundaries():
    assert is_silent(None, BASE_TIME, 48) is True
    assert is_silent(BASE_TIME - timedelta(hours=47.9), BASE_TIME, 48) is False
    assert is_silent(BASE_TIME - timedelta(hours=48), BASE_TIME, 48) is True
    assert is_silent(BASE_TIME - timedelta(hours=48.1), BASE_TIME, 48) is True


def test_rule_matches_dispatches_by_type():
    threshold_rule = SimpleNamespace(rule_type="threshold", operator=">=", threshold=180, threshold2=None)
    consecutive_rule = SimpleNamespace(rule_type="consecutive", operator=">", threshold=100, window_size=3)
    silence_rule = SimpleNamespace(rule_type="silence", silence_hours=48)

    assert rule_matches(threshold_rule, 180) is True
    assert rule_matches(threshold_rule, 179) is False

    # 连续规则：第 2 次不触发，第 3 次触发
    assert rule_matches(consecutive_rule, 103, [101], None, BASE_TIME) is False
    assert rule_matches(consecutive_rule, 103, [101, 102], None, BASE_TIME) is True
    assert rule_matches(consecutive_rule, 99, [101, 102], None, BASE_TIME) is False

    assert rule_matches(silence_rule, None, [], BASE_TIME - timedelta(hours=49), BASE_TIME) is True
    assert rule_matches(silence_rule, None, [], BASE_TIME - timedelta(hours=47), BASE_TIME) is False


# ---------------------------------------------------------------- 默认规则
def test_ensure_default_rules_idempotent(db):
    with freeze_time(FROZEN):
        assert alert_service.ensure_default_rules(db) == 5
        assert alert_service.ensure_default_rules(db) == 0
    rules = {rule.code: rule for rule in db.query(AlertRule).all()}
    assert set(rules) == {"SBP_HIGH", "SBP_LOW", "SPO2_LOW", "HR_CONSEC", "SILENCE"}

    sbp_high = rules["SBP_HIGH"]
    assert sbp_high.level == "red"
    assert sbp_high.operator == ">="
    assert sbp_high.threshold == 180
    assert sbp_high.cooldown_minutes == 30
    assert sbp_high.escalate_minutes == 30

    assert rules["SBP_LOW"].operator == "<=" and rules["SBP_LOW"].threshold == 90
    assert rules["SPO2_LOW"].operator == "<" and rules["SPO2_LOW"].threshold == 90
    assert rules["HR_CONSEC"].level == "yellow" and rules["HR_CONSEC"].window_size == 3
    assert rules["SILENCE"].level == "blue" and rules["SILENCE"].silence_hours == 48


# ---------------------------------------------------------------- 指标评估
@pytest.mark.parametrize(("value", "expected"), [(179, 0), (180, 1)])
def test_sbp_high_boundary(db, accounts, value, expected):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        metric = _metric(db, elder, "sbp", value, BASE_TIME)
        alerts = alert_service.evaluate_metric(db, metric)
    assert len(alerts) == expected
    if alerts:
        alert = alerts[0]
        assert str(alert.level) == "red"
        assert str(alert.status) == "pending"
        assert alert.metric_value == float(value)
        assert alert.pushed_at == BASE_TIME
        assert alert.deadline_at == BASE_TIME + timedelta(minutes=30)


@pytest.mark.parametrize(("value", "expected"), [(91, 0), (90, 1), (89, 1)])
def test_sbp_low_boundary(db, accounts, value, expected):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        metric = _metric(db, elder, "sbp", value, BASE_TIME)
        alerts = alert_service.evaluate_metric(db, metric)
    assert len(alerts) == expected
    if alerts:
        assert alerts[0].metric_value == float(value)


@pytest.mark.parametrize(("value", "expected"), [(90, 0), (89.9, 1)])
def test_spo2_low_boundary(db, accounts, value, expected):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        metric = _metric(db, elder, "spo2", value, BASE_TIME)
        alerts = alert_service.evaluate_metric(db, metric)
    assert len(alerts) == expected


def test_hr_consecutive_second_no_third_triggers(db, accounts):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        first = _metric(db, elder, "hr", 105, BASE_TIME)
        assert alert_service.evaluate_metric(db, first) == []
        second = _metric(db, elder, "hr", 106, BASE_TIME + timedelta(minutes=1))
        assert alert_service.evaluate_metric(db, second) == []
        third = _metric(db, elder, "hr", 107, BASE_TIME + timedelta(minutes=2))
        alerts = alert_service.evaluate_metric(db, third)
    assert len(alerts) == 1
    assert str(alerts[0].level) == "yellow"
    assert alerts[0].metric_code == "hr"


def test_evaluate_metric_cooldown_dedup(db, accounts):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        first = _metric(db, elder, "sbp", 185, BASE_TIME)
        assert len(alert_service.evaluate_metric(db, first)) == 1
    # 冷却期内（29 分钟）同规则不重复
    with freeze_time("2026-10-15 09:29:00"):
        second = _metric(db, elder, "sbp", 186, BASE_TIME + timedelta(minutes=29))
        assert alert_service.evaluate_metric(db, second) == []
    # 超过 30 分钟冷却后可再次触发
    with freeze_time("2026-10-15 09:31:00"):
        third = _metric(db, elder, "sbp", 187, BASE_TIME + timedelta(minutes=31))
        assert len(alert_service.evaluate_metric(db, third)) == 1
    assert db.query(Alert).count() == 2


def test_alert_notifies_grid_family_and_doctor(db, accounts):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        metric = _metric(db, elder, "sbp", 190, BASE_TIME)
        alert_service.evaluate_metric(db, metric)
    notifications = db.query(Notification).all()
    user_ids = {n.user_id for n in notifications}
    assert accounts["users"]["grid"].id in user_ids
    assert accounts["users"]["grid2"].id not in user_ids  # 只通知同网格网格员
    assert accounts["users"]["elder"].id in user_ids  # 家属（绑定账号）
    assert any("签约医生" in n.title for n in notifications)
    assert all(n.related_type == "alert" for n in notifications)
    assert all(n.content for n in notifications)


def test_red_alert_writes_sms_sim_log(db, accounts, sms_log):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        metric = _metric(db, elder, "sbp", 181, BASE_TIME)
        alert_service.evaluate_metric(db, metric)
    assert sms_log.exists()
    content = sms_log.read_text(encoding="utf-8")
    assert "red" in content
    assert elder.name in content
    assert content.count("\n") == 1


def test_yellow_alert_does_not_write_sms_log(db, accounts, sms_log):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        for index, value in enumerate((101, 102, 103)):
            metric = _metric(db, elder, "hr", value, BASE_TIME + timedelta(minutes=index))
            alerts = alert_service.evaluate_metric(db, metric)
    assert len(alerts) == 1
    assert str(alerts[0].level) == "yellow"
    assert not sms_log.exists()


# ---------------------------------------------------------------- 静默 / 升级
def test_scan_silence_boundary_47_9_and_48_1(db, accounts):
    elder = accounts["elder"]
    now = datetime(2026, 10, 17, 9, 0, 0)
    with freeze_time("2026-10-17 09:00:00"):
        _metric(db, elder, "hr", 80, now - timedelta(hours=47.9))
        assert alert_service.scan_alerts(db)["silence_created"] == 0

    later = now + timedelta(minutes=12)  # 距最近上报 48.1 小时
    with freeze_time("2026-10-17 09:12:00"):
        first = alert_service.scan_alerts(db)
        assert first["silence_created"] == 1
        # 已有未闭环静默预警，不重复生成
        assert alert_service.scan_alerts(db)["silence_created"] == 0

    alert = db.query(Alert).filter(Alert.level == "blue").one()
    assert str(alert.status) == "pending"
    assert alert.pushed_at == later
    rule = db.get(AlertRule, alert.rule_id)
    assert rule.code == "SILENCE"


def test_scan_never_reported_elder_is_silent(db, accounts):
    with freeze_time("2026-10-15 09:00:00"):
        result = alert_service.scan_alerts(db)
    assert result["silence_created"] == 1
    alert = db.query(Alert).one()
    assert str(alert.level) == "blue"
    assert "未上报" in (alert.summary or "")


def test_scan_escalates_overdue_alert_with_trace(db, accounts, orgs):
    elder = accounts["elder"]
    with freeze_time(FROZEN):
        metric = _metric(db, elder, "sbp", 185, BASE_TIME)
        alerts = alert_service.evaluate_metric(db, metric)
        alert_id = alerts[0].id

    with freeze_time("2026-10-15 09:31:00"):
        result = alert_service.scan_alerts(db)
    assert result["escalated"] == 1

    alert = db.get(Alert, alert_id)
    db.refresh(alert)
    assert alert.escalated_at == datetime(2026, 10, 15, 9, 31, 0)
    assert alert.escalated_to_org_id == orgs["street"].id
    assert str(alert.status) == "pending"

    actions = db.query(AlertAction).filter_by(alert_id=alert_id, action_type="escalate").all()
    assert len(actions) == 1
    assert "街道" in (actions[0].note or "")
    assert any("预警升级" in n.title for n in db.query(Notification).all())

    with freeze_time("2026-10-15 09:32:00"):
        assert alert_service.scan_alerts(db)["escalated"] == 0


# ---------------------------------------------------------------- 定时任务
def test_run_alert_timeout_scan_handles_error(monkeypatch):
    import app.db.session as session_module

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(session_module, "SessionLocal", _boom)
    result = alert_timeout.run_alert_timeout_scan()
    assert result["error"] == "db down"
    assert result["escalated"] == 0
