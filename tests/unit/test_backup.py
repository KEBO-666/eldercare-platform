"""A1 单元测试：SQLite 在线备份、附件打包、版本保留与按时间点恢复。"""
from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.enums import AuditAction
from app.core.exceptions import RuleViolation
from app.models import AuditLog, BackupRecord, Org
from app.services import admin_service
from app.tasks import backup_daily
from app.tasks.backup_daily import run_daily_backup, table_counts

TEST_PASSWORD = "Passw0rd!"


@pytest.fixture()
def backup_env(tmp_path, monkeypatch):
    """把备份目录/上传目录隔离到临时目录，避免污染项目 data/。"""
    backup_dir = tmp_path / "backups"
    upload_dir = tmp_path / "uploads"
    backup_dir.mkdir(parents=True, exist_ok=True)
    upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "backup_dir", str(backup_dir))
    monkeypatch.setattr(settings, "upload_dir", str(upload_dir))
    monkeypatch.setattr(settings, "backup_keep_versions", 30)
    return {"backup_dir": backup_dir, "upload_dir": upload_dir}


@pytest.fixture()
def session_factory(db):
    engine = db.get_bind()
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def _latest_record(db) -> BackupRecord:
    return db.query(BackupRecord).order_by(BackupRecord.id.desc()).first()


def test_run_daily_backup_creates_file_record_and_snapshot(db, accounts, backup_env, session_factory):
    upload_file = backup_env["upload_dir"] / "photo_001.jpg"
    upload_file.write_bytes(b"\xff\xd8fake-image")
    db.commit()

    result = run_daily_backup(
        "manual",
        created_by=accounts["users"]["super"].id,
        session_factory=session_factory,
    )
    assert result["ok"] is True
    assert result["tables"]["org"] == 6
    assert result["tables"]["user"] == len(accounts["users"])

    backup_file = Path(result["file"])
    assert backup_file.is_file()
    assert backup_file.parent == backup_env["backup_dir"]
    assert backup_file.name.endswith("_eldercare.db")
    zip_file = backup_file.with_suffix(".zip")
    assert zip_file.is_file()
    with zipfile.ZipFile(zip_file) as archive:
        assert "photo_001.jpg" in archive.namelist()

    record = _latest_record(db)
    assert record.id == result["backup_id"]
    assert record.backup_type == "manual"
    assert record.status == "success"
    assert record.file_size > 0
    assert record.tables_snapshot == result["tables"]
    assert record.created_by == accounts["users"]["super"].id

    # 备份文件是合法 SQLite 库，数据与快照一致
    connection = sqlite3.connect(str(backup_file))
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM org").fetchone()[0] == 6
        assert connection.execute("SELECT COUNT(*) FROM user").fetchone()[0] == len(accounts["users"])
    finally:
        connection.close()


def test_table_counts_matches_database(db, orgs, accounts):
    counts = table_counts(db)
    assert counts["org"] == 6
    assert counts["user"] == len(accounts["users"])
    assert counts["elder"] == 1
    assert counts["permission"] == 35


def test_backup_retention_keeps_latest_versions(db, accounts, backup_env, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "backup_keep_versions", 2)
    stamps = iter(["20261108_010000", "20261108_020000", "20261108_030000"])
    monkeypatch.setattr(backup_daily, "_timestamp", lambda: next(stamps))

    first = run_daily_backup("manual", created_by=1, session_factory=session_factory)
    second = run_daily_backup("manual", created_by=1, session_factory=session_factory)
    third = run_daily_backup("manual", created_by=1, session_factory=session_factory)

    assert first["ok"] and second["ok"] and third["ok"]
    dbs = sorted(backup_env["backup_dir"].glob("*_eldercare.db"))
    zips = sorted(backup_env["backup_dir"].glob("*_eldercare.zip"))
    assert len(dbs) == 2
    assert len(zips) == 2
    names = {path.name for path in dbs}
    assert Path(first["file"]).name not in names
    assert Path(third["file"]).name in names
    assert (backup_env["backup_dir"] / "20261108_020000_eldercare.db").is_file()
    assert (backup_env["backup_dir"] / "20261108_030000_eldercare.db").is_file()

    assert first["cleaned"] == []
    assert second["cleaned"] == []
    assert third["cleaned"] == ["20261108_010000_eldercare.db"]
    # 备份记录与文件版本同步保留 2 条
    assert db.query(BackupRecord).count() == 2


