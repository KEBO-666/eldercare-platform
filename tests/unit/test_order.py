"""C 模块单测：工单执行留痕（签到距离、照片压缩与限额、时长异常、异常处置）。"""
from __future__ import annotations

import io
import math
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image

from app.core.config import settings
from app.core.enums import AnomalyType, AppointmentStatus, OrderStatus
from app.core.exceptions import Conflict, RuleViolation
from app.db.base import utcnow
from app.models import (
    AnomalyReport,
    Appointment,
    AppointmentLog,
    HealthMetric,
    Notification,
    WorkOrder,
    WorkOrderItem,
    WorkOrderPhoto,
    WorkOrderTrack,
)
from app.services import order_service

BASE_TIME = datetime(2026, 10, 15, 9, 0, 0)


def _offset_lat(lat: float, meters: float) -> float:
    """沿纬度方向偏移指定米数（用于构造签到坐标）。"""
    return lat + math.degrees(meters / order_service.EARTH_RADIUS_M)


def _png_bytes(width: int = 2400, height: int = 1600) -> bytes:
    image = Image.new("RGB", (width, height), (180, 60, 40))
    pixels = image.load()
    for x in range(0, width, 6):
        for y in range(0, height, 6):
            pixels[x, y] = (x % 255, y % 255, (x + y) % 255)
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
            code=f"WO{counter['n']:04d}",
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


@pytest.fixture()
def make_appointment(db, accounts):
    counter = {"n": 0}

    def _make(**kwargs) -> Appointment:
        counter["n"] += 1
        defaults = dict(
            code=f"AP{counter['n']:04d}",
            elder_id=accounts["elder"].id,
            service_item_id=accounts["item"].id,
            desired_start=BASE_TIME,
            status=AppointmentStatus.DISPATCHED,
            community_org_id=accounts["elder"].community_org_id,
        )
        defaults.update(kwargs)
        appointment = Appointment(**defaults)
        db.add(appointment)
        db.flush()
        return appointment

    return _make


# ---------------------------------------------------------------- 纯函数
def test_haversine_distance_accuracy():
    lat, lng = 31.2310, 121.4740
    assert order_service.haversine_m(lat, lng, lat, lng) == 0.0
    # 1 度纬度 ≈ 111.19 公里
    assert order_service.haversine_m(lat, lng, lat + 1, lng) == pytest.approx(111_195, rel=0.001)
    # 精确 199 米 / 201 米
    assert order_service.haversine_m(lat, lng, _offset_lat(lat, 199), lng) == pytest.approx(199, abs=1)
    assert order_service.haversine_m(lat, lng, _offset_lat(lat, 201), lng) == pytest.approx(201, abs=1)


def test_compress_image_limits_long_edge():
    raw = _png_bytes(2400, 1600)
    data, width, height = order_service.compress_image(raw)
    assert max(width, height) <= order_service.PHOTO_MAX_EDGE
    assert (width, height) == (1280, 853)
    assert len(data) < len(raw)
    assert Image.open(io.BytesIO(data)).format == "JPEG"


def test_compress_image_rejects_non_image():
    with pytest.raises(RuleViolation):
        order_service.compress_image(b"not-an-image")


# ---------------------------------------------------------------- 接单 / 拒单
def test_accept_order_records_time(db, accounts, make_order):
    order = make_order()
    data = order_service.accept_order(db, accounts["users"]["nurse"], order.id)
    assert data["status"] == OrderStatus.ACCEPTED
    assert "checkin" in data["available_actions"]
    db.refresh(order)
    assert order.accepted_at is not None


def test_accept_illegal_state_raises_conflict(db, accounts, make_order):
    order = make_order(status=OrderStatus.ACCEPTED)
    with pytest.raises(Conflict):
        order_service.accept_order(db, accounts["users"]["nurse"], order.id)


def test_reject_requires_reason(db, accounts, make_order):
    order = make_order()
    with pytest.raises(RuleViolation):
        order_service.reject_order(db, accounts["users"]["nurse"], order.id, "   ")


