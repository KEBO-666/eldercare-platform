"""组织、用户、角色权限、护理员。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import DataScope, OrgLevel, RoleCode
from app.db.base import Base, CommonMixin


class Org(CommonMixin, Base):
    """组织树：街道 → 社区 → 网格。"""
    __tablename__ = "org"

    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    level: Mapped[str] = mapped_column(String(16), default=OrgLevel.COMMUNITY, index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("org.id"), nullable=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)


role_permission = Table(
    "role_permission",
    Base.metadata,
    Column("role_id", ForeignKey("role.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", ForeignKey("permission.id", ondelete="CASCADE"), primary_key=True),
)


class Role(CommonMixin, Base):
    __tablename__ = "role"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, default=RoleCode.ELDER)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    data_scope: Mapped[str] = mapped_column(String(16), default=DataScope.SELF)
    permissions: Mapped[list["Permission"]] = relationship(
        secondary=role_permission, lazy="selectin", back_populates="roles"
    )


class Permission(CommonMixin, Base):
    __tablename__ = "permission"

    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    module: Mapped[str] = mapped_column(String(32), default="common")
    roles: Mapped[list[Role]] = relationship(
        secondary=role_permission, lazy="selectin", back_populates="permissions"
    )


class User(CommonMixin, Base):
    __tablename__ = "user"

    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    real_name: Mapped[str] = mapped_column(String(64))
    phone_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("role.id"), index=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("org.id"), nullable=True, index=True)
    data_scope: Mapped[str] = mapped_column(String(16), default=DataScope.SELF)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    role: Mapped[Role] = relationship(lazy="joined")
    org: Mapped[Org | None] = relationship(lazy="joined")


class Staff(CommonMixin, Base):
    """护理员档案（与 User 一对一）。"""
    __tablename__ = "staff"

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), unique=True, index=True)
    employee_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    phone_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    skills: Mapped[list] = mapped_column(JSON, default=list)           # ["shower","bedcare"]
    service_radius_km: Mapped[float] = mapped_column(Float, default=3.0)
    base_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    base_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    rating_cached: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_count_cached: Mapped[int] = mapped_column(Integer, default=0)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("org.id"), nullable=True, index=True)
    is_focus: Mapped[bool] = mapped_column(Boolean, default=False)     # 重点关注（3 次投诉成立）
