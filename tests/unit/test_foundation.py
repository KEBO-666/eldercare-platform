"""基座自检：应用装配、模型建表、状态机、安全工具、审计哈希链。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.core.audit import verify_chain, write_audit
from app.core.exceptions import Conflict
from app.core.security import (
    create_access_token,
    decode_token,
    decrypt_field,
    encrypt_field,
    field_digest,
    hash_password,
    mask_id_card,
    mask_phone,
    verify_password,
)
from app.core.state_machine import ALERT, APPOINTMENT, BILL, COMPLAINT, ORDER
from app.models import Base


def test_app_boots(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "healthy"


def test_all_tables_created(engine):
    names = set(Base.metadata.tables)
    for expected in ("elder", "work_order", "alert", "bill", "audit_log", "user", "role", "permission"):
        assert expected in names
    assert len(names) >= 35


def test_password_and_token_roundtrip():
    hashed = hash_password("Passw0rd!")
    assert verify_password("Passw0rd!", hashed)
    assert not verify_password("wrong", hashed)
    token = create_access_token(7, {"role": "grid"})
    payload = decode_token(token)
    assert payload["sub"] == "7"
    assert payload["role"] == "grid"


def test_field_encryption_and_masking():
    plain = "110101199001011234"
    enc = encrypt_field(plain)
    assert enc != plain
    assert decrypt_field(enc) == plain
    assert field_digest(plain) == field_digest(plain)
    assert field_digest(plain) != field_digest("110101199001011235")
    assert mask_id_card(plain) == "110***********1234"
    assert mask_phone("13812345678") == "138****5678"


def test_state_machines_legal_flow():
    assert APPOINTMENT.next("pending_accept", "accept") == "accepted"
    assert APPOINTMENT.next("accepted", "to_dispatch") == "pending_dispatch"
    assert APPOINTMENT.next("pending_dispatch", "dispatch") == "dispatched"
    assert APPOINTMENT.next("dispatched", "start") == "in_service"
    assert APPOINTMENT.next("in_service", "complete") == "completed"

    assert ORDER.next("pending_accept", "accept") == "accepted"
    assert ORDER.next("accepted", "checkin") == "in_service"
    assert ORDER.next("in_service", "checkout") == "pending_evaluation"
    assert ORDER.next("pending_evaluation", "complete") == "completed"

    assert ALERT.next("pending", "refer") == "referred"
    assert COMPLAINT.next("reviewing", "conclude") == "concluded"
    assert BILL.next("pending_review", "confirm") == "confirmed"


def test_state_machines_reject_illegal_jump():
    with pytest.raises(Conflict):
        APPOINTMENT.next("pending_accept", "complete")
    with pytest.raises(Conflict):
        ORDER.next("completed", "checkin")
    with pytest.raises(Conflict):
        BILL.next("paid", "submit")


def test_audit_hash_chain(db, accounts):
    user = accounts["users"]["super"]
    write_audit(db, user=user, action="create", entity_type="elder", entity_id=1, after={"name": "张爷爷"})
    write_audit(db, user=user, action="export", entity_type="elder", detail="导出档案")
    db.commit()
    result = verify_chain(db)
    assert result["valid"] is True
    assert result["checked"] == 2


def test_audit_chain_detects_tamper(db, accounts):
    user = accounts["users"]["grid"]
    write_audit(db, user=user, action="create", entity_type="elder", entity_id=2)
    write_audit(db, user=user, action="update", entity_type="elder", entity_id=2)
    db.commit()

    from app.models import AuditLog

    first = db.query(AuditLog).order_by(AuditLog.id.asc()).first()
    first.detail = "被篡改"
    db.commit()
    result = verify_chain(db)
    assert result["valid"] is False


def test_rbac_accounts_and_scope(accounts, headers):
    assert accounts["elder"].grid_org_id is not None
    grid_headers = headers("grid")
    assert grid_headers["Authorization"].startswith("Bearer ")


def test_org_hierarchy(orgs):
    assert orgs["grid1"].parent_id == orgs["community1"].id
    assert orgs["community1"].parent_id == orgs["street"].id
    assert datetime.now().year >= 2026
