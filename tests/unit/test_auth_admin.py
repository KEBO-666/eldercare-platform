"""A1 单元测试：认证服务（登录/权限装配/审计）与系统管理服务（用户/角色/组织/配置/审计链）。"""
from __future__ import annotations

import pytest

from app.core.audit import write_audit
from app.core.enums import AuditAction
from app.core.exceptions import Conflict, Forbidden, NotFound, RuleViolation, Unauthorized
from app.core.permissions import PERMISSIONS, ROLE_PERMISSIONS
from app.core.security import decode_token, verify_password
from app.models import AuditLog, ConfigItem, User
from app.services import admin_service, auth_service

TEST_PASSWORD = "Passw0rd!"


# ==========================================================================
# 认证服务
# ==========================================================================
def test_login_success_updates_last_login_and_writes_audit(db, accounts):
    user = accounts["users"]["super"]
    result = auth_service.login(db, "super", TEST_PASSWORD)

    payload = decode_token(result["token"])
    assert payload["sub"] == str(user.id)
    assert payload["role"] == "super"
    assert payload["username"] == "super"

    info = result["user"]
    assert info["id"] == user.id
    assert info["username"] == "super"
    assert info["role"] == "super"
    assert info["role_name"] == "系统超级管理员"
    assert info["data_scope"] == "street"
    assert info["permissions"] == ["*"]
    assert user.last_login_at is not None

    logs = db.query(AuditLog).filter(AuditLog.action == AuditAction.LOGIN).all()
    assert len(logs) == 1
    assert logs[0].user_id == user.id
    assert logs[0].username == "super"


def test_login_wrong_password_uses_unified_message_and_audits(db, accounts):
    with pytest.raises(Unauthorized) as exc:
        auth_service.login(db, "super", "wrong-password")
    assert str(exc.value) == "用户名或密码错误"
    logs = db.query(AuditLog).filter(AuditLog.action == AuditAction.LOGIN_FAIL).all()
    assert len(logs) == 1
    assert logs[0].user_id == accounts["users"]["super"].id


def test_login_unknown_user_same_unified_message(db, accounts):
    with pytest.raises(Unauthorized) as exc:
        auth_service.login(db, "not-exist-user", "whatever")
    assert str(exc.value) == "用户名或密码错误"
    logs = db.query(AuditLog).filter(AuditLog.action == AuditAction.LOGIN_FAIL).all()
    assert len(logs) == 1
    assert logs[0].user_id is None


def test_login_disabled_user_is_forbidden_and_audited(db, accounts):
    user = accounts["users"]["grid"]
    user.is_active = False
    db.commit()
    with pytest.raises(Forbidden):
        auth_service.login(db, "grid", TEST_PASSWORD)
    logs = db.query(AuditLog).filter(AuditLog.action == AuditAction.LOGIN_FAIL).all()
    assert len(logs) == 1
    assert "停用" in (logs[0].detail or "")


def test_user_payload_contains_role_permissions(accounts):
    payload = auth_service.build_user_payload(accounts["users"]["grid"])
    assert payload["role"] == "grid"
    assert payload["role_name"] == "社区网格员"
    assert sorted(payload["permissions"]) == sorted(ROLE_PERMISSIONS["grid"])
    assert "*" not in payload["permissions"]


# ==========================================================================
# 用户管理
# ==========================================================================
def test_create_user_duplicate_username_conflict(db, accounts, orgs):
    operator = accounts["users"]["super"]
    with pytest.raises(Conflict):
        admin_service.create_user(
            db,
            operator,
            {"username": "super", "password": "StrongPass1", "role": "grid", "org_id": orgs["grid1"].id},
        )


def test_create_user_weak_password_rule_violation(db, accounts, orgs):
    operator = accounts["users"]["super"]
    with pytest.raises(RuleViolation):
        admin_service.create_user(
            db,
            operator,
            {"username": "weak-pwd", "password": "1234567", "role": "grid", "org_id": orgs["grid1"].id},
        )


def test_create_user_success_and_audit(db, accounts, orgs):
    operator = accounts["users"]["super"]
    created = admin_service.create_user(
        db,
        operator,
        {
            "username": "new_nurse",
            "password": "NewPass123",
            "real_name": "新护理员",
            "role": "nurse",
            "org_id": orgs["community1"].id,
            "data_scope": "self",
            "phone": "13800001111",
        },
    )
    assert created["username"] == "new_nurse"
    assert created["role"] == "nurse"
    assert created["data_scope"] == "self"
    assert created["phone"] == "138****1111"

    user = db.query(User).filter(User.username == "new_nurse").one()
    assert verify_password("NewPass123", user.password_hash)
    assert user.phone_enc and user.phone_enc != "13800001111"

    logs = db.query(AuditLog).filter(
        AuditLog.action == AuditAction.CREATE, AuditLog.entity_type == "user"
    ).all()
    assert len(logs) == 1
    assert logs[0].entity_id == user.id


