"""审计日志（哈希链）、备份记录、配置项。"""
from __future__ import annotations

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CommonMixin


class AuditLog(CommonMixin, Base):
    """只允许 INSERT；hash/prev_hash 组成哈希链，保证不可篡改。"""
    __tablename__ = "audit_log"

    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ua: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hash: Mapped[str] = mapped_column(String(64), index=True)


class BackupRecord(CommonMixin, Base):
    __tablename__ = "backup_record"

    file_path: Mapped[str] = mapped_column(String(512))
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    backup_type: Mapped[str] = mapped_column(String(16), default="auto")   # auto/manual
    status: Mapped[str] = mapped_column(String(16), default="success")
    tables_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # 各表记录数
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ConfigItem(CommonMixin, Base):
    """运行期可调配置（派单权重、阈值等），支持页面热更新。"""
    __tablename__ = "config_item"

    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[str] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
