"""服务项目与服务价格版本。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ItemStatus, PricingMode
from app.db.base import Base, SoftCommonMixin


class ServiceItem(SoftCommonMixin, Base):
    __tablename__ = "service_item"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32), default="other")
    pricing_mode: Mapped[str] = mapped_column(String(16), default=PricingMode.PER_TIME)
    standard_duration_min: Mapped[int] = mapped_column(Float, default=60)
    required_skills: Mapped[list] = mapped_column(JSON, default=list)
    applicable_levels: Mapped[list] = mapped_column(JSON, default=list)   # 空表示全部适配
    is_subsidized: Mapped[bool] = mapped_column(Boolean, default=True)
    subsidy_ratio: Mapped[float] = mapped_column(Float, default=0.0)      # 基础补贴比例
    status: Mapped[str] = mapped_column(String(8), default=ItemStatus.ON, index=True)
    current_price_version_id: Mapped[int | None] = mapped_column(ForeignKey("service_price_version.id"), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class ServicePriceVersion(SoftCommonMixin, Base):
    __tablename__ = "service_price_version"

    service_item_id: Mapped[int] = mapped_column(ForeignKey("service_item.id"), index=True)
    version: Mapped[int] = mapped_column(default=1)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    effective_from: Mapped[datetime] = mapped_column(DateTime)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int | None] = mapped_column(nullable=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
