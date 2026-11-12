"""预约受理接口集成测试（B 模块）：三重校验、状态流转、取消计费、数据范围。"""
from __future__ import annotations

from datetime import datetime

import pytest
from freezegun import freeze_time

from app.core.enums import AbilityLevel


def _post_create(client, headers, accounts, desired_start: str, **overrides):
    payload = {"service_item_id": accounts["item"].id, "desired_start": desired_start}
    payload.update(overrides)
    return client.post("/api/v1/appointments", json=payload, headers=headers("elder"))


def test_create_appointment_success(client, headers, accounts):
    resp = _post_create(client, headers, accounts, "2026-10-16T10:00:00")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["status"] == "pending_accept"
    assert data["code"].startswith("AP")
    assert data["elder_id"] == accounts["elder"].id
    assert data["community_org_id"] == accounts["elder"].community_org_id
    assert len(data["logs"]) == 1


def test_duplicate_appointment_returns_needs_manual(client, headers, accounts):
    first = _post_create(client, headers, accounts, "2026-10-16T10:00:00")
    assert first.status_code == 200, first.text
    second = _post_create(client, headers, accounts, "2026-10-16T11:00:00")
    assert second.status_code == 422
    assert second.json()["code"] == "NEEDS_MANUAL"
    assert "已存在预约单" in second.json()["message"]


def test_ability_mismatch_returns_needs_manual(client, headers, accounts, db):
    elder = accounts["elder"]
    elder.ability_level = AbilityLevel.INTACT.value
    db.commit()
    resp = _post_create(client, headers, accounts, "2026-10-16T10:00:00", elder_id=elder.id)
    assert resp.status_code == 422
    assert resp.json()["code"] == "NEEDS_MANUAL"
    assert "能力等级" in resp.json()["message"]


def test_off_shelf_item_is_rejected(client, headers, accounts):
    admin = headers("org_admin")
    item = accounts["item"]
    toggled = client.post(f"/api/v1/catalog/items/{item.id}/status", json={"status": "off"}, headers=admin)
    assert toggled.status_code == 200
    resp = _post_create(client, headers, accounts, "2026-10-16T10:00:00")
    assert resp.status_code == 400
    assert resp.json()["code"] == "RULE_VIOLATION"
    assert "已下架" in resp.json()["message"]


def test_accept_to_dispatch_and_detail(client, headers, accounts):
    created = _post_create(client, headers, accounts, "2026-10-16T10:00:00").json()["data"]
    appointment_id = created["id"]

    accepted = client.post(f"/api/v1/appointments/{appointment_id}/accept", headers=headers("grid"))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["status"] == "accepted"

    pending = client.post(f"/api/v1/appointments/{appointment_id}/to-dispatch", headers=headers("grid"))
    assert pending.status_code == 200, pending.text
    assert pending.json()["data"]["status"] == "pending_dispatch"

    detail = client.get(f"/api/v1/appointments/{appointment_id}", headers=headers("grid"))
    assert detail.status_code == 200
    data = detail.json()["data"]
    assert [log["action"] for log in data["logs"]] == ["create", "accept", "to_dispatch"]
    assert data["status_name"] == "待派单"

    illegal = client.post(f"/api/v1/appointments/{appointment_id}/accept", headers=headers("grid"))
    assert illegal.status_code == 409
    assert illegal.json()["code"] == "INVALID_TRANSITION"


