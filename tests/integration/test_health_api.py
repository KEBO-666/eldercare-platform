"""D1 健康数据接入 —— API 集成测试（TestClient + 内存库）。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models import Device, HealthMetric

BASE_URL = "/api/v1/health"


def _seed_metrics(db, elder_id: int, count: int = 3) -> list[HealthMetric]:
    base = datetime(2026, 10, 10, 8, 0, 0)
    rows = [
        HealthMetric(
            elder_id=elder_id,
            metric_code="sbp",
            value=120 + i,
            unit="mmHg",
            source="visit",
            measured_at=base + timedelta(days=i),
        )
        for i in range(count)
    ]
    db.add_all(rows)
    db.commit()
    return rows


def test_create_metric_api_ok(client, headers, accounts, db):
    """合法指标：200 + code=0 + metric_id，且已入库、recorded_by 为当前用户。"""
    elder = accounts["elder"]
    operator = accounts["users"]["super"]
    resp = client.post(
        f"{BASE_URL}/metrics",
        headers=headers("super"),
        json={
            "elder_id": elder.id,
            "metric_code": "sbp",
            "value": 138,
            "measured_at": "2026-10-15T09:30:00",
            "source": "visit",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["metric_id"]
    assert isinstance(data["alerts"], list)

    metric = db.get(HealthMetric, data["metric_id"])
    assert metric is not None
    assert metric.value == pytest.approx(138.0)
    assert metric.metric_code == "sbp"
    assert metric.recorded_by == operator.id
    assert metric.measured_at == datetime(2026, 10, 15, 9, 30, 0)


def test_create_metric_invalid_code_api(client, headers, accounts):
    """非法 metric_code → HTTP 422 且错误码 RULE_VIOLATION。"""
    elder = accounts["elder"]
    resp = client.post(
        f"{BASE_URL}/metrics",
        headers=headers("super"),
        json={"elder_id": elder.id, "metric_code": "bodyfat", "value": 20},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "RULE_VIOLATION"
    assert "指标编码" in body["message"]


def test_create_metric_requires_write_permission(client, headers, accounts):
    """网格员没有 health:write 权限 → 403 FORBIDDEN。"""
    elder = accounts["elder"]
    resp = client.post(
        f"{BASE_URL}/metrics",
        headers=headers("grid"),
        json={"elder_id": elder.id, "metric_code": "sbp", "value": 130},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "FORBIDDEN"


def test_list_metrics_paged_desc_api(client, headers, accounts, db):
    """分页查询：total/pages 正确，按测量时间倒序。"""
    elder = accounts["elder"]
    _seed_metrics(db, elder.id, count=3)

    resp = client.get(
        f"{BASE_URL}/elders/{elder.id}/metrics",
        headers=headers("grid"),
        params={"metric_code": "sbp", "page": 1, "size": 2},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 3
    assert data["pages"] == 2
    assert data["page"] == 1 and data["size"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["measured_at"] >= data["items"][1]["measured_at"]

    # 时间范围过滤
    resp2 = client.get(
        f"{BASE_URL}/elders/{elder.id}/metrics",
        headers=headers("grid"),
        params={"from_time": "2026-10-11T00:00:00", "to_time": "2026-10-12T23:59:59"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["data"]["total"] == 2


def test_metrics_cross_grid_forbidden(client, headers, accounts, db):
    """跨网格访问：grid2 访问 grid1 老人 → 403 FORBIDDEN。"""
    elder = accounts["elder"]
    _seed_metrics(db, elder.id, count=1)

    resp = client.get(f"{BASE_URL}/elders/{elder.id}/metrics", headers=headers("grid2"))
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "FORBIDDEN"

    resp2 = client.get(f"{BASE_URL}/elders/{elder.id}/series", headers=headers("grid2"))
    assert resp2.status_code == 403
    assert resp2.json()["code"] == "FORBIDDEN"


def test_series_api_daily_aggregation(client, headers, accounts):
    """series：按日聚合条数与均值。"""
    elder = accounts["elder"]
    now = datetime.now().replace(microsecond=0)
    day1 = (now - timedelta(days=2)).replace(hour=8, minute=0, second=0)
    day2 = (now - timedelta(days=1)).replace(hour=9, minute=0, second=0)

    for payload in (
        {"measured_at": day1.isoformat(), "value": 120},
        {"measured_at": (day1 + timedelta(hours=2)).isoformat(), "value": 140},
        {"measured_at": day2.isoformat(), "value": 100},
    ):
        resp = client.post(
            f"{BASE_URL}/metrics",
            headers=headers("super"),
            json={"elder_id": elder.id, "metric_code": "sbp", **payload},
        )
        assert resp.status_code == 200, resp.text

    resp = client.get(
        f"{BASE_URL}/elders/{elder.id}/series",
        headers=headers("super"),
        params={"metric_code": "sbp", "days": 30},
    )
    assert resp.status_code == 200, resp.text
    series = resp.json()["data"]
    assert isinstance(series, list)
    assert len(series) == 2
    first = series[0]
    assert first["count"] == 2
    assert first["avg"] == pytest.approx(130.0)
    assert first["min"] == pytest.approx(120.0)
    assert first["max"] == pytest.approx(140.0)
    assert series[1]["avg"] == pytest.approx(100.0)


def test_batch_api_partial_failure(client, headers, accounts, db):
    """批量录入：部分成功，返回 success/failed 明细/alerts_count。"""
    elder = accounts["elder"]
    resp = client.post(
        f"{BASE_URL}/metrics/batch",
        headers=headers("super"),
        json={
            "items": [
                {"elder_id": elder.id, "metric_code": "sbp", "value": 130},
                {"elder_id": elder.id, "metric_code": "bodyfat", "value": 1},
                {"elder_id": elder.id, "metric_code": "hr", "value": 78},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["success"] == 2
    assert data["alerts_count"] >= 0
    assert len(data["failed"]) == 1
    assert data["failed"][0]["index"] == 1
    assert data["failed"][0]["reason"]

    stored = db.query(HealthMetric).filter(HealthMetric.elder_id == elder.id).all()
    assert {row.metric_code for row in stored} == {"sbp", "hr"}


def test_batch_api_empty_items_rejected(client, headers, accounts):
    """空 items 视为参数错误（RULE_VIOLATION）。"""
    resp = client.post(f"{BASE_URL}/metrics/batch", headers=headers("nurse"), json={"items": []})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "RULE_VIOLATION"


def test_devices_bind_and_list(client, headers, accounts, db):
    """设备绑定成功；重复绑定 409；列表支持 elder_id 过滤与数据范围。"""
    elder = accounts["elder"]
    resp = client.post(
        f"{BASE_URL}/devices",
        headers=headers("super"),
        json={"elder_id": elder.id, "device_code": "BAND-8801", "device_type": "band"},
    )
    assert resp.status_code == 200, resp.text
    device_id = resp.json()["data"]["id"]
    assert device_id

    dup = client.post(
        f"{BASE_URL}/devices",
        headers=headers("super"),
        json={"elder_id": elder.id, "device_code": "BAND-8801"},
    )
    assert dup.status_code == 409, dup.text
    assert dup.json()["code"] == "CONFLICT"

    listed = client.get(
        f"{BASE_URL}/devices",
        headers=headers("grid"),
        params={"elder_id": elder.id},
    )
    assert listed.status_code == 200, listed.text
    data = listed.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["device_code"] == "BAND-8801"

    # 跨网格不可见
    empty = client.get(f"{BASE_URL}/devices", headers=headers("grid2"))
    assert empty.status_code == 200
    assert empty.json()["data"]["total"] == 0
    assert db.query(Device).count() == 1
