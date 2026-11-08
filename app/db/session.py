"""数据库引擎与会话管理。"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

connect_args = {"check_same_thread": False} if settings.db_url.startswith("sqlite") else {}
engine: Engine = create_engine(
    settings.db_url,
    connect_args=connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_conn, _record) -> None:  # pragma: no cover - 环境相关
    """SQLite 打开外键约束与 WAL，提升并发与一致性。"""
    if settings.db_url.startswith("sqlite"):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求级会话，异常回滚、结束关闭。"""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """建表（演示环境使用；生产可切换 Alembic）。"""
    from app import models  # noqa: F401  确保模型注册到 metadata
    from app.db.base import Base

    Base.metadata.create_all(bind=engine)
