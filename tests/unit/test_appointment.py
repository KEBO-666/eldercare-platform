"""预约受理单元测试（B 模块）：三重校验、状态流转、取消计费边界。"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from freezegun import freeze_time

from app.core.enums import AbilityLevel
from app.core.exceptions import Conflict, NeedsManual, RuleViolation
from app.models import Appointment, WorkOrder
from app.services import appointment_service, catalog_service, dispatch_service

BASE = datetime(2026, 10, 15, 9, 0, 0)


def _create(db, accounts, start, elder_id=None, item_id=None):
    return appointment_service.create_appointment(
        db,
        service_item_id=item_id or accounts["item"].id,
        desired_start=start,
        elder_id=elder_id,
        user=accounts["users"]["elder"],
    )


def test_create_appointment_pending_accept(db, accounts):
    data = _create(db, accounts, datetime(2026, 10, 16, 10, 0))
    assert data["status"] == "pending_accept"
    assert data["code"].startswith("AP")
    assert data["elder_id"] == accounts["elder"].id
    assert data["service_item_id"] == accounts["item"].id
    assert data["community_org_id"] == accounts["elder"].community_org_id
    assert data["is_chargeable_cancel"] is False
    assert len(data["logs"]) == 1
    assert data["logs"][0]["action"] == "create"
    assert data["logs"][0]["to_status"] == "pending_accept"


def test_duplicate_appointment_needs_manual(db, accounts):
    start = datetime(2026, 10, 16, 10, 0)
    _create(db, accounts, start)
    with pytest.raises(NeedsManual) as exc:
        _create(db, accounts, start + timedelta(hours=1))
    assert "已存在预约单" in exc.value.message
    # ±2 小时以外允许
    second = _create(db, accounts, start + timedelta(hours=3))
    assert second["id"] != 0


def test_duplicate_with_existing_work_order_needs_manual(db, accounts):
    elder = accounts["elder"]
    start = datetime(2026, 10, 16, 10, 0)
    db.add(
        WorkOrder(
            code="WO202610169999",
            elder_id=elder.id,
            service_item_id=accounts["item"].id,
            nurse_id=accounts["staff"].id,
            scheduled_start=start,
            community_org_id=elder.community_org_id,
        )
    )
    db.commit()
    with pytest.raises(NeedsManual) as exc:
        _create(db, accounts, start + timedelta(minutes=30))
    assert "已存在工单" in exc.value.message


def test_ability_level_mismatch_needs_manual(db, accounts, make_elder):
    elder = make_elder(ability_level=AbilityLevel.INTACT.value)
    with pytest.raises(NeedsManual) as exc:
        _create(db, accounts, datetime(2026, 10, 16, 10, 0), elder_id=elder.id)
    assert "能力等级" in exc.value.message


def test_community_scope_mismatch_needs_manual(db, accounts, orgs, make_elder):
    elder = make_elder(community_org_id=orgs["community2"].id, grid_org_id=orgs["grid3"].id)
    with pytest.raises(NeedsManual) as exc:
        _create(db, accounts, datetime(2026, 10, 16, 10, 0), elder_id=elder.id)
    assert "可服务范围" in exc.value.message


def test_off_shelf_item_rejected(db, accounts):
    item = accounts["item"]
    item.status = "off"
    db.commit()
    with pytest.raises(RuleViolation) as exc:
        _create(db, accounts, datetime(2026, 10, 16, 10, 0))
    assert "已下架" in exc.value.message


def test_accept_then_to_dispatch_flow(db, accounts):
    grid = accounts["users"]["grid"]
    created = _create(db, accounts, datetime(2026, 10, 16, 10, 0))
    accepted = appointment_service.accept_appointment(db, created["id"], user=grid)
    assert accepted["status"] == "accepted"
    assert accepted["accepted_by"] == grid.id
    pending = appointment_service.to_dispatch_appointment(db, created["id"], user=grid)
    assert pending["status"] == "pending_dispatch"
    assert [log["action"] for log in pending["logs"]] == ["create", "accept", "to_dispatch"]


def test_illegal_transition_raises_conflict(db, accounts):
    grid = accounts["users"]["grid"]
    created = _create(db, accounts, datetime(2026, 10, 16, 10, 0))
    with pytest.raises(Conflict):
        appointment_service.to_dispatch_appointment(db, created["id"], user=grid)
    appointment_service.accept_appointment(db, created["id"], user=grid)
    with pytest.raises(Conflict):
        appointment_service.accept_appointment(db, created["id"], user=grid)


def test_cancel_119_minutes_is_chargeable(db, accounts):
    with freeze_time("2026-10-15 09:00:00"):
        created = _create(db, accounts, datetime(2026, 10, 15, 10, 59))
        data = appointment_service.cancel_appointment(
            db, created["id"], reason="临时有事", user=accounts["users"]["elder"]
        )
    assert data["status"] == "cancelled"
    assert data["is_chargeable_cancel"] is True
    assert data["cancellation_fee"] == 120.0
    assert data["hours_to_start"] == pytest.approx(119 / 60, abs=1e-3)
    assert data["cancel_reason"] == "临时有事"
    assert data["logs"][-1]["action"] == "cancel"


def test_cancel_121_minutes_is_free(db, accounts):
    with freeze_time("2026-10-15 09:00:00"):
        created = _create(db, accounts, datetime(2026, 10, 15, 11, 1))
        data = appointment_service.cancel_appointment(
            db, created["id"], reason="行程变更", user=accounts["users"]["elder"]
        )
    assert data["is_chargeable_cancel"] is False
    assert data["cancellation_fee"] == 0.0
    assert data["hours_to_start"] == pytest.approx(121 / 60, abs=1e-3)


def test_cancel_requires_reason(db, accounts):
    created = _create(db, accounts, datetime(2026, 10, 16, 10, 0))
    with pytest.raises(RuleViolation):
        appointment_service.cancel_appointment(db, created["id"], reason="  ", user=accounts["users"]["elder"])


def test_cancel_fee_uses_current_price_version(db, accounts):
    user = accounts["users"]["org_admin"]
    item = accounts["item"]
    with freeze_time("2026-10-15 09:00:00"):
        created = _create(db, accounts, datetime(2026, 10, 15, 10, 0))
        catalog_service.add_price_version(db, item.id, unit_price="150.00", user=user)
        data = appointment_service.cancel_appointment(
            db, created["id"], reason="改价后取消", user=accounts["users"]["elder"]
        )
    assert catalog_service.current_price(db, item) == Decimal("150.00")
    assert data["cancellation_fee"] == 150.0


def test_list_appointments_respects_scope(db, accounts, orgs, make_elder):
    elder2 = make_elder(
        community_org_id=orgs["community2"].id,
        grid_org_id=orgs["grid3"].id,
        ability_level=AbilityLevel.MODERATE.value,
    )
    super_user = accounts["users"]["super"]
    appointment_service.create_appointment(
        db,
        service_item_id=accounts["item"].id,
        desired_start=datetime(2026, 10, 16, 9, 0),
        elder_id=elder2.id,
        user=super_user,
    )
    _create(db, accounts, datetime(2026, 10, 16, 15, 0))

    grid_rows, grid_total = appointment_service.list_appointments(db, accounts["users"]["grid"])
    assert grid_total == 1
    assert grid_rows[0]["elder_id"] == accounts["elder"].id

    super_rows, super_total = appointment_service.list_appointments(db, super_user)
    assert super_total == 2

    elder_rows, elder_total = appointment_service.list_appointments(db, accounts["users"]["elder"])
    assert elder_total == 1
    assert elder_rows[0]["elder_id"] == accounts["elder"].id


def test_no_show_flow(db, accounts):
    """dispatched → no_show（8 状态全链路覆盖）。"""
    grid = accounts["users"]["grid"]
    created = _create(db, accounts, datetime(2026, 10, 16, 10, 0))
    appointment_service.accept_appointment(db, created["id"], user=grid)
    appointment_service.to_dispatch_appointment(db, created["id"], user=grid)
    dispatch_service.assign_order(db, created["id"], accounts["staff"].id, user=grid)
    data = appointment_service.no_show_appointment(db, created["id"], user=grid, reason="联系不上老人")
    assert data["status"] == "no_show"
    assert [log["action"] for log in data["logs"]] == [
        "create",
        "accept",
        "to_dispatch",
        "dispatch",
        "no_show",
    ]


def test_appointment_detail_contains_logs_and_available_actions(db, accounts):
    created = _create(db, accounts, datetime(2026, 10, 16, 10, 0))
    detail = appointment_service.get_appointment_detail(db, created["id"], accounts["users"]["elder"])
    assert detail["logs"][0]["action"] == "create"
    assert "accept" in detail["available_actions"]
    assert detail["status_name"] == "待受理"
    stored = db.get(Appointment, created["id"])
    assert stored.status == "pending_accept"
