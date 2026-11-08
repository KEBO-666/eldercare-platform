"""审计日志：哈希链写入与校验（不可篡改）。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, User

GENESIS_HASH = "0" * 64


def _payload(log: AuditLog) -> str:
    raw = "|".join(
        [
            str(log.user_id or ""),
            str(log.username or ""),
            log.action,
            str(log.entity_type or ""),
            str(log.entity_id or ""),
            json.dumps(log.before_json, ensure_ascii=False, sort_keys=True, default=str),
            json.dumps(log.after_json, ensure_ascii=False, sort_keys=True, default=str),
            str(log.detail or ""),
            str(log.ip or ""),
            str(log.ua or ""),
            log.prev_hash or GENESIS_HASH,
        ]
    )
    return raw


def compute_hash(log: AuditLog) -> str:
    return hashlib.sha256(_payload(log).encode("utf-8")).hexdigest()


def write_audit(
    db: Session,
    *,
    user: User | None,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    detail: str | None = None,
    request: Request | None = None,
) -> AuditLog:
    """写入一条审计日志（只 INSERT，不 UPDATE/DELETE）。"""
    prev = db.execute(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1)).scalar_one_or_none()
    ip = ua = None
    if request is not None:
        ip = request.client.host if request.client else None
        ua = (request.headers.get("user-agent") or "")[:255]
    log = AuditLog(
        user_id=getattr(user, "id", None),
        username=getattr(user, "username", None),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json=before,
        after_json=after,
        detail=detail,
        ip=ip,
        ua=ua,
        prev_hash=prev or GENESIS_HASH,
    )
    log.hash = compute_hash(log)
    db.add(log)
    db.flush()
    return log


def verify_chain(db: Session, limit: int = 5000) -> dict[str, Any]:
    """校验哈希链完整性，返回 {valid, checked, broken_id}。"""
    rows = db.execute(select(AuditLog).order_by(AuditLog.id.asc()).limit(limit)).scalars().all()
    prev = GENESIS_HASH
    for row in rows:
        if (row.prev_hash or GENESIS_HASH) != prev:
            return {"valid": False, "checked": len(rows), "broken_id": row.id, "reason": "prev_hash 不匹配"}
        expected = compute_hash(row)
        if expected != row.hash:
            return {"valid": False, "checked": len(rows), "broken_id": row.id, "reason": "hash 不匹配"}
        prev = row.hash
    return {"valid": True, "checked": len(rows), "broken_id": None}
