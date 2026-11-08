"""预约单、工单、执行留痕、异常单。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import AnomalyType, AppointmentStatus, OrderStatus
from app.db.base import Base, CommonMixin


class Appointment(CommonMixin, Base):
    __tablename__ = "appointment"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    service_item_id: Mapped[int] = mapped_column(ForeignKey("service_item.id"), index=True)
    desired_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    address_enc: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    cycle_rule: Mapped[str | None] = mapped_column(String(32), nullable=True)   # weekly 等
    status: Mapped[str] = mapped_column(String(24), default=AppointmentStatus.PENDING_ACCEPT, index=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cancellation_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    is_chargeable_cancel: Mapped[bool] = mapped_column(Boolean, default=False)
    community_org_id: Mapped[int] = mapped_column(ForeignKey("org.id"), index=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accepted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AppointmentLog(CommonMixin, Base):
    __tablename__ = "appointment_log"

    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointment.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str] = mapped_column(String(24))
    action: Mapped[str] = mapped_column(String(32))
    operator_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)


class WorkOrder(CommonMixin, Base):
    __tablename__ = "work_order"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("appointment.id"), nullable=True, index=True)
    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    service_item_id: Mapped[int] = mapped_column(ForeignKey("service_item.id"), index=True)
    nurse_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default=OrderStatus.PENDING_ACCEPT, index=True)

    scheduled_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 签到
    checkin_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checkin_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    checkin_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    checkin_distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    checkin_note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # 签退
    checkout_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_anomaly: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 价格快照（版本化）
    price_version_id: Mapped[int | None] = mapped_column(ForeignKey("service_price_version.id"), nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    quantity: Mapped[float] = mapped_column(Float, default=1)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)

    community_org_id: Mapped[int] = mapped_column(ForeignKey("org.id"), index=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)


class WorkOrderItem(CommonMixin, Base):
    __tablename__ = "work_order_item"

    work_order_id: Mapped[int] = mapped_column(ForeignKey("work_order.id"), index=True)
    item_name: Mapped[str] = mapped_column(String(64))
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WorkOrderPhoto(CommonMixin, Base):
    __tablename__ = "work_order_photo"

    work_order_id: Mapped[int] = mapped_column(ForeignKey("work_order.id"), index=True)
    path: Mapped[str] = mapped_column(String(512))
    size_kb: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    uploaded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class WorkOrderTrack(CommonMixin, Base):
    """签到/签退定位轨迹。"""
    __tablename__ = "work_order_track"

    work_order_id: Mapped[int] = mapped_column(ForeignKey("work_order.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))     # checkin / checkout
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AnomalyReport(CommonMixin, Base):
    __tablename__ = "anomaly_report"

    work_order_id: Mapped[int | None] = mapped_column(ForeignKey("work_order.id"), nullable=True, index=True)
    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    anomaly_type: Mapped[str] = mapped_column(String(32), default=AnomalyType.OTHER)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reported_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")   # pending/handled
    handled_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    handle_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handle_note: Mapped[str | None] = mapped_column(Text, nullable=True)
