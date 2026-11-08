"""ORM 基类与公共字段。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """统一使用本地时区感知时间（演示环境为 Asia/Shanghai，系统本地时间即东八区）。"""
    return datetime.now()


class Base(DeclarativeBase):
    pass


class IdMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class SoftDeleteMixin:
    is_deleted: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CommonMixin(IdMixin, TimestampMixin):
    """绝大多数业务表使用：自增主键 + 创建/更新时间。"""
    __abstract__ = True


class SoftCommonMixin(IdMixin, TimestampMixin, SoftDeleteMixin):
    """需要逻辑删除的表使用。"""
    __abstract__ = True
