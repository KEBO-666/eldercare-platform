"""健康预警权限边界：真实依赖、六角色、最小授权和数据范围，无真实库操作。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.models import (
    Alert,
    AlertAction,
    AlertRule,
    AuditLog,
    Device,
    HealthMetric,
    Notification,
    Permission,
    WorkOrder,
)
from app.services import alert_service

NOW = datetime(2026, 11, 12, 9)
API = "/api/v1"
MODELS = (HealthMetric, Device, Alert, AlertRule, AlertAction, Notification, AuditLog)
ROLE_SCOPE = {
    "elder": {"own"},
    "nurse": {"own", "other_community"},
    "grid": {"own", "same_grid"},
    "org_admin": {"own", "same_grid", "other_grid"},
    "street": {"own", "same_grid", "other_grid", "other_community"},
    "super": {"own", "same_grid", "other_grid", "other_community"},
}


@pytest.fixture()
def permission_world(db, accounts, orgs, make_elder, tmp_path, monkeypatch):
    monkeypatch.setattr(alert_service, "sms_log_path", lambda: tmp_path / "sms.log")
    elders = {
        "own": accounts["elder"],
        "same_grid": make_elder(),
        "other_grid": make_elder(grid_org_id=orgs["grid2"].id),
        "other_community": make_elder(
            community_org_id=orgs["community2"].id, grid_org_id=orgs["grid3"].id,
        ),
    }
    pending, referred = {}, {}
    for key, elder in elders.items():
        pending[key] = Alert(
            elder_id=elder.id, summary=f"PRIVATE-ALERT-{key}", level="yellow", status="pending",
            created_at=NOW, pushed_at=NOW, deadline_at=NOW + timedelta(minutes=30),
        )
        referred[key] = Alert(
            elder_id=elder.id, summary=f"PRIVATE-REFERRAL-{key}", level="blue", status="referred",
            created_at=NOW, resolved_at=NOW, pushed_at=NOW,
        )
        db.add_all([
            pending[key], referred[key],
            HealthMetric(elder_id=elder.id, metric_code="hr", value=75, measured_at=NOW, source="visit"),
            Device(elder_id=elder.id, device_code=f"PRIVATE-DEVICE-{key}"),
        ])
    for key in ("own", "other_community"):
        elder = elders[key]
        db.add(WorkOrder(
            code=f"PERMISSION-ORDER-{key}", elder_id=elder.id, service_item_id=accounts["item"].id,
            nurse_id=accounts["staff"].id, community_org_id=elder.community_org_id, scheduled_start=NOW,
        ))
    db.add(AlertRule(
        code="PERMISSION_RULE", name="PRIVATE-RULE-NAME", rule_type="threshold", metric_code="sbp",
        operator=">=", threshold=180, level="red", enabled=True,
    ))
    db.commit()
    for key, alert in pending.items():
        db.add(AlertAction(
            alert_id=alert.id, action_type="push", note=f"PRIVATE-ACTION-{key}", action_at=NOW,
        ))
    db.commit()
    return {"elders": elders, "pending": pending, "referred": referred}


def _snapshot(db):
    """比较所有相关存储列，既捕捉新增副作用也捕捉既有预警/规则被改写。"""
    return {
        model.__tablename__: [tuple(row) for row in db.execute(
            select(model.__table__).order_by(model.__table__.c.id),
        )]
        for model in MODELS
    }


def _token(user):
    return create_access_token(user.id, {"role": user.role.code, "username": user.username})


def _page_as(client, user, path):
    client.cookies.clear()
    if user is not None:
        client.cookies.set("access_token", _token(user))
    return client.get(path, follow_redirects=False)


def _main_content(response):
    return response.text.split('<main class="app-content"', 1)[1].split("</main>", 1)[0]


def _write(client, db, method, path, body, auth, expected):
    before = _snapshot(db)
    response = client.request(method, API + path, json=body, headers=auth)
    assert response.status_code == expected, response.text
    if expected >= 400:
        assert _snapshot(db) == before, f"拒绝的写操作产生副作用: {path}"
    return response


def _health_payload(world, key="own"):
    return {"elder_id": world["elders"][key].id, "metric_code": "hr", "value": 76}


@pytest.mark.parametrize("role", ROLE_SCOPE)
def test_default_six_role_permission_matrix(client, headers, db, accounts, permission_world, role):
    world, auth = permission_world, headers(role)
    own = world["elders"]["own"].id
    pending, referred = world["pending"]["own"].id, world["referred"]["own"].id
    health_read, health_write = role != "elder", role in {"nurse", "super"}
    handle, rule = role in {"grid", "street", "super"}, role == "super"
    for path in (f"/health/elders/{own}/metrics", f"/health/elders/{own}/series", "/health/devices"):
        response = client.get(API + path, headers=auth)
        assert response.status_code == (200 if health_read else 403), response.text
    for path in ("/alerts", "/alerts/stats", f"/alerts/{pending}", "/alerts/rules"):
        assert client.get(API + path, headers=auth).status_code == 200
    response = _page_as(client, accounts["users"][role], f"/monitoring/{own}")
    assert response.status_code == (200 if health_read else 403)
    if health_read:
        assert ('id="metric-form"' in response.text) is health_write
        assert ('id="report-form"' in response.text) is health_write
    response = _page_as(client, accounts["users"][role], f"/alerts/{pending}")
    assert response.status_code == 200
    assert ('id="alert-work-form"' in response.text) is handle
    response = _page_as(client, accounts["users"][role], "/alerts")
    assert response.status_code == 200
    assert ("data-rule-new" in _main_content(response)) is rule
    client.cookies.clear()
    for path, body in (
        ("/health/metrics", _health_payload(world)),
        ("/health/metrics/batch", {"items": [_health_payload(world)]}),
        ("/health/devices", {"elder_id": own, "device_code": f"NEW-DEVICE-{role}"}),
        ("/alerts/report", {"elder_id": own, "summary": "permission report", "note": "reported on site"}),
    ):
        _write(client, db, "POST", path, body, auth, 200 if health_write else 403)
    for suffix, body in (
        ("ack", {}), ("progress", {"note": "already contacted family"}),
        ("handle", {"resolution_type": "resolved", "measure": "checked in person"}),
    ):
        _write(client, db, "POST", f"/alerts/{pending}/{suffix}", body, auth, 200 if handle else 403)
    _write(client, db, "POST", f"/alerts/{referred}/follow-up", {"note": "actual outcome verified", "close": True},
           auth, 200 if handle else 403)
    _write(client, db, "POST", "/alerts/rules", {
        "code": f"NEW-PERMISSION-{role}", "name": "new threshold", "metric_code": "hr",
        "operator": ">", "threshold": 150,
    }, auth, 200 if rule else 403)
    existing_rule = db.scalar(select(AlertRule).where(AlertRule.code == "PERMISSION_RULE"))
    _write(client, db, "PUT", f"/alerts/rules/{existing_rule.id}", {"threshold": 190}, auth, 200 if rule else 403)
    _write(client, db, "POST", "/alerts/scan", {}, auth, 200 if rule else 403)


@pytest.mark.parametrize("role", ROLE_SCOPE)
def test_scope_same_for_list_stats_detail_health_devices_and_pages(
    client, headers, db, accounts, permission_world, role,
):
    world, allowed = permission_world, ROLE_SCOPE[role]
    auth = headers(role)
    response = client.get(API + "/alerts", headers=auth)
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert {item["elder_id"] for item in items} == {world["elders"][key].id for key in allowed}
    assert response.json()["data"]["total"] == len(allowed) * 2
    stats = client.get(API + "/alerts/stats", headers=auth).json()["data"]
    assert stats["total"] == stats["lifecycle_open"] == len(allowed) * 2
    assert stats["pending"] == stats["referral_followup_pending"] == len(allowed)
    response = _page_as(client, accounts["users"][role], "/alerts")
    assert response.status_code == 200
    for key in world["elders"]:
        assert (f"PRIVATE-ALERT-{key}" in response.text) is (key in allowed)
    client.cookies.clear()
    if role != "elder":
        devices = client.get(API + "/health/devices", headers=auth).json()["data"]
        assert {item["elder_id"] for item in devices["items"]} == {world["elders"][key].id for key in allowed}
    for key, elder in world["elders"].items():
        visible, alert_id = key in allowed, world["pending"][key].id
        response = client.get(API + f"/alerts/{alert_id}", headers=auth)
        assert response.status_code == (200 if visible else 404), response.text
        if not visible:
            assert f"PRIVATE-ALERT-{key}" not in response.text and f"PRIVATE-ACTION-{key}" not in response.text
        filtered = client.get(API + "/alerts", params={"elder_id": elder.id}, headers=auth)
        assert filtered.json()["data"]["total"] == (2 if visible else 0)
        response = _page_as(client, accounts["users"][role], f"/alerts/{alert_id}")
        assert response.status_code == (200 if visible else 404)
        if not visible:
            assert f"PRIVATE-ALERT-{key}" not in response.text and f"PRIVATE-ACTION-{key}" not in response.text
        client.cookies.clear()
        response = client.get(API + f"/health/elders/{elder.id}/metrics", headers=auth)
        assert response.status_code == (403 if role == "elder" or not visible else 200)
        response = client.get(API + f"/health/elders/{elder.id}/series", headers=auth)
        assert response.status_code == (403 if role == "elder" or not visible else 200)
        response = client.get(API + "/health/devices", headers=auth, params={"elder_id": elder.id})
        assert response.status_code == (403 if role == "elder" or not visible else 200)
        response = _page_as(client, accounts["users"][role], f"/monitoring/{elder.id}")
        assert response.status_code == (403 if role == "elder" or not visible else 200)
        if not visible:
            assert f"PRIVATE-ALERT-{key}" not in response.text and f"PRIVATE-DEVICE-{key}" not in response.text
        client.cookies.clear()


@pytest.mark.parametrize("identity", ["anonymous", "invalid_token", "disabled"])
def test_missing_invalid_or_disabled_identity_cannot_read_or_write(
    client, db, accounts, permission_world, identity,
):
    world = permission_world
    auth = {}
    if identity == "invalid_token":
        auth = {"Authorization": "Bearer invalid-token"}
    elif identity == "disabled":
        user = accounts["users"]["super"]
        auth = {"Authorization": "Bearer " + _token(user)}
        user.is_active = False
        db.commit()
    before = _snapshot(db)
    own, alert_id = world["elders"]["own"].id, world["pending"]["own"].id
    for path in ("/alerts", "/alerts/stats", f"/alerts/{alert_id}", "/alerts/rules",
                 f"/health/elders/{own}/metrics", "/health/devices"):
        response = client.get(API + path, headers=auth)
        assert response.status_code == 401, response.text
        assert "PRIVATE-" not in response.text
    for path, body in (
        ("/health/metrics", _health_payload(world)),
        ("/health/metrics/batch", {"items": [_health_payload(world)]}),
        ("/health/devices", {"elder_id": own, "device_code": "unauthorized"}),
        ("/alerts/report", {"elder_id": own, "summary": "not permitted", "note": "not permitted"}),
        (f"/alerts/{alert_id}/ack", {}),
        (f"/alerts/{alert_id}/progress", {"note": "not permitted"}),
        (f"/alerts/{alert_id}/handle", {"resolution_type": "resolved", "measure": "not permitted"}),
        (f"/alerts/{world['referred']['own'].id}/follow-up", {"note": "not permitted", "close": True}),
        ("/alerts/rules", {"code": "UNAUTH-RULE", "name": "not permitted", "metric_code": "hr", "operator": ">", "threshold": 150}),
        ("/alerts/scan", {}),
    ):
        _write(client, db, "POST", path, body, auth, 401)
    existing_rule = db.scalar(select(AlertRule).where(AlertRule.code == "PERMISSION_RULE"))
    _write(client, db, "PUT", f"/alerts/rules/{existing_rule.id}", {"threshold": 190}, auth, 401)
    if identity == "invalid_token":
        client.cookies.set("access_token", "invalid-token")
    elif identity == "disabled":
        client.cookies.set("access_token", auth["Authorization"][7:])
    for path in ("/monitoring", f"/monitoring/{own}", "/alerts", f"/alerts/{alert_id}"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/login")
        assert "PRIVATE-" not in response.text
    assert _snapshot(db) == before


@pytest.mark.parametrize("permission", ["health:read", "health:write", "alert:read", "alert:handle", "alert:rule"])
def test_single_capability_never_implies_other_capabilities(
    client, db, headers, accounts, permission_world, permission,
):
    user, world = accounts["users"]["org_admin"], permission_world
    user.role.permissions = [db.scalar(select(Permission).where(Permission.code == permission))]
    db.commit()
    auth, own, alert_id = headers("org_admin"), world["elders"]["own"].id, world["pending"]["own"].id
    assert client.get(API + f"/health/elders/{own}/metrics", headers=auth).status_code == (200 if permission == "health:read" else 403)
    assert client.get(API + "/alerts", headers=auth).status_code == (200 if permission == "alert:read" else 403)
    assert client.get(API + f"/alerts/{alert_id}", headers=auth).status_code == (200 if permission == "alert:read" else 403)
    assert client.get(API + "/alerts/stats", headers=auth).status_code == (200 if permission == "alert:read" else 403)
    assert client.get(API + "/alerts/rules", headers=auth).status_code == (200 if permission == "alert:read" else 403)
    health_page = _page_as(client, user, f"/monitoring/{own}")
    assert health_page.status_code == (200 if permission == "health:read" else 403)
    if permission == "health:read":
        assert 'id="metric-form"' not in health_page.text and 'id="report-form"' not in health_page.text
        assert "PRIVATE-ALERT-" not in health_page.text and "PRIVATE-RULE-NAME" not in health_page.text
        assert 'href="/alerts' not in _main_content(health_page)
        assert 'data-nav="alerts"' not in health_page.text
        dashboard = _page_as(client, user, "/")
        assert dashboard.status_code == 200
        assert "PRIVATE-ALERT-" not in dashboard.text and "PRIVATE-REFERRAL-" not in dashboard.text
        assert 'href="/alerts' not in _main_content(dashboard)
        assert 'data-nav="alerts"' not in dashboard.text
    alert_page = _page_as(client, user, f"/alerts/{alert_id}")
    assert alert_page.status_code == (200 if permission == "alert:read" else 403)
    if permission == "alert:read":
        assert 'id="alert-work-form"' not in alert_page.text
        assert f'href="/monitoring/{own}"' not in _main_content(alert_page)
    client.cookies.clear()
    _write(client, db, "POST", "/health/metrics", _health_payload(world), auth, 200 if permission == "health:write" else 403)
    _write(client, db, "POST", "/alerts/report", {"elder_id": own, "summary": "granular report", "note": "observed"},
           auth, 200 if permission == "health:write" else 403)
    _write(client, db, "POST", f"/alerts/{alert_id}/ack", {}, auth, 200 if permission == "alert:handle" else 403)
    _write(client, db, "POST", "/alerts/rules", {
        "code": "GRANULAR-NEW", "name": "granular new rule", "metric_code": "hr", "operator": ">", "threshold": 150,
    }, auth, 200 if permission == "alert:rule" else 403)


@pytest.mark.parametrize("role,key", [("grid", "other_grid"), ("org_admin", "other_community"), ("nurse", "same_grid")])
def test_cross_scope_writes_are_rejected_without_state_actions_or_notifications(
    client, headers, db, accounts, permission_world, role, key,
):
    # 给同一用户写权限以隔离测试“对象范围”，避免403仅来自缺少动作权限。
    user, world = accounts["users"][role], permission_world
    required = {"health:read", "health:write", "alert:read", "alert:handle"}
    user.role.permissions = list(db.scalars(select(Permission).where(Permission.code.in_(required))))
    db.commit()
    auth, elder_id = headers(role), world["elders"][key].id
    pending, referred = world["pending"][key].id, world["referred"][key].id
    for path, body in (
        ("/health/metrics", _health_payload(world, key)),
        ("/health/devices", {"elder_id": elder_id, "device_code": "cross-scope-device"}),
        ("/alerts/report", {"elder_id": elder_id, "summary": "cross-scope-report", "note": "not allowed"}),
    ):
        _write(client, db, "POST", path, body, auth, 403)
    for target, operation, body in (
        (pending, "ack", {}), (pending, "progress", {"note": "not allowed"}),
        (pending, "handle", {"resolution_type": "resolved", "measure": "not allowed"}),
        (referred, "follow-up", {"note": "not allowed", "close": True}),
    ):
        _write(client, db, "POST", f"/alerts/{target}/{operation}", body, auth, 404)
    before = _snapshot(db)
    response = client.post(API + "/health/metrics/batch", headers=auth, json={"items": [_health_payload(world, key)]})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["success"] == 0 and data["results"] == []
    assert data["failed"][0]["code"] == "FORBIDDEN" and data["failed"][0]["index"] == 0
    assert _snapshot(db) == before


@pytest.mark.parametrize("identity", ["unbound_elder", "unassigned_nurse", "missing_org"])
def test_missing_scope_binding_fails_closed(client, headers, db, accounts, permission_world, identity):
    world = permission_world
    if identity == "unbound_elder":
        user, role = accounts["users"]["elder"], "elder"
        accounts["elder"].user_id = None
    elif identity == "unassigned_nurse":
        user, role = accounts["users"]["nurse"], "nurse"
        for order in db.scalars(select(WorkOrder)):
            order.nurse_id = None
    else:
        user, role = accounts["users"]["grid"], "grid"
        user.org_id = None
    db.commit()
    auth = headers(role)
    response = client.get(API + "/alerts", headers=auth)
    assert response.status_code == 200 and response.json()["data"]["total"] == 0
    response = client.get(API + "/alerts/stats", headers=auth)
    assert response.status_code == 200 and response.json()["data"]["total"] == 0
    response = client.get(API + f"/alerts/{world['pending']['own'].id}", headers=auth)
    assert response.status_code == 404
    response = _page_as(client, user, "/alerts")
    assert response.status_code == 200 and "PRIVATE-ALERT-" not in response.text