def test_reject_rolls_back_appointment_and_notifies_grid(db, accounts, make_order, make_appointment):
    appointment = make_appointment()
    order = make_order(appointment_id=appointment.id)
    data = order_service.reject_order(db, accounts["users"]["nurse"], order.id, "突发急事", request=None)
    assert data["status"] == OrderStatus.CANCELLED
    db.refresh(appointment)
    assert appointment.status == AppointmentStatus.PENDING_DISPATCH
    logs = db.query(AppointmentLog).filter(AppointmentLog.appointment_id == appointment.id).all()
    assert len(logs) == 1
    assert logs[0].action == "reject_rollback"
    assert logs[0].to_status == AppointmentStatus.PENDING_DISPATCH
    notifications = db.query(Notification).filter(Notification.related_type == "work_order").all()
    assert notifications, "拒单后应通知网格员"
    assert data["notified_count"] >= 1


# ---------------------------------------------------------------- 签到距离
def test_checkin_at_199_meters_passes(db, accounts, make_order):
    order = make_order(status=OrderStatus.ACCEPTED)
    elder = accounts["elder"]
    lat = _offset_lat(elder.lat, 199)
    data = order_service.checkin_order(db, accounts["users"]["nurse"], order.id, lat=lat, lng=elder.lng)
    assert data["status"] == OrderStatus.IN_SERVICE
    assert data["distance_m"] == pytest.approx(199, abs=1)
    db.refresh(order)
    assert order.checkin_at is not None
    track = db.query(WorkOrderTrack).filter(WorkOrderTrack.work_order_id == order.id).one()
    assert track.kind == "checkin"
    assert track.distance_m == pytest.approx(199, abs=1)


def test_checkin_at_201_meters_without_note_rejected(db, accounts, make_order):
    order = make_order(status=OrderStatus.ACCEPTED)
    elder = accounts["elder"]
    lat = _offset_lat(elder.lat, 201)
    with pytest.raises(RuleViolation) as excinfo:
        order_service.checkin_order(db, accounts["users"]["nurse"], order.id, lat=lat, lng=elder.lng)
    assert "200 米" in str(excinfo.value)
    assert excinfo.value.data["distance_m"] > settings.checkin_distance_threshold_m
    db.refresh(order)
    assert order.status == OrderStatus.ACCEPTED
    assert order.checkin_at is None
    assert db.query(WorkOrderTrack).filter(WorkOrderTrack.work_order_id == order.id).count() == 0


def test_checkin_at_201_meters_with_note_passes(db, accounts, make_order):
    order = make_order(status=OrderStatus.ACCEPTED)
    elder = accounts["elder"]
    lat = _offset_lat(elder.lat, 201)
    data = order_service.checkin_order(
        db, accounts["users"]["nurse"], order.id, lat=lat, lng=elder.lng, note="小区同名楼栋，门牌有偏差"
    )
    assert data["status"] == OrderStatus.IN_SERVICE
    db.refresh(order)
    assert order.checkin_note == "小区同名楼栋，门牌有偏差"
    assert order.checkin_distance_m > 200


def test_checkin_requires_accepted_status(db, accounts, make_order):
    order = make_order()  # pending_accept
    elder = accounts["elder"]
    with pytest.raises(Conflict):
        order_service.checkin_order(
            db, accounts["users"]["nurse"], order.id, lat=elder.lat, lng=elder.lng
        )