def test_update_user_disable_and_scope_guard(db, accounts, orgs):
    super_user = accounts["users"]["super"]
    org_admin = accounts["users"]["org_admin"]

    with pytest.raises(Forbidden):
        admin_service.update_user(db, org_admin, accounts["users"]["grid"].id, {"is_active": False})

    updated = admin_service.update_user(db, super_user, accounts["users"]["nurse"].id, {"is_active": False})
    assert updated["is_active"] is False
    assert db.get(User, accounts["users"]["nurse"].id).is_active is False
    logs = db.query(AuditLog).filter(
        AuditLog.action == AuditAction.UPDATE, AuditLog.entity_type == "user"
    ).all()
    assert len(logs) == 1
    assert logs[0].before_json["is_active"] is True
    assert logs[0].after_json["is_active"] is False

    # 超级管理员不能停用自己
    with pytest.raises(RuleViolation):
        admin_service.update_user(db, super_user, super_user.id, {"is_active": False})


def test_update_user_missing_raises_not_found(db, accounts):
    with pytest.raises(NotFound):
        admin_service.update_user(db, accounts["users"]["super"], 999999, {"real_name": "x"})


def test_reset_password_success_and_generated(db, accounts):
    operator = accounts["users"]["super"]
    target = accounts["users"]["grid"]

    result = admin_service.reset_password(db, operator, target.id, {"password": "ResetPass9"})
    assert result["generated"] is False
    assert verify_password("ResetPass9", db.get(User, target.id).password_hash)

    generated = admin_service.reset_password(db, operator, target.id)
    assert generated["generated"] is True
    assert len(generated["password"]) >= 8
    assert verify_password(generated["password"], db.get(User, target.id).password_hash)

    with pytest.raises(RuleViolation):
        admin_service.reset_password(db, operator, target.id, {"password": "short"})


def test_list_users_with_role_keyword_and_scope(db, accounts, orgs):
    super_user = accounts["users"]["super"]
    org_admin = accounts["users"]["org_admin"]

    items, total = admin_service.list_users(db, super_user)
    assert total == len(accounts["users"])
    assert len(items) == total

    grid_items, grid_total = admin_service.list_users(db, super_user, role="grid")
    assert grid_total == 2
    assert {item["username"] for item in grid_items} == {"grid", "grid2"}

    keyword_items, keyword_total = admin_service.list_users(db, super_user, keyword="orgadmin")
    assert keyword_total == 1
    assert keyword_items[0]["username"] == "orgadmin"

    scoped_items, scoped_total = admin_service.list_users(db, org_admin)
    assert scoped_total == 3
    assert {item["username"] for item in scoped_items} == {"orgadmin", "nurse", "elder"}
    assert {item["org_id"] for item in scoped_items} == {orgs["community1"].id}

    page_items, page_total = admin_service.list_users(db, super_user, page=2, size=3)
    assert page_total == total
    assert len(page_items) == 3


# ==========================================================================
# 角色 / 权限
# ==========================================================================
def test_roles_permission_counts(db, accounts):
    roles = admin_service.list_roles(db)
    assert len(roles) == 6
    by_code = {role["code"]: role for role in roles}
    for code, expected in ROLE_PERMISSIONS.items():
        assert by_code[code]["permission_count"] == len(expected)
        assert sorted(by_code[code]["permissions"]) == sorted(expected)
    assert by_code["super"]["permission_count"] == len(PERMISSIONS)


def test_permissions_total_and_modules(db, accounts):
    data = admin_service.list_permissions(db)
    assert data["total"] == len(PERMISSIONS) == 35
    assert len(data["items"]) == 35
    assert sum(data["module_counts"].values()) == 35
    assert data["module_counts"]["admin"] == 5


# ==========================================================================
# 组织
# ==========================================================================
def test_org_tree_and_level_filter(db, orgs):
    tree = admin_service.list_orgs(db)
    assert len(tree) == 1
    root = tree[0]
    assert root["level"] == "street"
    assert len(root["children"]) == 2
    community = next(node for node in root["children"] if node["id"] == orgs["community1"].id)
    assert {child["id"] for child in community["children"]} == {orgs["grid1"].id, orgs["grid2"].id}

    communities = admin_service.list_orgs(db, level="community")
    assert len(communities) == 2
    assert all(node["level"] == "community" for node in communities)
    assert sum(len(node["children"]) for node in communities) == 3

    grids = admin_service.list_orgs(db, level="grid")
    assert len(grids) == 3
    assert all(node["level"] == "grid" and node["children"] == [] for node in grids)

    with pytest.raises(RuleViolation):
        admin_service.list_orgs(db, level="county")


