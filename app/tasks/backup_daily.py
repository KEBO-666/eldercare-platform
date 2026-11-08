"""每日全量备份：SQLite 在线备份 API + 附件目录打包 + 版本保留。

设计要点：
- 使用 ``sqlite3.Connection.backup``（在线备份 API），不直接复制 WAL 状态的库文件，
  保证备份时点一致性；
- 备份文件命名 ``<YYYYMMDD_HHMMSS>_eldercare.db``，附件目录打包为同名 ``.zip``；
- 写 ``BackupRecord``（file_path / file_size / tables_snapshot / status）；
- 只保留最近 ``settings.backup_keep_versions`` 个版本，超出的 ``.db/.zip`` 成对删除；
- 使用独立 Session（``SessionLocal``），不依赖请求上下文，可由 APScheduler 直接调用。
"""
from __future__ import annotations

import logging
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.base import Base
from app.models import BackupRecord

logger = logging.getLogger("eldercare.backup")

BACKUP_SUFFIX = "_eldercare.db"


class BackupError(RuntimeError):
    """备份/恢复过程中的可预期错误。"""


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _new_backup_path(backup_dir: Path) -> Path:
    """生成不冲突的备份路径（同一秒多次备份时追加序号）。"""
    stamp = _timestamp()
    candidate = backup_dir / f"{stamp}{BACKUP_SUFFIX}"
    index = 1
    while candidate.exists():
        candidate = backup_dir / f"{stamp}_{index}{BACKUP_SUFFIX}"
        index += 1
    return candidate


def _engine_of(db: Session):
    bind = db.get_bind()
    return getattr(bind, "engine", bind)


def table_counts(db: Session) -> dict[str, int]:
    """统计各业务表当前记录数（用于备份快照与恢复核对报告）。"""
    from app import models  # noqa: F401  确保 Base.metadata 注册全部表

    try:
        existing = set(inspect(db.get_bind()).get_table_names())
    except Exception:  # noqa: BLE001 - 统计失败不阻断主流程
        logger.exception("读取数据库表清单失败")
        existing = set()
    counts: dict[str, int] = {}
    for name in Base.metadata.tables:
        if name not in existing:
            continue
        try:
            counts[name] = int(db.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar() or 0)
        except Exception:  # noqa: BLE001 - 单表统计失败记为 0
            logger.exception("统计表 %s 记录数失败", name)
            counts[name] = 0
    return counts


def _zip_uploads(upload_dir: Path, zip_path: Path) -> Path:
    """把附件目录打包为 zip（无附件时也生成空包，便于成对保留/恢复）。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        if upload_dir.exists():
            for path in sorted(upload_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(upload_dir).as_posix())
    return zip_path


def _sqlite_backup(db: Session, target: Path) -> None:
    """用 SQLite 在线备份 API 把当前库复制到 target 文件。"""
    engine = _engine_of(db)
    if engine.dialect.name != "sqlite":
        raise BackupError("当前数据库类型不支持在线备份（仅支持 SQLite）")
    db.commit()  # 结束当前事务，释放连接，保证备份时点一致
    raw = engine.raw_connection()
    target_conn = None
    try:
        source = raw.driver_connection
        target_conn = sqlite3.connect(str(target))
        source.backup(target_conn)
        target_conn.commit()
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite 在线备份失败：{exc}") from exc
    finally:
        if target_conn is not None:
            target_conn.close()
        raw.close()


def _remove_backup_files(path: Path) -> None:
    for victim in (path, path.with_suffix(".zip")):
        try:
            if victim.exists():
                victim.unlink()
        except OSError:  # pragma: no cover - 文件被占用等环境问题
            logger.warning("清理旧备份失败：%s", victim)


def _cleanup_old_backups(db: Session, backup_dir: Path, keep: int) -> list[str]:
    """保留最近 keep 个版本，其余 .db/.zip 成对删除，并同步删除备份记录。"""
    keep = max(int(keep or 0), 1)
    files = sorted(
        backup_dir.glob(f"*{BACKUP_SUFFIX}"),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )
    cleaned: list[str] = []
    for path in files[keep:]:
        _remove_backup_files(path)
        cleaned.append(path.name)
        try:
            db.execute(delete(BackupRecord).where(BackupRecord.file_path == str(path)))
        except Exception:  # noqa: BLE001 - 记录清理失败不影响文件清理
            logger.exception("清理备份记录失败：%s", path)
    if cleaned:
        db.commit()
    return cleaned


def _create_backup(db: Session, backup_type: str, created_by: int | None) -> dict[str, Any]:
    backup_dir = settings.backup_path
    upload_dir = settings.upload_path
    backup_dir.mkdir(parents=True, exist_ok=True)

    target = _new_backup_path(backup_dir)
    _sqlite_backup(db, target)

    tables = table_counts(db)
    zip_path = _zip_uploads(upload_dir, target.with_suffix(".zip"))
    file_size = target.stat().st_size

    record = BackupRecord(
        file_path=str(target),
        file_size=file_size,
        backup_type=backup_type,
        status="success",
        tables_snapshot=tables,
        note=f"附件包：{zip_path.name}",
        created_by=created_by,
    )
    db.add(record)
    db.commit()

    cleaned = _cleanup_old_backups(db, backup_dir, settings.backup_keep_versions)
    return {
        "ok": True,
        "backup_id": record.id,
        "file": str(target),
        "file_name": target.name,
        "zip": str(zip_path),
        "file_size": file_size,
        "tables": tables,
        "cleaned": cleaned,
        "backup_type": backup_type,
        "created_at": record.created_at,
    }


def run_daily_backup(
    backup_type: str = "auto",
    created_by: int | None = None,
    *,
    session_factory: sessionmaker | None = None,
) -> dict[str, Any]:
    """执行一次全量备份，返回 {ok, file, tables, cleaned, ...}。

    ``session_factory`` 仅供测试/特殊场景注入；默认使用独立 ``SessionLocal``。
    """
    if session_factory is None:
        from app.db.session import SessionLocal

        session_factory = SessionLocal

    db = session_factory()
    try:
        return _create_backup(db, backup_type=backup_type, created_by=created_by)
    except Exception as exc:  # noqa: BLE001 - 定时任务不应因备份失败中断
        logger.exception("备份失败")
        try:
            db.rollback()
            db.add(
                BackupRecord(
                    file_path="",
                    file_size=0,
                    backup_type=backup_type,
                    status="failed",
                    tables_snapshot={},
                    note=str(exc)[:255],
                    created_by=created_by,
                )
            )
            db.commit()
        except Exception:  # noqa: BLE001 - 连失败记录都写不了时仅记录日志
            logger.exception("写入备份失败记录出错")
        return {"ok": False, "message": str(exc), "file": None, "tables": {}, "cleaned": []}
    finally:
        db.close()
