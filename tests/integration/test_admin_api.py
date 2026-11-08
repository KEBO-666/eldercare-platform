"""A1 集成测试：系统管理接口（用户/权限/组织/审计/配置/备份恢复）。"""
from __future__ import annotations

from pathlib import Path

from app.core.config import settings
from app.core.enums import AuditAction
from app.core.permissions import PERMISSIONS, ROLE_PERMISSIONS
from app.models import AuditLog, BackupRecord, Org, User

TEST_PASSWORD = "Passw0rd!"


def _count_nodes(nodes: list[dict]) -> int:
    return sum(1 + _count_nodes(node.get("children") or []) for node in nodes)


# ==========================================================================
# 鉴权与数据范围
# ==========================================================================
def test_admin_requires_login(client):
    for path in ("/api/v1/admin/users", "/api/v1/admin/roles", "/api/v1/admin/orgs",
                 "/api/v1/admin/audit-logs", "/api/v1/admin/config", "/api/v1/admin/backups"):
        resp = client.get(path)
        assert resp.status_code == 401, path
        assert resp.json()["code"] == "UNAUTHORIZED"


def test_grid_user_forbidden_on_admin_endpoints(client, headers):
    grid_headers = headers("grid")
    for path in ("/api/v1/admin/users", "/api/v1/admin/roles", "/api/v1/admin/permissions",
                 "/api/v1/admin/orgs", "/api/v1/admin/audit-logs", "/api/v1/admin/config",
                 "/api/v1/admin/backups"):
        resp = client.get(path, headers=grid_headers)
        assert resp.status_code == 403, path
        assert resp.json()["code"] == "FORBIDDEN"

    resp = client.post("/api/v1/admin/users", headers=grid_headers, json={
        "username": "hacker", "password": "Hacker123", "role": "super",
    })
    assert resp.status_code == 403


