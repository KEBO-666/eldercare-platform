"""A2 接口集成测试：档案 CRUD、脱敏、数据范围、二次授权、变更日志、导入导出。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from app.core.security import (
    decrypt_field,
    field_digest,
    mask_address,
    mask_id_card,
    mask_name,
    mask_phone,
)
from app.models import AccessGrant, AuditLog, Elder

ID_CARD = "320101194301019876"
PHONE = "13812340987"
NEW_PHONE = "13911112222"
ADDRESS = "幸福街道幸福社区2号楼202室"


def _body(orgs, **overrides):
    data = {
        "code": "E6001",
        "name": "王奶奶",
        "gender": "female",
        "birth_date": "1942-03-04",
        "id_card": ID_CARD,
        "phone": PHONE,
        "address": ADDRESS,
        "community_org_id": orgs["community1"].id,
        "grid_org_id": orgs["grid1"].id,
        "ability_level": "moderate",
        "difficulty_type": "normal",
        "tags": ["empty_nest"],
    }
    data.update(overrides)
    return data


def test_create_and_detail_are_masked(client, db, orgs, headers):
    resp = client.post("/api/v1/elders", json=_body(orgs), headers=headers("grid"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["code"] == "E6001"
    assert data["name"] == mask_name("王奶奶")
    assert data["id_card"] == mask_id_card(ID_CARD)
    assert data["phone"] == mask_phone(PHONE)
    assert data["address"] == mask_address(ADDRESS)
    assert ID_CARD not in resp.text and PHONE not in resp.text and ADDRESS not in resp.text
    assert "_enc" not in resp.text

    stored = db.query(Elder).filter_by(code="E6001").one()
    assert stored.id_card_enc != ID_CARD
    assert decrypt_field(stored.id_card_enc) == ID_CARD
    assert stored.id_card_hash == field_digest(ID_CARD)
    assert stored.phone_hash == field_digest(PHONE)
    assert stored.address_masked == mask_address(ADDRESS)

    detail = client.get(f"/api/v1/elders/{data['id']}", headers=headers("grid"))
    assert detail.status_code == 200
    assert detail.json()["data"]["id_card"] == mask_id_card(ID_CARD)

    audit = db.query(AuditLog).filter_by(action="create", entity_type="elder", entity_id=stored.id).one()
    assert audit.username == "grid"


def test_list_keyword_and_filters(client, db, orgs, headers):
    created = client.post("/api/v1/elders", json=_body(orgs), headers=headers("grid")).json()["data"]

    resp = client.get("/api/v1/elders", params={"keyword": ID_CARD}, headers=headers("super"))
    items = resp.json()["data"]["items"]
    assert [item["code"] for item in items] == ["E6001"]

    resp = client.get("/api/v1/elders", params={"keyword": PHONE}, headers=headers("super"))
    assert resp.json()["data"]["total"] == 1

    resp = client.get(
        "/api/v1/elders",
        params={"ability_level": "moderate", "tag": "empty_nest", "grid_id": orgs["grid1"].id},
        headers=headers("super"),
    )
    body = resp.json()["data"]
    assert body["total"] >= 1
    assert created["id"] in [item["id"] for item in body["items"]]

    resp = client.get("/api/v1/elders", params={"page": 1, "size": 1}, headers=headers("super"))
    body = resp.json()["data"]
    assert body["page"] == 1 and body["size"] == 1 and len(body["items"]) == 1
    assert body["pages"] == body["total"]


def test_data_scope_grid_and_elder(client, db, orgs, accounts, headers, make_elder):
    outside = make_elder(
        code="E6002",
        name="网格外老人",
        community_org_id=orgs["community2"].id,
        grid_org_id=orgs["grid3"].id,
    )

    resp = client.get("/api/v1/elders", headers=headers("grid"))
    codes = {item["code"] for item in resp.json()["data"]["items"]}
    assert "E6002" not in codes

    resp = client.get(f"/api/v1/elders/{outside.id}", headers=headers("grid"))
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"

    resp = client.get(f"/api/v1/elders/{outside.id}", headers=headers("super"))
    assert resp.status_code == 200

    # 老人账号只能看到绑定自己的档案
    resp = client.get("/api/v1/elders", headers=headers("elder"))
    items = resp.json()["data"]["items"]
    assert [item["code"] for item in items] == ["E0001"]
    assert client.get("/api/v1/elders", headers=headers("elder")).json()["data"]["total"] == 1
    resp = client.get(f"/api/v1/elders/{outside.id}", headers=headers("elder"))
    assert resp.status_code == 403


def test_sensitive_requires_valid_grant(client, db, orgs, accounts, headers):
    elder_id = client.post(
        "/api/v1/elders", json=_body(orgs), headers=headers("grid")
    ).json()["data"]["id"]

    # 无授权 → 403
    resp = client.get(f"/api/v1/elders/{elder_id}/sensitive", headers=headers("super"))
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"

    # 申请授权（10 分钟有效）
    resp = client.post(
        f"/api/v1/elders/{elder_id}/grants",
        json={"fields": ["id_card", "phone"], "reason": "医保报销核验"},
        headers=headers("super"),
    )
    assert resp.status_code == 200, resp.text
    grant = resp.json()["data"]
    assert grant["grant_id"] and grant["fields"] == ["id_card", "phone"]
    expires_at = datetime.fromisoformat(grant["expires_at"])
    assert timedelta(minutes=9) < expires_at - datetime.now() <= timedelta(minutes=10, seconds=5)
    assert db.query(AuditLog).filter_by(action="authorize", entity_id=elder_id).count() == 1

    # 明文返回 + used_at 留痕
    resp = client.get(
        f"/api/v1/elders/{elder_id}/sensitive",
        params={"grant_id": grant["grant_id"]},
        headers=headers("super"),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["id_card"] == ID_CARD and data["phone"] == PHONE
    assert data["fields"]["id_card"] == ID_CARD
    assert db.get(AccessGrant, grant["grant_id"]).used_at is not None

    # 字段覆盖不足 → 403
    resp = client.get(
        f"/api/v1/elders/{elder_id}/sensitive",
        params={"grant_id": grant["grant_id"], "fields": "address"},
        headers=headers("super"),
    )
    assert resp.status_code == 403

    # 授权过期 → 403
    expired = client.post(
        f"/api/v1/elders/{elder_id}/grants",
        json={"fields": ["id_card"], "reason": "过期测试"},
        headers=headers("super"),
    ).json()["data"]
    row = db.get(AccessGrant, expired["grant_id"])
    row.expires_at = datetime.now() - timedelta(seconds=1)
    db.commit()
    resp = client.get(
        f"/api/v1/elders/{elder_id}/sensitive",
        params={"grant_id": expired["grant_id"]},
        headers=headers("super"),
    )
    assert resp.status_code == 403
    assert "过期" in resp.json()["message"]

    # 授权他人 → 403
    foreign = AccessGrant(
        user_id=accounts["users"]["org_admin"].id,
        entity_type="elder",
        entity_id=elder_id,
        fields=["id_card"],
        reason="越权测试",
        expires_at=datetime.now() + timedelta(minutes=10),
    )
    db.add(foreign)
    db.commit()
    resp = client.get(
        f"/api/v1/elders/{elder_id}/sensitive",
        params={"grant_id": foreign.id},
        headers=headers("super"),
    )
    assert resp.status_code == 403

    # 网格员无 elder:grant 权限
    resp = client.post(
        f"/api/v1/elders/{elder_id}/grants",
        json={"fields": ["id_card"], "reason": "无权限"},
        headers=headers("grid"),
    )
    assert resp.status_code == 403


def test_update_writes_changes_and_forbids_code_change(client, db, orgs, headers):
    elder_id = client.post(
        "/api/v1/elders", json=_body(orgs), headers=headers("grid")
    ).json()["data"]["id"]

    resp = client.put(
        f"/api/v1/elders/{elder_id}",
        json={"name": "王婆婆", "phone": NEW_PHONE, "reason": "信息更正"},
        headers=headers("grid"),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["name"] == mask_name("王婆婆")
    assert data["phone"] == mask_phone(NEW_PHONE)

    changes = client.get(f"/api/v1/elders/{elder_id}/changes", headers=headers("grid")).json()["data"]
    assert isinstance(changes, list) and changes
    latest = changes[0]
    assert set(latest["changed_fields"]) == {"name", "phone"}
    assert latest["before"]["name"] == "王奶奶"
    assert latest["after"]["name"] == "王婆婆"
    assert latest["after"]["phone"] == mask_phone(NEW_PHONE)
    dumped = json.dumps(changes, ensure_ascii=False)
    assert NEW_PHONE not in dumped and ID_CARD not in dumped

    stored = db.get(Elder, elder_id)
    assert decrypt_field(stored.phone_enc) == NEW_PHONE
    assert stored.phone_hash == field_digest(NEW_PHONE)

    # 相同值不产生新变更
    count = len(client.get(f"/api/v1/elders/{elder_id}/changes", headers=headers("grid")).json()["data"])
    client.put(f"/api/v1/elders/{elder_id}", json={"name": "王婆婆"}, headers=headers("grid"))
    count_after = len(
        client.get(f"/api/v1/elders/{elder_id}/changes", headers=headers("grid")).json()["data"]
    )
    assert count_after == count

    # 禁止修改 code
    resp = client.put(
        f"/api/v1/elders/{elder_id}", json={"code": "E6999"}, headers=headers("grid")
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "RULE_VIOLATION"


def test_logical_delete(client, db, orgs, accounts, headers):
    elder_id = client.post(
        "/api/v1/elders", json=_body(orgs), headers=headers("grid")
    ).json()["data"]["id"]

    resp = client.delete(f"/api/v1/elders/{elder_id}", headers=headers("super"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["deleted"] is True

    stored = db.get(Elder, elder_id)
    assert stored.is_deleted == 1 and stored.deleted_at is not None
    assert db.query(AuditLog).filter_by(action="delete", entity_id=elder_id).count() == 1

    assert client.get(f"/api/v1/elders/{elder_id}", headers=headers("super")).status_code == 404
    assert client.get(f"/api/v1/elders/{elder_id}/changes", headers=headers("super")).status_code == 404
    assert client.delete(f"/api/v1/elders/{elder_id}", headers=headers("super")).status_code == 404

    codes = {item["code"] for item in client.get("/api/v1/elders", headers=headers("super")).json()["data"]["items"]}
    assert "E6001" not in codes


def test_tags_diseases_medications_assessments(client, db, accounts, headers):
    elder_id = accounts["elder"].id

    resp = client.post(
        f"/api/v1/elders/{elder_id}/tags", json={"tags": ["dementia", "dementia"]}, headers=headers("grid")
    )
    assert resp.status_code == 200
    assert resp.json()["data"].count("dementia") == 1
    resp = client.post(
        f"/api/v1/elders/{elder_id}/tags", json={"tag": "dementia"}, headers=headers("grid")
    )
    tags = resp.json()["data"]
    assert tags.count("dementia") == 1 and "living_alone" in tags

    resp = client.post(
        f"/api/v1/elders/{elder_id}/diseases",
        json={"name": "高血压", "diagnosed_at": "2019-06-01", "note": "长期服药"},
        headers=headers("grid"),
    )
    assert resp.status_code == 200, resp.text
    diseases = client.get(f"/api/v1/elders/{elder_id}/diseases", headers=headers("grid")).json()["data"]
    assert diseases[0]["name"] == "高血压"

    resp = client.post(
        f"/api/v1/elders/{elder_id}/medications",
        json={"name": "氨氯地平", "dosage": "5mg", "frequency": "每日一次"},
        headers=headers("grid"),
    )
    assert resp.status_code == 200, resp.text
    meds = client.get(f"/api/v1/elders/{elder_id}/medications", headers=headers("grid")).json()["data"]
    assert meds[0]["name"] == "氨氯地平"

    resp = client.post(
        f"/api/v1/elders/{elder_id}/assessments",
        json={"level": "severe", "score": 18, "basis": "复评：生活自理能力下降"},
        headers=headers("grid"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["level"] == "severe"
    assert db.get(Elder, elder_id).ability_level == "severe"
    assessments = client.get(
        f"/api/v1/elders/{elder_id}/assessments", headers=headers("grid")
    ).json()["data"]
    assert len(assessments) >= 2
    audit = db.query(AuditLog).filter_by(action="update", entity_id=elder_id).all()
    assert any("能力评估" in (row.detail or "") for row in audit)


def test_import_partial_failure_and_export(client, db, orgs, headers):
    items = [
        _body(orgs, code="E6101", name="导入老人", id_card="320101195202024321", phone="13700009999"),
        {"code": "E0001", "name": "重复编码", "community_org_id": orgs["community1"].id},
        {"code": "E6102", "community_org_id": orgs["community1"].id},
    ]
    resp = client.post("/api/v1/elders/import", json=items, headers=headers("grid"))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["success"] == 1
    assert [item["index"] for item in data["failed"]] == [1, 2]
    assert all(item["reason"] for item in data["failed"])
    assert db.query(Elder).filter_by(code="E6101").count() == 1
    assert db.query(Elder).filter_by(code="E6102").count() == 0

    resp = client.get("/api/v1/elders/export", headers=headers("org_admin"))
    assert resp.status_code == 200, resp.text
    assert resp.content.startswith(b"\xef\xbb\xbf")
    text = resp.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("编号,姓名")
    assert "E6101" in text and "E0001" in text
    assert "导入老人" not in text and "张爷爷" not in text
    assert mask_id_card("320101195202024321") in text
    assert db.query(AuditLog).filter_by(action="export", entity_type="elder").count() == 1


def test_auth_and_permissions(client, accounts, headers):
    assert client.get("/api/v1/elders").status_code == 401
    assert client.get("/api/v1/elders", headers=headers("grid")).status_code == 200
    assert client.delete(f"/api/v1/elders/{accounts['elder'].id}", headers=headers("grid")).status_code == 403
    assert client.get("/api/v1/elders/export", headers=headers("grid")).status_code == 403
    assert client.get("/api/v1/elders/999999", headers=headers("super")).status_code == 404