def test_cancel_119_minutes_chargeable(client, headers, accounts):
    with freeze_time("2026-10-15 09:00:00"):
        created = _post_create(client, headers, accounts, "2026-10-15T10:59:00").json()["data"]
        resp = client.post(
            f"/api/v1/appointments/{created['id']}/cancel",
            json={"reason": "临时有事"},
            headers=headers("elder"),
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "cancelled"
    assert data["is_chargeable_cancel"] is True
    assert data["cancellation_fee"] == 120.0
    assert data["hours_to_start"] == pytest.approx(119 / 60, abs=1e-3)


def test_cancel_121_minutes_free(client, headers, accounts):
    with freeze_time("2026-10-15 09:00:00"):
        created = _post_create(client, headers, accounts, "2026-10-15T11:01:00").json()["data"]
        resp = client.post(
            f"/api/v1/appointments/{created['id']}/cancel",
            json={"reason": "行程调整"},
            headers=headers("elder"),
        )
    data = resp.json()["data"]
    assert data["is_chargeable_cancel"] is False
    assert data["cancellation_fee"] == 0.0


def test_cancel_requires_reason(client, headers, accounts):
    created = _post_create(client, headers, accounts, "2026-10-16T10:00:00").json()["data"]
    resp = client.post(
        f"/api/v1/appointments/{created['id']}/cancel",
        json={"reason": ""},
        headers=headers("elder"),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "RULE_VIOLATION"


def test_list_appointments_filters_and_pagination(client, headers, accounts):
    for start in ("2026-10-20T08:00:00", "2026-10-21T12:00:00", "2026-10-22T16:00:00"):
        assert _post_create(client, headers, accounts, start).status_code == 200

    listed = client.get("/api/v1/appointments", params={"page": 1, "size": 2}, headers=headers("grid"))
    assert listed.status_code == 200, listed.text
    data = listed.json()["data"]
    assert data["total"] == 3
    assert len(data["items"]) == 2
    assert data["pages"] == 2

    filtered = client.get(
        "/api/v1/appointments",
        params={"elder_id": accounts["elder"].id, "status": "pending_accept"},
        headers=headers("grid"),
    ).json()["data"]
    assert filtered["total"] == 3

    date_filtered = client.get(
        "/api/v1/appointments",
        params={"date_from": "2026-10-21T00:00:00", "date_to": "2026-10-21T23:59:59"},
        headers=headers("grid"),
    ).json()["data"]
    assert date_filtered["total"] == 1


def test_appointment_scope_and_permission(client, headers, accounts, orgs, make_elder):
    elder2 = make_elder(
        community_org_id=orgs["community2"].id,
        grid_org_id=orgs["grid3"].id,
        ability_level=AbilityLevel.MODERATE.value,
    )
    created = client.post(
        "/api/v1/appointments",
        json={
            "service_item_id": accounts["item"].id,
            "desired_start": "2026-10-22T10:00:00",
            "elder_id": elder2.id,
        },
        headers=headers("super"),
    )
    assert created.status_code == 200, created.text
    other_id = created.json()["data"]["id"]

    # 社区网格员看不到安康社区的预约
    detail = client.get(f"/api/v1/appointments/{other_id}", headers=headers("grid"))
    assert detail.status_code == 403
    assert detail.json()["code"] == "FORBIDDEN"

    grid_list = client.get("/api/v1/appointments", headers=headers("grid")).json()["data"]
    assert grid_list["total"] == 0

    super_list = client.get("/api/v1/appointments", headers=headers("super")).json()["data"]
    assert super_list["total"] == 1

    # 护理员无 appointment:read 权限
    denied = client.get("/api/v1/appointments", headers=headers("nurse"))
    assert denied.status_code == 403


def test_elder_only_sees_own_appointments(client, headers, accounts, orgs, make_elder):
    _post_create(client, headers, accounts, "2026-10-23T10:00:00")
    elder2 = make_elder(
        community_org_id=orgs["community1"].id,
        grid_org_id=orgs["grid1"].id,
        ability_level=AbilityLevel.MODERATE.value,
    )
    created = client.post(
        "/api/v1/appointments",
        json={
            "service_item_id": accounts["item"].id,
            "desired_start": "2026-10-23T15:00:00",
            "elder_id": elder2.id,
        },
        headers=headers("super"),
    )
    assert created.status_code == 200, created.text

    own = client.get("/api/v1/appointments", headers=headers("elder")).json()["data"]
    assert own["total"] == 1
    assert own["items"][0]["elder_id"] == accounts["elder"].id


def test_cycle_rule_and_remark_persisted(client, headers, accounts):
    resp = _post_create(
        client,
        headers,
        accounts,
        "2026-10-24T10:00:00",
        remark="每周一次",
        cycle_rule="weekly",
        address="幸福街道幸福社区 1 号楼 101",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["remark"] == "每周一次"
    assert data["cycle_rule"] == "weekly"
    assert data["address_masked"].endswith("****")


def test_past_appointment_still_allowed_for_recording(client, headers, accounts):
    """演示环境允许补录历史时段预约（仅做冲突校验）。"""
    start = datetime(2026, 9, 1, 9, 0, 0).isoformat()
    resp = _post_create(client, headers, accounts, start)
    assert resp.status_code == 200, resp.text
