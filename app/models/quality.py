"""评价、回访、投诉、月度绩效、消息通知。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import CallbackStatus, ComplaintStatus
from app.db.base import Base, CommonMixin


class Evaluation(CommonMixin, Base):
    __tablename__ = "evaluation"

    work_order_id: Mapped[int] = mapped_column(ForeignKey("work_order.id"), index=True)
    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    nurse_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True, index=True)
    punctuality: Mapped[int] = mapped_column(Integer, default=5)
    attitude: Mapped[int] = mapped_column(Integer, default=5)
    professionalism: Mapped[int] = mapped_column(Integer, default=5)
    effect: Mapped[int] = mapped_column(Integer, default=5)
    avg_score: Mapped[float] = mapped_column(Float, default=5.0)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    rated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CallbackTask(CommonMixin, Base):
    __tablename__ = "callback_task"

    evaluation_id: Mapped[int | None] = mapped_column(ForeignKey("evaluation.id"), nullable=True, index=True)
    complaint_id: Mapped[int | None] = mapped_column(ForeignKey("complaint.id"), nullable=True)
    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    assigned_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default=CallbackStatus.PENDING, index=True)
    result: Mapped[str | None] = mapped_column(String(32), nullable=True)     # improve / to_complaint
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Complaint(CommonMixin, Base):
    __tablename__ = "complaint"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    work_order_id: Mapped[int | None] = mapped_column(ForeignKey("work_order.id"), nullable=True)
    nurse_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=ComplaintStatus.NEW, index=True)
    handler_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    institution_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    conclusion: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_established: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)   # refund/change_nurse/deduct
    refund_bill_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    concluded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class StaffPerformanceMonthly(CommonMixin, Base):
    __tablename__ = "staff_performance_monthly"

    staff_id: Mapped[int] = mapped_column(ForeignKey("staff.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)     # YYYY-MM
    avg_score: Mapped[float] = mapped_column(Float, default=0)
    punctuality_rate: Mapped[float] = mapped_column(Float, default=0)
    anomaly_rate: Mapped[float] = mapped_column(Float, default=0)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    complaint_count: Mapped[int] = mapped_column(Integer, default=0)
    rank_no: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Notification(CommonMixin, Base):
    __tablename__ = "notification"

    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True, index=True)
    elder_id: Mapped[int | None] = mapped_column(ForeignKey("elder.id"), nullable=True)
    channel: Mapped[str] = mapped_column(String(16), default="inapp")
    title: Mapped[str] = mapped_column(String(128))
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="sent")
    related_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    related_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
