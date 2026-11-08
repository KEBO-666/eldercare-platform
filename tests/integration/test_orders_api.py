"""C 模块集成测试：工单执行接口（签到距离 / 照片 / 签退 / 异常处置 / 数据范围）。"""
from __future__ import annotations

import io
import math
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image

from app.core.config import settings
from app.core.enums import OrderStatus
from app.db.base import utcnow
from app.models import (
    HealthMetric,
    Notification,
    WorkOrder,
    WorkOrderPhoto,
    WorkOrderTrack,
)
from app.services import order_service

BASE_TIME = datetime(2026, 10, 15, 9, 0, 0)
API = "/api/v1"


def _offset_lat(lat: float, meters: float) -> float:
    return lat + math.degrees(meters / order_service.EARTH_RADIUS_M)


def _png_bytes(width: int = 2400, height: int = 1600) -> bytes:
    image = Image.new("RGB", (width, height), (150, 80, 60))
    pixels = image.load()
    for x in range(0, width, 6):
        for y in range(0, height, 6):
            pixels[x, y] = ((x * 3) % 255, (y * 5) % 255, (x + y) % 255)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _resolve_path(stored: str) -> Path:
    path = Path(stored)
    return path if path.is_absolute() else settings.abs_path(stored)


@pytest.fixture()
def make_order(db, accounts):
    counter = {"n": 0}

    def _make(**kwargs) -> WorkOrder:
        counter["n"] += 1
        defaults = dict(
            code=f"AO{counter['n']:04d}",
            elder_id=accounts["elder"].id,
            service_item_id=accounts["item"].id,
            nurse_id=accounts["staff"].id,
            status=OrderStatus.PENDING_ACCEPT,
            scheduled_start=BASE_TIME,
            price_version_id=accounts["price"].id,
            unit_price=Decimal("120.00"),
            quantity=1,
            amount=Decimal("120.00"),
            community_org_id=accounts["elder"].community_org_id,
        )
        defaults.update(kwargs)
        order = WorkOrder(**defaults)
        db.add(order)
        db.flush()
        return order

    return _make


