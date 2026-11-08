"""D1 健康指标接入 —— 服务层单元测试（内存 SQLite）。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.enums import MetricCode, MetricSource
from app.core.exceptions import Conflict, NotFound, RuleViolation
from app.models import HealthMetric
from app.services import health_service as svc


def test_create_metric_persists_with_defaults(db, accounts):
    """合法指标入库：编码/数值/来源/记录人/测量时间均正确落库。"""
    elder = accounts["elder"]
    nurse = accounts["users"]["nurse"]

    metric, alerts = svc.create_metric(
        db,
        elder_id=elder.id,
        metric_code="sbp",
        value=132.0,
        recorded_by=nurse.id,
    )

    assert isinstance(alerts, list)
    assert metric.id
    assert metric.elder_id == elder.id
    assert metric.metric_code == MetricCode.SBP.value
    assert metric.value == pytest.approx(132.0)
    assert metric.unit == "mmHg"
    assert metric.source == MetricSource.VISIT.value
    assert metric.recorded_by == nurse.id
    assert isinstance(metric.measured_at, datetime)
    assert metric.measured_at.tzinfo is None  # naive datetime

    fetched = db.get(HealthMetric, metric.id)
    assert fetched is not None
    assert fetched.metric_code == "sbp"


def test_create_metric_rejects_invalid_metric_code(db, accounts):
    """非法 metric_code 抛 RULE_VIOLATION。"""
    elder = accounts["elder"]
    with pytest.raises(RuleViolation) as exc_info:
        svc.create_metric(db, elder_id=elder.id, metric_code="bodyfat", value=20)
    assert exc_info.value.code == "RULE_VIOLATION"
    assert exc_info.value.http_status == 422


def test_create_metric_rejects_invalid_source_and_range(db, accounts):
    elder = accounts["elder"]
    with pytest.raises(RuleViolation) as exc_info:
        svc.create_metric(db, elder_id=elder.id, metric_code="hr", value=80, source="manual")
    assert exc_info.value.code == "RULE_VIOLATION"

    with pytest.raises(RuleViolation) as exc_info:
        svc.create_metric(db, elder_id=elder.id, metric_code="spo2", value=130)
    assert exc_info.value.code == "RULE_VIOLATION"


def test_create_metric_elder_not_found(db):
    with pytest.raises(NotFound):
        svc.create_metric(db, elder_id=999999, metric_code="sbp", value=120)


def test_metric_series_daily_aggregation(db, accounts):
    """series 按日聚合：条数、均值、最小值/最大值与 count 正确。"""
    elder = accounts["elder"]
    now = datetime.now().replace(microsecond=0)
    day1 = (now - timedelta(days=2)).replace(hour=8, minute=0, second=0)
    day2 = (now - timedelta(days=1)).replace(hour=9, minute=0, second=0)

    svc.create_metric(db, elder_id=elder.id, metric_code="sbp", value=120, measured_at=day1)
    svc.create_metric(db, elder_id=elder.id, metric_code="sbp", value=140, measured_at=day1 + timedelta(hours=1))
    svc.create_metric(db, elder_id=elder.id, metric_code="sbp", value=100, measured_at=day2)
    # 干扰项：其他指标不参与聚合
    svc.create_metric(db, elder_id=elder.id, metric_code="hr", value=66, measured_at=day1)

    series = svc.metric_series(db, elder_id=elder.id, metric_code="sbp", days=30)

    assert len(series) == 2
    first, second = series[0], series[1]
    assert first["date"] < second["date"]
    assert first["count"] == 2
    assert first["avg"] == pytest.approx(130.0)
    assert first["min"] == pytest.approx(120.0)
    assert first["max"] == pytest.approx(140.0)
    assert second["count"] == 1
    assert second["avg"] == pytest.approx(100.0)


def test_list_metrics_desc_and_filter(db, accounts, make_elder):
    """列表按测量时间倒序，支持指标编码过滤与分页。"""
    elder = accounts["elder"]
    base = datetime(2026, 10, 1, 8, 0, 0)
    for i in range(3):
        svc.create_metric(
            db, elder_id=elder.id, metric_code="sbp", value=120 + i, measured_at=base + timedelta(days=i)
        )
    svc.create_metric(db, elder_id=elder.id, metric_code="hr", value=70, measured_at=base)

    items, total = svc.list_metrics(db, elder_id=elder.id, metric_code="sbp", page=1, size=2)
    assert total == 3
    assert len(items) == 2
    assert items[0].measured_at > items[1].measured_at
    assert all(item.metric_code == "sbp" for item in items)

    other = make_elder()
    other_items, other_total = svc.list_metrics(db, elder_id=other.id)
    assert other_total == 0
    assert other_items == []


def test_batch_partial_failure_details(db, accounts):
    """批量部分失败：成功条数、失败明细 index/reason、落库条数均正确。"""
    elder = accounts["elder"]
    items = [
        {"elder_id": elder.id, "metric_code": "sbp", "value": 130},
        {"elder_id": elder.id, "metric_code": "bodyfat", "value": 1},   # 非法编码
        {"elder_id": 999999, "metric_code": "hr", "value": 80},         # 老人不存在
        {"elder_id": elder.id, "metric_code": "spo2", "value": 97},
    ]

    result = svc.create_metrics_batch(db, items, operator_id=accounts["users"]["nurse"].id)

    assert result["success"] == 2
    assert result["alerts_count"] >= 0
    failed = result["failed"]
    assert [item["index"] for item in failed] == [1, 2]
    assert all(item["reason"] for item in failed)
    assert "指标编码" in failed[0]["reason"]

    stored = db.query(HealthMetric).filter(HealthMetric.elder_id == elder.id).all()
    assert len(stored) == 2
    assert {row.metric_code for row in stored} == {"sbp", "spo2"}


def test_bind_device_unique_code(db, accounts):
    """设备绑定成功；device_code 重复抛 CONFLICT。"""
    elder = accounts["elder"]
    device = svc.bind_device(db, elder_id=elder.id, device_code="BAND-0001")
    assert device.id
    assert device.device_code == "BAND-0001"
    assert device.status == "online"

    with pytest.raises(Conflict) as exc_info:
        svc.bind_device(db, elder_id=elder.id, device_code="BAND-0001")
    assert exc_info.value.code == "CONFLICT"


def test_list_devices_scope_filter(db, accounts):
    """设备列表按数据范围裁剪，支持 elder_id 过滤。"""
    elder = accounts["elder"]
    svc.bind_device(db, elder_id=elder.id, device_code="BAND-0002", device_type="watch")

    grid = accounts["users"]["grid"]
    items, total = svc.list_devices(db, user=grid, elder_id=elder.id)
    assert total == 1
    assert items[0].device_type == "watch"

    grid2 = accounts["users"]["grid2"]
    items2, total2 = svc.list_devices(db, user=grid2)
    assert total2 == 0
    assert items2 == []