def test_super_list_users_with_filters(client, headers, accounts):
    super_headers = headers("super")

    resp = client.get("/api/v1/admin/users", headers=super_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == len(accounts["users"])
    assert len(data["items"]) == data["total"]
    first = data["items"][0]
    for field_name in ("id", "username", "real_name", "role", "role_name", "org_id", "data_scope", "is_active"):
        assert field_name in first

    resp = client.get("/api/v1/admin/users?role=grid", headers=super_headers)
    assert resp.json()["data"]["total"] == 2
    assert {item["username"] for item in resp.json()["data"]["items"]} == {"grid", "grid2"}

    resp = client.get("/api/v1/admin/users?keyword=orgadmin", headers=super_headers)
    assert resp.json()["data"]["total"] == 1
    assert resp.json()["data"]["items"][0]["role"] == "org_admin"

    resp = client.get("/api/v1/admin/users?page=2&size=3", headers=super_headers)
    page_data = resp.json()["data"]
    assert page_data["page"] == 2
    assert page_data["size"] == 3
    assert len(page_data["items"]) == 3
    assert page_data["total"] == len(accounts["users"])


def test_org_admin_only_sees_own_scope(client, headers, accounts, orgs):
    org_headers = headers("org_admin")

    resp = client.get("/api/v1/admin/users", headers=org_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 3
    assert {item["username"] for item in data["items"]} == {"orgadmin", "nurse", "elder"}
    assert {item["org_id"] for item in data["items"]} == {orgs["community1"].id}

    # 不能编辑范围外用户
    resp = client.put(
        f"/api/v1/admin/users/{accounts['users']['street'].id}",
        headers=org_headers,
        json={"is_active": False},
    )
    assert resp.status_code == 403

    # 只能在本机构下创建用户
    resp = client.post("/api/v1/admin/users", headers=org_headers, json={
        "username": "scope_nurse", "password": "ScopePass1", "role": "nurse",
        "org_id": orgs["grid1"].id, "data_scope": "self",
    })
    assert resp.status_code == 403

    resp = client.post("/api/v1/admin/users", headers=org_headers, json={
        "username": "scope_nurse", "password": "ScopePass1", "role": "nurse",
        "org_id": orgs["community1"].id, "data_scope": "self",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["org_id"] == orgs["community1"].id

    # 无权分配超管角色
    resp = client.post("/api/v1/admin/users", headers=org_headers, json={
        "username": "fake_super", "password": "FakePass12", "role": "super",
    })
    assert resp.status_code == 403


# ==========================================================================
# 用户管理
# ==========================================================================
def test_create_user_validation(client, headers, orgs):
    super_headers = headers("super")

    resp = client.post("/api/v1/admin/users", headers=super_headers, json={
        "username": "super", "password": "StrongPass1", "role": "grid",
    })
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "CONFLICT"

    resp = client.post("/api/v1/admin/users", headers=super_headers, json={
        "username": "weak_user", "password": "1234567", "role": "grid", "org_id": orgs["grid1"].id,
    })
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "RULE_VIOLATION"

    resp = client.post("/api/v1/admin/users", headers=super_headers, json={
        "username": "unknown_role", "password": "StrongPass1", "role": "emperor",
    })
    assert resp.status_code == 400


def test_user_lifecycle_create_disable_reset(client, headers, orgs, db):
    super_headers = headers("super")

    resp = client.post("/api/v1/admin/users", headers=super_headers, json={
        "username": "new_nurse", "password": "NewPass123", "real_name": "新护理员",
        "role": "nurse", "org_id": orgs["community1"].id, "data_scope": "self",
        "phone": "13800001111",
    })
    assert resp.status_code == 200, resp.text
    created = resp.json()["data"]
    user_id = created["id"]
    assert created["username"] == "new_nurse"
    assert created["role"] == "nurse"
    assert created["phone"] == "138****1111"

    login_resp = client.post("/api/v1/auth/login", json={"username": "new_nurse", "password": "NewPass123"})
    assert login_resp.status_code == 200, login_resp.text

    # 停用后无法登录
    resp = client.put(f"/api/v1/admin/users/{user_id}", headers=super_headers, json={"is_active": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["is_active"] is False
    login_resp = client.post("/api/v1/auth/login", json={"username": "new_nurse", "password": "NewPass123"})
    assert login_resp.status_code == 403

    # 启用 + 重置密码
    resp = client.put(f"/api/v1/admin/users/{user_id}", headers=super_headers, json={"is_active": True})
    assert resp.status_code == 200
    resp = client.post(
        f"/api/v1/admin/users/{user_id}/reset-password",
        headers=super_headers,
        json={"password": "ResetPass9"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["generated"] is False
    login_resp = client.post("/api/v1/auth/login", json={"username": "new_nurse", "password": "ResetPass9"})
    assert login_resp.status_code == 200

    # 不传新密码则随机生成并返回
    resp = client.post(f"/api/v1/admin/users/{user_id}/reset-password", headers=super_headers)
    assert resp.status_code == 200, resp.text
    generated = resp.json()["data"]
    assert generated["generated"] is True
    assert len(generated["password"]) >= 8
    login_resp = client.post(
        "/api/v1/auth/login", json={"username": "new_nurse", "password": generated["password"]}
    )
    assert login_resp.status_code == 200

    # 审计覆盖 create / update
    actions = {log.action for log in db.query(AuditLog).all()}
    assert AuditAction.CREATE in actions
    assert AuditAction.UPDATE in actions


# ==========================================================================
# 角色、权限与组织
# ==========================================================================
def test_roles_and_permissions(client, headers):
    super_headers = headers("super")

    resp = client.get("/api/v1/admin/roles", headers=super_headers)
    assert resp.status_code == 200, resp.text
    roles = resp.json()["data"]
    assert len(roles) == 6
    by_code = {role["code"]: role for role in roles}
    for code, permission_codes in ROLE_PERMISSIONS.items():
        assert by_code[code]["permission_count"] == len(permission_codes)

    resp = client.get("/api/v1/admin/permissions", headers=super_headers)
    data = resp.json()["data"]
    assert data["total"] == len(PERMISSIONS) == 35
    assert len(data["items"]) == 35
    assert sum(data["module_counts"].values()) == 35


def test_org_tree_and_create_hierarchy(client, headers, orgs):
    super_headers = headers("super")

    resp = client.get("/api/v1/admin/orgs", headers=super_headers)
    assert resp.status_code == 200, resp.text
    tree = resp.json()["data"]
    assert len(tree) == 1
    assert tree[0]["level"] == "street"
    assert _count_nodes(tree) == 6

    resp = client.get("/api/v1/admin/orgs?level=community", headers=super_headers)
    communities = resp.json()["data"]
    assert len(communities) == 2
    assert all(node["level"] == "community" for node in communities)
    assert sum(len(node["children"]) for node in communities) == 3

    resp = client.get("/api/v1/admin/orgs?level=grid", headers=super_headers)
    assert len(resp.json()["data"]) == 3

    resp = client.post("/api/v1/admin/orgs", headers=super_headers, json={
        "code": "CM09", "name": "新社区", "level": "community", "parent_id": orgs["street"].id,
    })
    assert resp.status_code == 200, resp.text
    community = resp.json()["data"]
    assert community["level"] == "community"

    resp = client.post("/api/v1/admin/orgs", headers=super_headers, json={
        "code": "GD09", "name": "新网格", "parent_id": community["id"],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["level"] == "grid"

    resp = client.post("/api/v1/admin/orgs", headers=super_headers, json={
        "code": "CM10", "name": "无上级社区", "level": "community",
    })
    assert resp.status_code == 400
    assert resp.json()["code"] == "RULE_VIOLATION"

    resp = client.post("/api/v1/admin/orgs", headers=super_headers, json={
        "code": "GD10", "name": "错误层级网格", "level": "grid", "parent_id": orgs["street"].id,
    })
    assert resp.status_code == 400

    resp = client.post("/api/v1/admin/orgs", headers=super_headers, json={
        "code": "CM09", "name": "重复社区", "level": "community", "parent_id": orgs["street"].id,
    })
    assert resp.status_code == 409


# ==========================================================================
# 审计
# ==========================================================================
def test_audit_logs_and_verify_chain(client, headers, orgs, db):
    super_headers = headers("super")

    resp = client.post("/api/v1/admin/orgs", headers=super_headers, json={
        "code": "CM88", "name": "审计社区", "level": "community", "parent_id": orgs["street"].id,
    })
    assert resp.status_code == 200

    resp = client.get("/api/v1/admin/audit-logs?action=create&entity_type=org", headers=super_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total"] == 1
    log = data["items"][0]
    assert log["action"] == "create"
    assert log["entity_type"] == "org"
    assert log["hash"] and log["prev_hash"]

    resp = client.get("/api/v1/admin/audit-logs?start=2030-01-01", headers=super_headers)
    assert resp.json()["data"]["total"] == 0
    resp = client.get("/api/v1/admin/audit-logs?start=2020-01-01&end=2099-12-31", headers=super_headers)
    assert resp.json()["data"]["total"] == 1

    resp = client.get("/api/v1/admin/audit/verify", headers=super_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["valid"] is True

    first = db.query(AuditLog).order_by(AuditLog.id.asc()).first()
    first.detail = "被篡改"
    db.commit()
    resp = client.get("/api/v1/admin/audit/verify", headers=super_headers)
    result = resp.json()["data"]
    assert result["valid"] is False
    assert result["broken_id"] == first.id


# ==========================================================================
# 运行期配置
# ==========================================================================
def test_config_endpoints(client, headers):
    super_headers = headers("super")

    resp = client.get("/api/v1/admin/config", headers=super_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["items"] == []
    assert data["dispatch_weights"] == [0.4, 0.3, 0.2, 0.1]
    assert data["defaults"]["dispatch_weights"] == "0.4,0.3,0.2,0.1"
    assert data["effective"]["dispatch_weights"] == "0.4,0.3,0.2,0.1"

    resp = client.put("/api/v1/admin/config", headers=super_headers, json={
        "key": "dispatch_weights", "value": "0.5,0.3,0.1,0.1",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["changed"] == ["dispatch_weights"]

    resp = client.put("/api/v1/admin/config", headers=super_headers, json={
        "items": [{"key": "order_daily_limit", "value": 5}],
    })
    assert resp.status_code == 200, resp.text

    resp = client.put("/api/v1/admin/config", headers=super_headers, json={"dispatch_top_n": 3})
    assert resp.status_code == 200, resp.text

    resp = client.get("/api/v1/admin/config", headers=super_headers)
    data = resp.json()["data"]
    assert len(data["items"]) == 3
    assert data["effective"]["dispatch_weights"] == "0.5,0.3,0.1,0.1"
    assert data["effective"]["order_daily_limit"] == 5

    resp = client.put("/api/v1/admin/config", headers=super_headers, json={
        "key": "dispatch_weights", "value": "0.5,0.5",
    })
    assert resp.status_code == 400
    assert resp.json()["code"] == "RULE_VIOLATION"


# ==========================================================================
# 备份与恢复（内存库同样走 SQLite 在线备份 API）
# ==========================================================================
def test_backup_and_restore_flow(client, headers, accounts, db, monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "backup_dir", str(backup_dir))
    monkeypatch.setattr(settings, "upload_dir", str(upload_dir))
    monkeypatch.setattr(settings, "backup_keep_versions", 30)
    super_headers = headers("super")

    # 备份前：6 个组织
    resp = client.get("/api/v1/admin/orgs", headers=super_headers)
    assert _count_nodes(resp.json()["data"]) == 6

    resp = client.post("/api/v1/admin/backup", headers=super_headers)
    assert resp.status_code == 200, resp.text
    backup = resp.json()["data"]
    assert backup["ok"] is True
    backup_id = backup["backup_id"]
    backup_file = Path(backup["file"])
    assert backup_file.is_file()
    assert backup_file.with_suffix(".zip").is_file()
    assert backup["tables"]["org"] == 6
    assert backup["tables"]["user"] == len(accounts["users"])

    # 备份列表可查
    resp = client.get("/api/v1/admin/backups", headers=super_headers)
    assert resp.status_code == 200, resp.text
    listed = resp.json()["data"]
    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["id"] == backup_id
    assert item["file_name"].endswith("_eldercare.db")
    assert item["tables_snapshot"]["org"] == 6
    assert item["exists"] is True

    # 备份后改动数据
    resp = client.post("/api/v1/admin/orgs", headers=super_headers, json={
        "code": "CM90", "name": "备份后新增社区", "level": "community", "parent_id": 1,
    })
    assert resp.status_code == 200, resp.text
    resp = client.get("/api/v1/admin/orgs", headers=super_headers)
    assert _count_nodes(resp.json()["data"]) == 7

    # 缺少 confirm=true 的恢复必须拒绝
    resp = client.post("/api/v1/admin/restore", headers=super_headers, json={"backup_id": backup_id})
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "RULE_VIOLATION"

    # 恢复不存在的备份
    resp = client.post(
        "/api/v1/admin/restore", headers=super_headers, json={"backup_id": 99999, "confirm": True}
    )
    assert resp.status_code == 404

    # 正式恢复
    resp = client.post(
        "/api/v1/admin/restore", headers=super_headers, json={"backup_id": backup_id, "confirm": True}
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()["data"]
    assert report["backup_id"] == backup_id
    assert report["integrity"]["integrity"] == "ok"
    assert report["before"]["org"] == 7
    assert report["after"]["org"] == 6
    assert report["diff"]["org"]["delta"] == -1

    # 数据回到备份时刻
    resp = client.get("/api/v1/admin/orgs", headers=super_headers)
    assert _count_nodes(resp.json()["data"]) == 6
    assert db.query(Org).filter(Org.code == "CM90").count() == 0
    # 备份目录记录仍可追溯
    assert db.query(BackupRecord).filter(BackupRecord.id == backup_id).count() == 1

    # 恢复审计
    resp = client.get("/api/v1/admin/audit-logs?action=restore", headers=super_headers)
    assert resp.json()["data"]["total"] >= 1
    audit = resp.json()["data"]["items"][0]
    assert audit["before"]["org"] == 7
    assert audit["after"]["org"] == 6

    # 恢复后审计链仍完整
    resp = client.get("/api/v1/admin/audit/verify", headers=super_headers)
    assert resp.json()["data"]["valid"] is True

    # 恢复后的用户仍可正常鉴权
    resp = client.get("/api/v1/admin/users", headers=super_headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == len(accounts["users"])


def test_restore_by_file_path_only(client, headers, db, tmp_path, monkeypatch):
    backup_dir = tmp_path / "backups"
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "backup_dir", str(backup_dir))
    monkeypatch.setattr(settings, "upload_dir", str(upload_dir))
    super_headers = headers("super")

    resp = client.post("/api/v1/admin/backup", headers=super_headers)
    assert resp.status_code == 200, resp.text
    file_path = resp.json()["data"]["file"]

    db.add(User(username="tmp_user", password_hash="x", real_name="临时", role_id=1, data_scope="self"))
    db.commit()
    assert db.query(User).filter(User.username == "tmp_user").count() == 1

    resp = client.post(
        "/api/v1/admin/restore", headers=super_headers, json={"file_path": file_path, "confirm": True}
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()["data"]
    assert report["before"]["user"] == report["after"]["user"] + 1
    assert db.query(User).filter(User.username == "tmp_user").count() == 0
