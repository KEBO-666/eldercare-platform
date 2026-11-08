"""A2 单元测试：加密脱敏、摘要检索、数据范围、变更留痕、二次授权、导入导出。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.core.exceptions import Conflict, Forbidden, NotFound, RuleViolation
from app.core.security import decrypt_field, field_digest, mask_id_card, mask_name, mask_phone
from app.models import AccessGrant, ArchiveChangeLog, AuditLog, Elder
from app.schemas.elder import AssessmentCreate, ElderCreate, ElderUpdate, GrantCreate
from app.services import archive_service

ID_CARD = "110101199001011234"
PHONE = "13812345678"
NEW_PHONE = "13900001111"
ADDRESS = "幸福街道幸福社区1号楼101室"


def _create_payload(orgs, **overrides):
    data = {
        "code": "E7001",
        "name": "李奶奶",
        "gender": "female",
        "birth_date": "1940-05-06",
        "id_card": ID_CARD,
        "phone": PHONE,
        "address": ADDRESS,
        "community_org_id": orgs["community1"].id,
        "grid_org_id": orgs["grid1"].id,
        "ability_level": "mild",
        "difficulty_type": "low_income",
        "tags": ["living_alone"],
    }
    data.update(overrides)
    return ElderCreate(**data)


def test_create_encrypts_and_returns_masked_view(db, accounts, orgs):
    user = accounts["users"]["grid"]
    elder = archive_service.create_elder(db, user, _create_payload(orgs))

    stored = db.get(Elder, elder.id)
    assert stored.id_card_enc != ID_CARD
    assert decrypt_field(stored.id_card_enc) == ID_CARD
    assert stored.id_card_hash == field_digest(ID_CARD)
    assert stored.phone_enc != PHONE
    assert stored.phone_hash == field_digest(PHONE)
    assert stored.address_enc != ADDRESS
    assert stored.address_masked.endswith("****")
    assert ADDRESS.startswith(stored.address_masked.removesuffix("****"))

    view = archive_service.detail_view(db, accounts["users"]["super"], elder.id)
    assert view["name"] == mask_name("李奶奶")
    assert view["id_card"] == mask_id_card(ID_CARD)
    assert view["phone"] == mask_phone(PHONE)
    assert view["address"] == stored.address_masked
    assert view["tags"] == ["living_alone"]
    dumped = json.dumps(view, ensure_ascii=False)
    assert ID_CARD not in dumped and PHONE not in dumped and ADDRESS not in dumped
    assert all(not key.endswith("_enc") for key in view)

    log = db.query(ArchiveChangeLog).filter_by(elder_id=elder.id).one()
    assert log.before_json == {}
    assert "name" in log.changed_fields and "id_card" in log.changed_fields
    assert log.after_json["name"] == "李奶奶"
    assert log.after_json["id_card"] == mask_id_card(ID_CARD)

    audit = db.query(AuditLog).filter_by(
        action="create", entity_type="elder", entity_id=elder.id
    ).one()
    assert audit.username == "grid"
    assert ID_CARD not in json.dumps(audit.after_json, ensure_ascii=False)


def test_keyword_search_by_id_card_and_phone_digest(db, accounts, orgs):
    user = accounts["users"]["super"]
    archive_service.create_elder(db, user, _create_payload(orgs))

    items, total = archive_service.list_elders(db, user, keyword=ID_CARD)
    assert total == 1
    assert items[0]["code"] == "E7001"

    items, total = archive_service.list_elders(db, user, keyword=PHONE)
    assert total == 1 and items[0]["code"] == "E7001"

    items, total = archive_service.list_elders(db, user, keyword="李奶")
    assert total == 1 and items[0]["code"] == "E7001"

    items, total = archive_service.list_elders(db, user, keyword="不存在的关键字")
    assert total == 0 and items == []


def test_list_filters_and_scope(db, accounts, orgs):
    grid_user = accounts["users"]["grid"]
    other = archive_service.create_elder(
        db,
        accounts["users"]["grid2"],
        _create_payload(
            orgs,
            code="E7002",
            name="安康老人",
            id_card="110101194505067777",
            phone="13700002222",
            community_org_id=orgs["community2"].id,
            grid_org_id=orgs["grid3"].id,
            ability_level="severe",
            tags=["bedridden"],
        ),
    )
    mine = archive_service.create_elder(db, grid_user, _create_payload(orgs))

    grid_items, _grid_total = archive_service.list_elders(db, grid_user)
    codes = {item["code"] for item in grid_items}
    assert "E7001" in codes and "E7002" not in codes

    with pytest.raises(Forbidden):
        archive_service.detail_view(db, grid_user, other.id)
    # 同网格外仍然可以按 id 过滤（夹具老人 E0001 也在 grid1）
    items, total = archive_service.list_elders(db, grid_user, grid_id=orgs["grid1"].id)
    assert total == 2 and mine.id in {item["id"] for item in items}
    items, total = archive_service.list_elders(db, grid_user, community_id=orgs["community2"].id)
    assert total == 0 and items == []

    # 老人账号只能看到自己绑定的档案
    elder_user = accounts["users"]["elder"]
    items, total = archive_service.list_elders(db, elder_user)
    assert total == 1 and items[0]["code"] == "E0001"
    with pytest.raises(Forbidden):
        archive_service.detail_view(db, elder_user, mine.id)

    # 过滤条件：能力等级 / 标签 / 分页
    items, total = archive_service.list_elders(db, accounts["users"]["super"], ability_level="severe")
    assert total == 1 and items[0]["code"] == "E7002"
    items, total = archive_service.list_elders(db, accounts["users"]["super"], tag="living_alone")
    assert total == 2 and "E7001" in {item["code"] for item in items}
    page1, total = archive_service.list_elders(db, accounts["users"]["super"], page=1, size=1)
    page2, _ = archive_service.list_elders(db, accounts["users"]["super"], page=2, size=1)
    assert total >= 2 and len(page1) == 1 and len(page2) == 1
    assert page1[0]["id"] != page2[0]["id"]


def test_update_writes_change_log_and_rejects_code_change(db, accounts, orgs):
    user = accounts["users"]["grid"]
    elder = archive_service.create_elder(db, user, _create_payload(orgs))

    archive_service.update_elder(
        db, user, elder.id, ElderUpdate(name="李婆婆", ability_level="severe")
    )
    log = (
        db.query(ArchiveChangeLog)
        .filter_by(elder_id=elder.id)
        .order_by(ArchiveChangeLog.id.desc())
        .first()
    )
    assert set(log.changed_fields) == {"name", "ability_level"}
    assert log.before_json["name"] == "李奶奶"
    assert log.after_json["name"] == "李婆婆"
    assert log.after_json["ability_level"] == "severe"
    assert db.get(Elder, elder.id).ability_level == "severe"

    archive_service.update_elder(db, user, elder.id, ElderUpdate(phone=NEW_PHONE))
    stored = db.get(Elder, elder.id)
    assert decrypt_field(stored.phone_enc) == NEW_PHONE
    assert stored.phone_hash == field_digest(NEW_PHONE)
    latest = (
        db.query(ArchiveChangeLog)
        .filter_by(elder_id=elder.id)
        .order_by(ArchiveChangeLog.id.desc())
        .first()
    )
    assert latest.changed_fields == ["phone"]
    assert latest.after_json["phone"] == mask_phone(NEW_PHONE)
    assert NEW_PHONE not in json.dumps(latest.after_json, ensure_ascii=False)

    # 相同值不产生新的变更日志
    count_before = db.query(ArchiveChangeLog).filter_by(elder_id=elder.id).count()
    archive_service.update_elder(db, user, elder.id, ElderUpdate(name="李婆婆"))
    assert db.query(ArchiveChangeLog).filter_by(elder_id=elder.id).count() == count_before

    with pytest.raises(RuleViolation):
        archive_service.update_elder(db, user, elder.id, ElderUpdate(code="E9999"))

    update_audits = db.query(AuditLog).filter_by(
        action="update", entity_type="elder", entity_id=elder.id
    ).count()
    assert update_audits >= 2


def test_delete_is_logical_and_blocks_further_access(db, accounts, orgs):
    user = accounts["users"]["super"]
    elder = archive_service.create_elder(db, user, _create_payload(orgs))

    archive_service.delete_elder(db, user, elder.id)
    stored = db.get(Elder, elder.id)
    assert stored.is_deleted == 1 and stored.deleted_at is not None
    with pytest.raises(NotFound):
        archive_service.detail_view(db, user, elder.id)
    assert db.query(AuditLog).filter_by(
        action="delete", entity_type="elder", entity_id=elder.id
    ).count() == 1
    items, total = archive_service.list_elders(db, user)
    assert all(item["id"] != elder.id for item in items)


def test_grant_and_sensitive_flow(db, accounts, orgs):
    super_user = accounts["users"]["super"]
    elder = archive_service.create_elder(db, super_user, _create_payload(orgs))

    with pytest.raises(Forbidden):
        archive_service.read_sensitive(db, super_user, elder.id, None)

    grant_data = archive_service.create_grant(
        db, super_user, elder.id, GrantCreate(fields=["id_card", "phone"], reason="医保核验")
    )
    expires_at = datetime.fromisoformat(grant_data["expires_at"])
    delta = expires_at - datetime.now()
    assert timedelta(minutes=9) < delta <= timedelta(minutes=10, seconds=5)

    plain = archive_service.read_sensitive(
        db, super_user, elder.id, grant_data["grant_id"]
    )
    assert plain["id_card"] == ID_CARD
    assert plain["phone"] == PHONE
    assert plain["fields"]["id_card"] == ID_CARD
    grant = db.get(AccessGrant, grant_data["grant_id"])
    assert grant.used_at is not None
    assert db.query(AuditLog).filter_by(action="authorize", entity_id=elder.id).count() >= 2

    # 字段覆盖不足
    with pytest.raises(Forbidden):
        archive_service.read_sensitive(
            db, super_user, elder.id, grant_data["grant_id"], fields=["address"]
        )

    # 授权他人
    foreign = AccessGrant(
        user_id=accounts["users"]["org_admin"].id,
        entity_type="elder",
        entity_id=elder.id,
        fields=["id_card"],
        reason="越权尝试",
        expires_at=datetime.now() + timedelta(minutes=10),
    )
    db.add(foreign)
    db.commit()
    with pytest.raises(Forbidden):
        archive_service.read_sensitive(db, super_user, elder.id, foreign.id)

    # 已过期
    expired = AccessGrant(
        user_id=super_user.id,
        entity_type="elder",
        entity_id=elder.id,
        fields=["id_card"],
        reason="过期授权",
        expires_at=datetime.now() - timedelta(minutes=1),
    )
    db.add(expired)
    db.commit()
    with pytest.raises(Forbidden):
        archive_service.read_sensitive(db, super_user, elder.id, expired.id)


def test_tags_dedupe_and_assessment_sync(db, accounts, orgs):
    user = accounts["users"]["grid"]
    elder = archive_service.create_elder(db, user, _create_payload(orgs))

    tags = archive_service.add_tags(db, user, elder.id, ["dementia", "dementia"])
    assert tags.count("dementia") == 1
    tags = archive_service.add_tags(db, user, elder.id, ["dementia", "bedridden"])
    assert tags.count("dementia") == 1 and "bedridden" in tags and "living_alone" in tags

    data = archive_service.add_assessment(
        db, user, elder.id, AssessmentCreate(level="severe", score=18, basis="复评")
    )
    assert data["level"] == "severe"
    stored = db.get(Elder, elder.id)
    assert stored.ability_level == "severe"
    assessments = archive_service.list_assessments(db, user, elder.id)
    assert len(assessments) == 1
    assert db.query(AuditLog).filter_by(action="update", entity_id=elder.id).count() >= 1


def test_import_partial_failure_and_export_masking(db, accounts, orgs):
    user = accounts["users"]["grid"]
    items = [
        {"code": "E7101", "name": "导入老人一", "community_org_id": orgs["community1"].id,
         "grid_org_id": orgs["grid1"].id, "id_card": "110101195001012222", "phone": "13600003333"},
        {"code": "E0001", "name": "重复编码", "community_org_id": orgs["community1"].id},
        {"name": "缺少编码", "community_org_id": orgs["community1"].id},
        {"code": "E7102", "community_org_id": orgs["community1"].id},
        {"code": "E7103", "name": "非法手机", "community_org_id": orgs["community1"].id,
         "phone": "12345"},
    ]
    result = archive_service.import_elders(db, user, items)
    assert result["success"] == 1
    assert [item["index"] for item in result["failed"]] == [1, 2, 3, 4]
    assert db.query(Elder).filter_by(code="E7101").one().id == result["created"][0]["id"]

    content, filename = archive_service.export_csv(db, user)
    assert content.startswith(b"\xef\xbb\xbf")
    assert filename.startswith("elders_") and filename.endswith(".csv")
    text = content.decode("utf-8-sig")
    assert "E7101" in text
    assert "110101195001012222" not in text
    assert mask_id_card("110101195001012222") in text
    assert db.query(AuditLog).filter_by(action="export", entity_type="elder").count() == 1


def test_duplicated_code_conflict(db, accounts, orgs):
    user = accounts["users"]["grid"]
    archive_service.create_elder(db, user, _create_payload(orgs))
    with pytest.raises(Conflict):
        archive_service.create_elder(db, user, _create_payload(orgs, name="重复编码"))