# ---------------------------------------------------------------- 逐项勾选
def test_items_only_allowed_in_service(db, accounts, make_order):
    order = make_order(status=OrderStatus.ACCEPTED)
    elder = accounts["elder"]
    with pytest.raises(Conflict):
        order_service.update_items(db, accounts["users"]["nurse"], order.id, [{"name": "测量血压", "completed": True}])
    order_service.checkin_order(db, accounts["users"]["nurse"], order.id, lat=elder.lat, lng=elder.lng)
    result = order_service.update_items(
        db,
        accounts["users"]["nurse"],
        order.id,
        [{"name": "测量血压", "completed": True}, {"name": "协助服药", "completed": False}],
    )
    assert len(result["items"]) == 2
    assert {item["item_name"] for item in result["items"]} == {"测量血压", "协助服药"}
    done = db.query(WorkOrderItem).filter(WorkOrderItem.work_order_id == order.id).filter_by(completed=True).one()
    assert done.completed_at is not None
    # 再次勾选同名项目 → 更新而不是新建
    order_service.update_items(db, accounts["users"]["nurse"], order.id, [{"name": "测量血压", "completed": False}])
    assert db.query(WorkOrderItem).filter(WorkOrderItem.work_order_id == order.id).count() == 2


# ---------------------------------------------------------------- 照片
def test_photo_upload_limit_and_compression(db, accounts, make_order, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"), raising=False)
    order = make_order(status=OrderStatus.ACCEPTED)
    raw = _png_bytes()
    for index in range(settings.photo_limit):
        result = order_service.save_photo(
            db, accounts["users"]["nurse"], order.id, raw, filename=f"p{index}.png"
        )
        assert result["count"] == index + 1
        assert max(result["width"], result["height"]) <= order_service.PHOTO_MAX_EDGE
    with pytest.raises(RuleViolation) as excinfo:
        order_service.save_photo(db, accounts["users"]["nurse"], order.id, raw, filename="p4.png")
    assert "最多 3 张" in str(excinfo.value)

    photos = db.query(WorkOrderPhoto).filter(WorkOrderPhoto.work_order_id == order.id).all()
    assert len(photos) == settings.photo_limit
    for photo in photos:
        path = _resolve_path(photo.path)
        assert path.exists(), f"照片未落盘：{path}"
        assert 0 < photo.size_kb <= settings.photo_max_kb
        with Image.open(path) as image:
            assert max(image.size) <= order_service.PHOTO_MAX_EDGE
            assert image.format == "JPEG"


def test_photo_requires_executable_status(db, accounts, make_order):
    order = make_order(status=OrderStatus.COMPLETED)
    with pytest.raises(Conflict):
        order_service.save_photo(db, accounts["users"]["nurse"], order.id, _png_bytes(200, 200))


# ---------------------------------------------------------------- 签退与时长异常
def test_checkout_duration_deviation_29_minutes_not_flagged(db, accounts, make_order):
    order = make_order(status=OrderStatus.IN_SERVICE, checkin_at=utcnow() - timedelta(minutes=89))
    data = order_service.checkout_order(db, accounts["users"]["nurse"], order.id, summary="服务完成")
    assert data["duration_min"] == 89
    assert data["standard_duration_min"] == 60
    assert data["deviation_min"] == 29
    assert data["duration_anomaly"] is False
    assert data["status"] == OrderStatus.PENDING_EVALUATION


def test_checkout_duration_deviation_31_minutes_flagged(db, accounts, make_order):
    order = make_order(status=OrderStatus.IN_SERVICE, checkin_at=utcnow() - timedelta(minutes=91))
    data = order_service.checkout_order(db, accounts["users"]["nurse"], order.id, summary="服务完成")
    assert data["duration_min"] == 91
    assert data["deviation_min"] == 31
    assert data["duration_anomaly"] is True
    db.refresh(order)
    assert order.duration_anomaly is True
    tracks = db.query(WorkOrderTrack).filter(WorkOrderTrack.work_order_id == order.id).all()
    assert [t.kind for t in tracks] == ["checkout"]


def test_checkout_requires_checkin(db, accounts, make_order):
    order = make_order(status=OrderStatus.IN_SERVICE)
    with pytest.raises(Conflict):
        order_service.checkout_order(db, accounts["users"]["nurse"], order.id, summary="未签到")


def test_checkout_writes_health_metrics(db, accounts, make_order):
    order = make_order(status=OrderStatus.IN_SERVICE, checkin_at=utcnow() - timedelta(minutes=60))
    data = order_service.checkout_order(
        db,
        accounts["users"]["nurse"],
        order.id,
        summary="测量血压",
        metrics=[{"metric_code": "sbp", "value": 150}, {"metric_code": "hr", "value": 82}],
    )
    assert len(data["metrics_recorded"]) == 2
    metrics = db.query(HealthMetric).filter(HealthMetric.work_order_id == order.id).all()
    assert {m.metric_code for m in metrics} == {"sbp", "hr"}
    assert all(m.source == "visit" for m in metrics)
    assert all(m.elder_id == order.elder_id for m in metrics)


def test_checkout_rejects_unknown_metric_code(db, accounts, make_order):
    order = make_order(status=OrderStatus.IN_SERVICE, checkin_at=utcnow() - timedelta(minutes=60))
    with pytest.raises(RuleViolation):
        order_service.checkout_order(
            db, accounts["users"]["nurse"], order.id, metrics=[{"metric_code": "blood", "value": 1}]
        )


# ---------------------------------------------------------------- 异常上报与处置
def test_anomaly_report_queue_and_handle(db, accounts, make_order):
    order = make_order(status=OrderStatus.IN_SERVICE, checkin_at=utcnow() - timedelta(minutes=10))
    data = order_service.report_anomaly(
        db,
        accounts["users"]["nurse"],
        order.id,
        anomaly_type=AnomalyType.SUDDEN_ILLNESS,
        description="老人突发胸闷",
    )
    assert data["status"] == OrderStatus.ANOMALY
    assert data["anomaly"]["status"] == "pending"
    db.refresh(order)
    assert order.status == OrderStatus.ANOMALY

    items, total = order_service.list_anomalies(db, accounts["users"]["grid"], status="pending")
    assert total == 1
    assert items[0]["waiting_minutes"] is not None
    assert items[0]["work_order_code"] == order.code

    handled = order_service.handle_anomaly(
        db, accounts["users"]["grid"], items[0]["id"], handle_note="已联系家属并转社区医生"
    )
    assert handled["status"] == "handled"
    assert handled["handle_minutes"] is not None and handled["handle_minutes"] >= 0
    assert handled["order_status"] == OrderStatus.COMPLETED
    report = db.query(AnomalyReport).filter(AnomalyReport.work_order_id == order.id).one()
    assert report.handled_by == accounts["users"]["grid"].id
    # 重复处置被拒绝
    with pytest.raises(RuleViolation):
        order_service.handle_anomaly(db, accounts["users"]["grid"], report.id, handle_note="再次处理")


def test_anomaly_report_invalid_type(db, accounts, make_order):
    order = make_order(status=OrderStatus.ACCEPTED)
    with pytest.raises(RuleViolation):
        order_service.report_anomaly(
            db, accounts["users"]["nurse"], order.id, anomaly_type="unknown_type", description="x"
        )


# ---------------------------------------------------------------- 详情
def test_order_detail_contains_execution_records(db, accounts, make_order, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "uploads"), raising=False)
    order = make_order(status=OrderStatus.ACCEPTED)
    nurse = accounts["users"]["nurse"]
    elder = accounts["elder"]
    order_service.checkin_order(db, nurse, order.id, lat=elder.lat, lng=elder.lng)
    order_service.update_items(db, nurse, order.id, [{"name": "整理床铺", "completed": True}])
    order_service.save_photo(db, nurse, order.id, _png_bytes(400, 300), filename="a.png")
    detail = order_service.get_order_detail(db, nurse, order.id)
    assert detail["status"] == OrderStatus.IN_SERVICE
    assert len(detail["items"]) == 1
    assert len(detail["photos"]) == 1
    assert [t["kind"] for t in detail["tracks"]] == ["checkin"]
    assert detail["anomalies"] == []
    assert detail["elder_name"] == elder.name
