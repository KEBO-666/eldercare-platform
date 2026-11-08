"""补贴政策、账单、明细、对账差异、缴费记录。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import BillStatus, DiffStatus
from app.db.base import Base, CommonMixin


class SubsidyPolicy(CommonMixin, Base):
    __tablename__ = "subsidy_policy"

    ability_level: Mapped[str] = mapped_column(String(16), index=True)
    difficulty_type: Mapped[str] = mapped_column(String(16), index=True)
    subsidy_ratio: Mapped[float] = mapped_column(default=0.0)
    monthly_limit: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    ltc_ratio: Mapped[float] = mapped_column(default=0.0)     # 长护险支付比例
    effective_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Bill(CommonMixin, Base):
    __tablename__ = "bill"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    community_org_id: Mapped[int] = mapped_column(ForeignKey("org.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)          # YYYY-MM
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    subsidy_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    ltc_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    self_pay_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    diff_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    diff_ratio: Mapped[float] = mapped_column(default=0.0)
    status: Mapped[str] = mapped_column(String(16), default=BillStatus.DRAFT, index=True)
    needs_review: Mapped[bool] = mapped_column(default=False)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    spot_check_passed: Mapped[bool] = mapped_column(default=False)
    spot_check_ratio: Mapped[float] = mapped_column(default=0.0)


class BillDetail(CommonMixin, Base):
    __tablename__ = "bill_detail"

    bill_id: Mapped[int] = mapped_column(ForeignKey("bill.id"), index=True)
    work_order_id: Mapped[int | None] = mapped_column(ForeignKey("work_order.id"), nullable=True)
    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    price_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    quantity: Mapped[float] = mapped_column(default=1)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    subsidy_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    ltc_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    self_pay_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)


class ReconciliationDiff(CommonMixin, Base):
    __tablename__ = "reconciliation_diff"

    bill_id: Mapped[int | None] = mapped_column(ForeignKey("bill.id"), nullable=True, index=True)
    work_order_id: Mapped[int | None] = mapped_column(ForeignKey("work_order.id"), nullable=True)
    elder_id: Mapped[int | None] = mapped_column(ForeignKey("elder.id"), nullable=True)
    rule_code: Mapped[str] = mapped_column(String(32))     # duplicate/unfinished/duration/subsidy_limit
    description: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    status: Mapped[str] = mapped_column(String(16), default=DiffStatus.OPEN, index=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    handler_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PaymentRecord(CommonMixin, Base):
    __tablename__ = "payment_record"

    bill_id: Mapped[int] = mapped_column(ForeignKey("bill.id"), index=True)
    payer_type: Mapped[str] = mapped_column(String(16))    # government/insurance/self
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    status: Mapped[str] = mapped_column(String(16), default="paid")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