def test_create_org_hierarchy_validation(db, accounts, orgs):
    operator = accounts["users"]["super"]

    community = admin_service.create_org(
        db, operator, {"code": "CM09", "name": "新社区", "level": "community", "parent_id": orgs["street"].id}
    )
    assert community["level"] == "community"
    assert community["parent_id"] == orgs["street"].id

    grid = admin_service.create_org(db, operator, {"code": "GD09", "name": "新网格", "parent_id": community["id"]})
    assert grid["level"] == "grid"
    assert grid["parent_id"] == community["id"]

    # 层级不合法
    with pytest.raises(RuleViolation):
        admin_service.create_org(db, operator, {"code": "CM10", "name": "无上级社区", "level": "community"})
    with pytest.raises(RuleViolation):
        admin_service.create_org(
            db, operator, {"code": "GD10", "name": "错误网格", "level": "grid", "parent_id": orgs["street"].id}
        )
    with pytest.raises(RuleViolation):
        admin_service.create_org(
            db, operator, {"code": "ST02", "name": "下级街道", "level": "street", "parent_id": orgs["street"].id}
        )
    # 编码唯一
    with pytest.raises(Conflict):
        admin_service.create_org(
            db, operator, {"code": "CM09", "name": "重复社区", "level": "community", "parent_id": orgs["street"].id}
        )
    logs = db.query(AuditLog).filter(
        AuditLog.action == AuditAction.CREATE, AuditLog.entity_type == "org"
    ).all()
    assert len(logs) == 2


# ==========================================================================
# 运行期配置
# ==========================================================================
def test_config_defaults_and_upsert(db, accounts):
    operator = accounts["users"]["super"]
    assert admin_service.get_config(db, "dispatch_weights", None) == "0.4,0.3,0.2,0.1"
    assert float(admin_service.get_config(db, "order_daily_limit", 6)) == 6.0

    admin_service.set_config(db, operator, {"key": "dispatch_weights", "value": "0.5,0.3,0.1,0.1"})
    assert admin_service.get_config(db, "dispatch_weights", None) == "0.5,0.3,0.1,0.1"

    admin_service.set_config(db, operator, {"order_daily_limit": 5, "dispatch_top_n": 3})
    assert admin_service.get_config(db, "order_daily_limit", 6) == 5
    assert db.query(ConfigItem).filter(ConfigItem.key == "order_daily_limit").one().value == "5"

    data = admin_service.list_configs(db)
    assert data["dispatch_weights"] == [0.5, 0.3, 0.1, 0.1]
    assert data["defaults"]["dispatch_weights"] == "0.4,0.3,0.2,0.1"
    assert data["effective"]["order_daily_limit"] == 5
    assert len(data["items"]) == 3

    with pytest.raises(RuleViolation):
        admin_service.set_config(db, operator, {"key": "dispatch_weights", "value": "0.5,0.5"})
    with pytest.raises(RuleViolation):
        admin_service.set_config(db, operator, {})


# ==========================================================================
# 审计查询与哈希链校验
# ==========================================================================
def test_audit_logs_query_and_chain_verify(db, accounts):
    user = accounts["users"]["super"]
    write_audit(db, user=user, action="create", entity_type="elder", entity_id=1, after={"name": "张爷爷"})
    write_audit(db, user=user, action="update", entity_type="elder", entity_id=1, before={"age": 80}, after={"age": 81})
    write_audit(db, user=user, action="export", entity_type="elder", detail="导出档案")
    db.commit()

    items, total = admin_service.list_audit_logs(db, action="create")
    assert total == 1
    assert items[0]["entity_type"] == "elder"
    assert items[0]["after"] == {"name": "张爷爷"}

    entity_items, entity_total = admin_service.list_audit_logs(db, entity_type="elder", page=1, size=2)
    assert entity_total == 3
    assert len(entity_items) == 2

    result = admin_service.verify_audit_chain(db)
    assert result["valid"] is True
    assert result["checked"] == 3

    # 篡改任一历史记录后被校验发现断点
    first = db.query(AuditLog).order_by(AuditLog.id.asc()).first()
    first.detail = "被篡改"
    db.commit()
    broken = admin_service.verify_audit_chain(db)
    assert broken["valid"] is False
    assert broken["broken_id"] is not None
