"""老人档案、能力评估、变更日志、二次授权。"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import AbilityLevel, DifficultyType
from app.db.base import Base, CommonMixin, TimestampMixin


class Elder(CommonMixin, Base):
    __tablename__ = "elder"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)   # 老人编号
    name: Mapped[str] = mapped_column(String(64))
    gender: Mapped[str] = mapped_column(String(8), default="unknown")        # male/female/unknown
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # 敏感字段：密文 + HMAC 摘要（支持等值检索）
    id_card_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    id_card_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    phone_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    address_enc: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    address_masked: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 列表快速展示
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)

    community_org_id: Mapped[int] = mapped_column(ForeignKey("org.id"), index=True)
    grid_org_id: Mapped[int | None] = mapped_column(ForeignKey("org.id"), nullable=True, index=True)

    ability_level: Mapped[str] = mapped_column(String(16), default=AbilityLevel.INTACT, index=True)
    difficulty_type: Mapped[str] = mapped_column(String(16), default=DifficultyType.NORMAL)
    insurance_type: Mapped[str | None] = mapped_column(String(32), nullable=True)   # 医保类型
    ltc_type: Mapped[str | None] = mapped_column(String(32), nullable=True)         # 长护险类型

    emergency_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    emergency_relation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    emergency_phone_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)

    allergy: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True, index=True)
    is_deleted: Mapped[int] = mapped_column(Integer, default=0, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ElderTag(CommonMixin, Base):
    __tablename__ = "elder_tag"

    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    tag: Mapped[str] = mapped_column(String(32), index=True)


class ElderDisease(CommonMixin, Base):
    __tablename__ = "elder_disease"

    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    diagnosed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ElderMedication(CommonMixin, Base):
    __tablename__ = "elder_medication"

    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    dosage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    frequency: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AbilityAssessment(CommonMixin, Base):
    __tablename__ = "ability_assessment"

    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    level: Mapped[str] = mapped_column(String(16))
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assessed_at: Mapped[datetime] = mapped_column(DateTime)
    assessor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    basis: Mapped[str | None] = mapped_column(Text, nullable=True)


class ArchiveChangeLog(CommonMixin, Base):
    __tablename__ = "archive_change_log"

    elder_id: Mapped[int] = mapped_column(ForeignKey("elder.id"), index=True)
    changed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    changed_fields: Mapped[list] = mapped_column(JSON, default=list)
    before_json: Mapped[dict] = mapped_column(JSON, default=dict)
    after_json: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AccessGrant(TimestampMixin, Base):
    """敏感字段二次授权留痕。"""
    __tablename__ = "access_grant"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(32), default="elder")
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    fields: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