# ---------------------------------------------------------------- 接单 / 签到
def test_accept_and_checkin_flow(client, db, headers, accounts, make_order):
    order = make_order()
    resp = client.post(f"{API}/orders/{order.id}/accept", headers=headers("nurse"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["code"] == 0
    assert resp.json()["data"]["status"] == OrderStatus.ACCEPTED
    assert resp.json()["data"]["accepted_at"] is not None

    elder = accounts["elder"]
    lat = _offset_lat(elder.lat, 199)
    resp = client.post(
        f"{API}/orders/{order.id}/checkin",
        json={"lat": lat, "lng": elder.lng},
        headers=headers("nurse"),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == OrderStatus.IN_SERVICE
    assert data["distance_m"] == pytest.approx(199, abs=1)
    assert len(data["tracks"]) == 1
    assert data["tracks"][0]["kind"] == "checkin"

    detail = client.get(f"{API}/orders/{order.id}", headers=headers("nurse")).json()["data"]
    assert detail["status"] == OrderStatus.IN_SERVICE
    assert detail["checkin_distance_m"] == pytest.approx(199, abs=1)


def test_checkin_over_200_meters_without_note_returns_rule_violation(
    client, db, headers, accounts, make_order
):
    order = make_order(status=OrderStatus.ACCEPTED)
    elder = accounts["elder"]
    resp = client.post(
        f"{API}/orders/{order.id}/checkin",
        json={"lat": _offset_lat(elder.lat, 201), "lng": elder.lng},
        headers=headers("nurse"),
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "RULE_VIOLATION"
    assert "200 米" in body["message"]
    db.refresh(order)
    assert order.status == OrderStatus.ACCEPTED
    assert db.query(WorkOrderTrack).filter(WorkOrderTrack.work_order_id == order.id).count() == 0


def test_checkin_over_200_meters_with_note_succeeds(client, db, headers, accounts, make_order):
    order = make_order(status=OrderStatus.ACCEPTED)
    elder = accounts["elder"]
    resp = client.post(
        f"{API}/orders/{order.id}/checkin",
        json={"lat": _offset_lat(elder.lat, 201), "lng": elder.lng, "note": "定位漂移，已与家属确认"},
        headers=headers("nurse"),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == OrderStatus.IN_SERVICE
    assert data["checkin_note"] == "定位漂移，已与家属确认"
    assert data["checkin_distance_m"] > 200


def test_checkin_requires_order_status(client, db, headers, accounts, make_order):
    order = make_order()  # pending_accept
    elder = accounts["elder"]
    resp = client.post(
        f"{API}/orders/{order.id}/checkin",
        json={"lat": elder.lat, "lng": elder.lng},
        headers=headers("nurse"),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "INVALID_TRANSITION"


def test_cannot_execute_other_nurse_order(client, db, headers, make_order, make_staff):
    other = make_staff()
    order = make_order(nurse_id=other.id)
    resp = client.post(f"{API}/orders/{order.id}/accept", headers=headers("nurse"))
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "FORBIDDEN"


# ---------------------------------------------------------------- 逐项勾选
def test_items_requires_in_service(client, db, headers, accounts, make_order):
    order = make_order(status=OrderStatus.ACCEPTED)
    payload = {"items": [{"name": "测量血压", "completed": True}]}
    resp = client.post(f"{API}/orders/{order.id}/items", json=payload, headers=headers("nurse"))
    assert resp.status_code == 409, resp.text

    elder = accounts["elder"]
    client.post(
        f"{API}/orders/{order.id}/checkin",
        json={"lat": elder.lat, "lng": elder.lng},
        headers=headers("nurse"),
    )
    resp = client.post(
        f"{API}/orders/{order.id}/items",
        json={"items": [{"name": "测量血压", "completed": True}, {"name": "协助服药", "completed": True}]},
        headers=headers("nurse"),
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]["items"]) == 2
    assert all(item["completed"] for item in resp.json()["data"]["items"])


# ---------------------------------------------------------------- 照片
def test_photo_upload_compresses_and_limits(client, db, headers, make_order, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"), raising=False)
    order = make_order(status=OrderStatus.ACCEPTED)
    raw = _png_bytes()
    for index in range(settings.photo_limit):
        resp = client.post(
            f"{API}/orders/{order.id}/photos",
            files={"file": (f"p{index}.png", raw, "image/png")},
            headers=headers("nurse"),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["count"] == index + 1
        assert max(data["width"], data["height"]) <= 1280

    resp = client.post(
        f"{API}/orders/{order.id}/photos",
        files={"file": ("p4.png", raw, "image/png")},
        headers=headers("nurse"),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "RULE_VIOLATION"
    assert "最多 3 张" in resp.json()["message"]

    photos = db.query(WorkOrderPhoto).filter(WorkOrderPhoto.work_order_id == order.id).all()
    assert len(photos) == settings.photo_limit
    for photo in photos:
        path = _resolve_path(photo.path)
        assert path.exists(), f"照片文件未落盘：{path}"
        assert 0 < photo.size_kb <= settings.photo_max_kb
        with Image.open(path) as image:
            assert max(image.size) <= 1280
            assert image.format == "JPEG"


def test_photo_rejects_non_image(client, db, headers, make_order):
    order = make_order(status=OrderStatus.ACCEPTED)
    resp = client.post(
        f"{API}/orders/{order.id}/photos",
        files={"file": ("fake.png", b"not-an-image", "image/png")},
        headers=headers("nurse"),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "RULE_VIOLATION"


# ---------------------------------------------------------------- 签退
def test_checkout_before_checkin_returns_409(client, db, headers, accounts, make_order):
    order = make_order(status=OrderStatus.ACCEPTED)
    resp = client.post(
        f"{API}/orders/{order.id}/checkout", json={"summary": "直接签退"}, headers=headers("nurse")
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "INVALID_TRANSITION"


def test_checkout_duration_anomaly_boundary(client, db, headers, accounts, make_order):
    normal = make_order(status=OrderStatus.IN_SERVICE, checkin_at=utcnow() - timedelta(minutes=89))
    resp = client.post(
        f"{API}/orders/{normal.id}/checkout", json={"summary": "正常完成"}, headers=headers("nurse")
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["duration_min"] == 89
    assert data["deviation_min"] == 29
    assert data["duration_anomaly"] is False
    assert data["status"] == OrderStatus.PENDING_EVALUATION

    abnormal = make_order(status=OrderStatus.IN_SERVICE, checkin_at=utcnow() - timedelta(minutes=91))
    resp = client.post(
        f"{API}/orders/{abnormal.id}/checkout",
        json={
            "summary": "服务完成，测量血压偏高",
            "metrics": [{"metric_code": "sbp", "value": 156}, {"metric_code": "hr", "value": 88}],
        },
        headers=headers("nurse"),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["duration_min"] == 91
    assert data["deviation_min"] == 31
    assert data["duration_anomaly"] is True
    assert len(data["metrics_recorded"]) == 2
    metrics = db.query(HealthMetric).filter(HealthMetric.work_order_id == abnormal.id).all()
    assert {m.metric_code for m in metrics} == {"sbp", "hr"}
    assert all(m.source == "visit" for m in metrics)


# ---------------------------------------------------------------- 异常上报 / 处置队列
def test_anomaly_report_queue_and_handle(client, db, headers, accounts, make_order):
    order = make_order(status=OrderStatus.IN_SERVICE, checkin_at=utcnow() - timedelta(minutes=15))
    resp = client.post(
        f"{API}/orders/{order.id}/anomaly",
        json={"anomaly_type": "sudden_illness", "description": "老人突发胸闷，需要转诊"},
        headers=headers("nurse"),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == OrderStatus.ANOMALY
    assert data["anomaly"]["status"] == "pending"

    queue = client.get(f"{API}/orders/anomalies", headers=headers("grid"))
    assert queue.status_code == 200, queue.text
    items = queue.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["work_order_code"] == order.code
    assert items[0]["waiting_minutes"] is not None
    anomaly_id = items[0]["id"]

    handled = client.post(
        f"{API}/orders/anomalies/{anomaly_id}/handle",
        json={"handle_note": "已联系家属并安排社区医生上门"},
        headers=headers("grid"),
    )
    assert handled.status_code == 200, handled.text
    handled_data = handled.json()["data"]
    assert handled_data["status"] == "handled"
    assert handled_data["handle_minutes"] >= 0
    assert handled_data["order_status"] == OrderStatus.COMPLETED

    db.refresh(order)
    assert order.status == OrderStatus.COMPLETED
    assert db.query(Notification).filter(Notification.related_type == "anomaly_report").count() >= 1


# ---------------------------------------------------------------- 拒单与预约回退
def test_reject_requires_reason(client, db, headers, make_order):
    order = make_order()
    resp = client.post(f"{API}/orders/{order.id}/reject", json={}, headers=headers("nurse"))
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "RULE_VIOLATION"


def test_reject_rolls_back_appointment(client, db, headers, accounts, make_order):
    from app.models import Appointment, AppointmentLog

    appointment = Appointment(
        code="AP9001",
        elder_id=accounts["elder"].id,
        service_item_id=accounts["item"].id,
        desired_start=BASE_TIME,
        status="dispatched",
        community_org_id=accounts["elder"].community_org_id,
    )
    db.add(appointment)
    db.flush()
    order = make_order(appointment_id=appointment.id)
    resp = client.post(
        f"{API}/orders/{order.id}/reject", json={"reason": "临时有急单无法履约"}, headers=headers("nurse")
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == OrderStatus.CANCELLED
    assert data["appointment"]["to_status"] == "pending_dispatch"
    db.refresh(appointment)
    assert appointment.status == "pending_dispatch"
    assert db.query(AppointmentLog).filter(AppointmentLog.appointment_id == appointment.id).count() == 1
    assert data["notified_count"] >= 1


# ---------------------------------------------------------------- 列表 / 数据范围
def test_orders_list_nurse_sees_only_own(client, db, headers, make_order, make_staff):
    make_order()
    other = make_staff()
    make_order(nurse_id=other.id)

    resp = client.get(f"{API}/orders", headers=headers("nurse"))
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["total"] == 1
    assert all(item["nurse_id"] != other.id for item in body["items"])

    resp = client.get(f"{API}/orders", headers=headers("org_admin"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["total"] == 2


def test_orders_list_filters_and_elder_scope(client, db, headers, accounts, make_order):
    order = make_order(status=OrderStatus.PENDING_ACCEPT)
    client.post(f"{API}/orders/{order.id}/accept", headers=headers("nurse"))
    resp = client.get(f"{API}/orders", params={"status": "accepted"}, headers=headers("nurse"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["total"] == 1

    resp = client.get(f"{API}/orders", params={"elder_id": accounts["elder"].id}, headers=headers("elder"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["total"] == 1

    resp = client.get(f"{API}/orders", headers=headers("grid"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["total"] == 1

    resp = client.get(f"{API}/orders", params={"date": "2026-10-15"}, headers=headers("org_admin"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["total"] == 1
    resp = client.get(f"{API}/orders", params={"date": "2020-01-01"}, headers=headers("org_admin"))
    assert resp.json()["data"]["total"] == 0
    # 非法日期
    resp = client.get(f"{API}/orders", params={"date": "2026/10/15"}, headers=headers("org_admin"))
    assert resp.status_code == 400 and resp.json()["code"] == "RULE_VIOLATION"


def test_order_detail_contains_execution_records(client, db, headers, accounts, make_order, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"), raising=False)
    order = make_order(status=OrderStatus.ACCEPTED)
    elder = accounts["elder"]
    client.post(
        f"{API}/orders/{order.id}/checkin",
        json={"lat": elder.lat, "lng": elder.lng},
        headers=headers("nurse"),
    )
    client.post(
        f"{API}/orders/{order.id}/items",
        json={"items": [{"name": "整理床铺", "completed": True}]},
        headers=headers("nurse"),
    )
    client.post(
        f"{API}/orders/{order.id}/photos",
        files={"file": ("a.png", _png_bytes(400, 300), "image/png")},
        headers=headers("nurse"),
    )
    resp = client.get(f"{API}/orders/{order.id}", headers=headers("nurse"))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["id"] == order.id
    assert data["elder_name"] == elder.name
    assert [item["item_name"] for item in data["items"]] == ["整理床铺"]
    assert len(data["photos"]) == 1
    assert [track["kind"] for track in data["tracks"]] == ["checkin"]
    assert data["anomalies"] == []
    assert data["evaluation"] is None
