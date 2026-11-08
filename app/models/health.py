"""健康指标、设备、预警规则、预警单与处置动作。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import AlertLevel, AlertRuleType, AlertStatus, MetricSource
from app.db.base import Base, CommonMixin


class HealthMetric(CommonMixin, Base):
    __tablename__ = "health_metric"

    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    metric_code: Mapped[str] = mapped_column(String(16), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default=MetricSource.VISIT)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("device.id"), nullable=True)
    measured_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    work_order_id: Mapped[int | None] = mapped_column(ForeignKey("work_order.id"), nullable=True)
    recorded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Device(CommonMixin, Base):
    __tablename__ = "device"

    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    device_type: Mapped[str] = mapped_column(String(32), default="band")   # band/watch/bp
    device_code: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="online")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AlertRule(CommonMixin, Base):
    """可配置预警规则（阈值 / 连续异常 / 静默超时）。"""
    __tablename__ = "alert_rule"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    rule_type: Mapped[str] = mapped_column(String(16), default=AlertRuleType.THRESHOLD)
    metric_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    operator: Mapped[str | None] = mapped_column(String(4), nullable=True)     # >=, <=, >, <
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold2: Mapped[float | None] = mapped_column(Float, nullable=True)
    window_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    silence_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    level: Mapped[str] = mapped_column(String(8), default=AlertLevel.YELLOW)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=30)
    escalate_minutes: Mapped[int] = mapped_column(Integer, default=30)
    push_minutes: Mapped[int] = mapped_column(Integer, default=5)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class Alert(CommonMixin, Base):
    __tablename__ = "alert"

    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("alert_rule.id"), nullable=True)
    work_order_id: Mapped[int | None] = mapped_column(ForeignKey("work_order.id"), nullable=True)
    level: Mapped[str] = mapped_column(String(8), default=AlertLevel.BLUE, index=True)
    status: Mapped[str] = mapped_column(String(16), default=AlertStatus.PENDING, index=True)
    metric_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    metric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_from_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)   # 处置截止
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    escalated_to_org_id: Mapped[int | None] = mapped_column(ForeignKey("org.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolution_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    measure: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handling_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AlertAction(CommonMixin, Base):
    """预警处置流水：推送 / 响应 / 升级 / 闭环。"""
    __tablename__ = "alert_action"

    alert_id: Mapped[int] = mapped_column(ForeignKey("alert.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(16))    # push/ack/escalate/resolve
    channel: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operator_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
