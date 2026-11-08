"""A1 集成测试：认证接口（登录/登出/当前用户/审计）。"""
from __future__ import annotations

from app.core.enums import AuditAction
from app.core.permissions import ROLE_PERMISSIONS
from app.models import AuditLog, User

TEST_PASSWORD = "Passw0rd!"


def test_login_returns_token_user_and_httponly_cookie(client, accounts):
    resp = client.post("/api/v1/auth/login", json={"username": "super", "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0

    data = body["data"]
    assert data["token"]
    user = data["user"]
    assert user["id"] == accounts["users"]["super"].id
    assert user["username"] == "super"
    assert user["real_name"] == "super"
    assert user["role"] == "super"
    assert user["role_name"] == "系统超级管理员"
    assert user["org_id"] is not None
    assert user["data_scope"] == "street"
    assert user["permissions"] == ["*"]

    set_cookie = resp.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Path=/" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert client.cookies.get("access_token")


def test_login_wrong_password_returns_401_and_writes_audit(db, client, accounts):
    resp = client.post("/api/v1/auth/login", json={"username": "super", "password": "bad-password"})
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["code"] == "UNAUTHORIZED"
    assert body["message"] == "用户名或密码错误"

    logs = db.query(AuditLog).filter(AuditLog.action == AuditAction.LOGIN_FAIL).all()
    assert len(logs) == 1
    assert logs[0].entity_id == accounts["users"]["super"].id


def test_login_unknown_user_hides_account_existence(client, accounts):
    resp = client.post("/api/v1/auth/login", json={"username": "ghost", "password": "whatever123"})
    assert resp.status_code == 401
    assert resp.json()["message"] == "用户名或密码错误"


def test_login_disabled_user_forbidden(db, client, accounts):
    user = accounts["users"]["grid"]
    user.is_active = False
    db.commit()
    resp = client.post("/api/v1/auth/login", json={"username": "grid", "password": TEST_PASSWORD})
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "FORBIDDEN"


def test_me_requires_authentication(client):
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"


def test_me_returns_current_user_and_permissions(client, headers, accounts):
    resp = client.get("/api/v1/auth/me", headers=headers("grid"))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["id"] == accounts["users"]["grid"].id
    assert data["username"] == "grid"
    assert data["role"] == "grid"
    assert data["data_scope"] == "grid"
    assert sorted(data["permissions"]) == sorted(ROLE_PERMISSIONS["grid"])


def test_me_super_has_wildcard_permission(client, headers):
    resp = client.get("/api/v1/auth/me", headers=headers("super"))
    assert resp.status_code == 200
    assert resp.json()["data"]["permissions"] == ["*"]


def test_login_then_me_via_cookie_and_logout(client, accounts):
    login_resp = client.post("/api/v1/auth/login", json={"username": "nurse", "password": TEST_PASSWORD})
    assert login_resp.status_code == 200

    me_resp = client.get("/api/v1/auth/me")
    assert me_resp.status_code == 200
    assert me_resp.json()["data"]["username"] == "nurse"

    logout_resp = client.post("/api/v1/auth/logout")
    assert logout_resp.status_code == 200, logout_resp.text
    assert logout_resp.json()["code"] == 0

    me_after = client.get("/api/v1/auth/me")
    assert me_after.status_code == 401


def test_login_updates_last_login_and_writes_login_audit(db, client, accounts):
    before = db.get(User, accounts["users"]["elder"].id).last_login_at
    assert before is None

    resp = client.post("/api/v1/auth/login", json={"username": "elder", "password": TEST_PASSWORD})
    assert resp.status_code == 200

    user = db.get(User, accounts["users"]["elder"].id)
    assert user.last_login_at is not None

    logs = db.query(AuditLog).filter(AuditLog.action == AuditAction.LOGIN).all()
    assert len(logs) == 1
    assert logs[0].username == "elder"
    assert logs[0].ip is not None