def test_restore_returns_records_to_backup_point(db, accounts, backup_env, session_factory):
    db.commit()
    backup = run_daily_backup(
        "manual", created_by=accounts["users"]["super"].id, session_factory=session_factory
    )
    assert backup["ok"] is True
    org_count_at_backup = db.query(Org).count()
    assert org_count_at_backup == 6

    # 备份后新增数据（模拟误操作）
    db.add_all(
        [
            Org(code="CM90", name="备份后新增社区", level="community", parent_id=1),
            Org(code="GD90", name="备份后新增网格", level="grid", parent_id=1),
        ]
    )
    db.commit()
    assert db.query(Org).count() == 8

    report = admin_service.restore_backup(
        db,
        backup_id=backup["backup_id"],
        operator=accounts["users"]["super"],
    )
    assert report["integrity"]["integrity"] == "ok"
    assert report["before"]["org"] == 8
    assert report["after"]["org"] == 6
    assert report["diff"]["org"] == {"before": 8, "after": 6, "delta": -2}
    assert db.query(Org).count() == 6
    assert db.query(Org).filter(Org.code == "CM90").count() == 0

    # 恢复动作写审计
    logs = db.query(AuditLog).filter(AuditLog.action == AuditAction.RESTORE).all()
    assert len(logs) == 1
    assert logs[0].before_json["org"] == 8
    assert logs[0].after_json["org"] == 6

    # 备份目录记录在恢复后仍可追溯
    assert db.query(BackupRecord).filter(BackupRecord.id == backup["backup_id"]).count() == 1


def test_restore_requires_target_and_existing_file(db, accounts, backup_env):
    with pytest.raises(RuleViolation):
        admin_service.restore_backup(db, operator=accounts["users"]["super"])

    missing = backup_env["backup_dir"] / "not_exists_eldercare.db"
    with pytest.raises(RuleViolation):
        admin_service.restore_backup(db, file_path=str(missing), operator=accounts["users"]["super"])

    record = BackupRecord(file_path=str(missing), file_size=0, backup_type="manual", status="success")
    db.add(record)
    db.commit()
    with pytest.raises(RuleViolation):
        admin_service.restore_backup(db, backup_id=record.id, operator=accounts["users"]["super"])


def test_restore_rejects_corrupt_backup(db, accounts, backup_env):
    corrupt = backup_env["backup_dir"] / "corrupt_eldercare.db"
    corrupt.write_bytes(b"this is not a sqlite database")
    with pytest.raises(RuleViolation):
        admin_service.restore_backup(db, file_path=str(corrupt), operator=accounts["users"]["super"])

    # 合法 SQLite 但不是业务库
    foreign = backup_env["backup_dir"] / "foreign_eldercare.db"
    connection = sqlite3.connect(str(foreign))
    connection.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    with pytest.raises(RuleViolation):
        admin_service.restore_backup(db, file_path=str(foreign), operator=accounts["users"]["super"])


def test_backup_failure_writes_failed_record(db, accounts, backup_env, session_factory, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("模拟备份失败")

    monkeypatch.setattr(backup_daily, "_sqlite_backup", _boom)
    result = run_daily_backup("auto", created_by=1, session_factory=session_factory)
    assert result["ok"] is False
    assert "模拟备份失败" in result["message"]

    record = _latest_record(db)
    assert record.status == "failed"
    assert "模拟备份失败" in (record.note or "")
    assert list(backup_env["backup_dir"].glob("*_eldercare.db")) == []
