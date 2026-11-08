"""安全工具：密码哈希、JWT、字段级加密、脱敏、哈希检索。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings


# ---------------- 密码 ----------------
def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------- JWT ----------------
def create_access_token(subject: str | int, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_expire_minutes)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])


# ---------------- 字段级加密（AES-256-GCM） ----------------
def _aes_key() -> bytes:
    """由配置字符串派生固定 32 字节密钥（便于演示环境零配置）。"""
    return hashlib.sha256(settings.encryption_key.encode("utf-8")).digest()


def encrypt_field(plain: str | None) -> str | None:
    """加密敏感字段，返回 urlsafe base64 字符串；None/空串原样返回。"""
    if not plain:
        return plain
    nonce = os.urandom(12)
    ct = AESGCM(_aes_key()).encrypt(nonce, plain.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt_field(token: str | None) -> str | None:
    if not token:
        return token
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        nonce, ct = raw[:12], raw[12:]
        return AESGCM(_aes_key()).decrypt(nonce, ct, None).decode("utf-8")
    except Exception:  # 兼容历史明文数据
        return token


def field_digest(value: str | None) -> str | None:
    """敏感字段 HMAC 摘要，用于密文等值检索（不泄露明文）。"""
    if not value:
        return value
    return hmac.new(settings.field_hash_key.encode(), value.encode("utf-8"), hashlib.sha256).hexdigest()


# ---------------- 脱敏 ----------------
def mask_name(name: str | None) -> str:
    if not name:
        return ""
    if len(name) <= 1:
        return name
    if len(name) == 2:
        return name[0] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1]


def mask_id_card(value: str | None) -> str:
    if not value:
        return ""
    if len(value) < 8:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 7) + value[-4:]


def mask_phone(value: str | None) -> str:
    if not value:
        return ""
    if len(value) < 7:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 7) + value[-4:]


def mask_address(value: str | None, keep: int = 6) -> str:
    if not value:
        return ""
    return value if len(value) <= keep else value[:keep] + "****"


def mask_text(value: str | None, head: int = 2, tail: int = 2) -> str:
    if not value:
        return ""
    if len(value) <= head + tail:
        return "*" * len(value)
    return value[:head] + "*" * (len(value) - head - tail) + value[-tail:]
