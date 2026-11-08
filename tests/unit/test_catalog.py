"""服务目录与价格版本化单元测试（B 模块）。"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.exceptions import NotFound, RuleViolation
from app.models import ServiceItem, ServicePriceVersion, WorkOrder
from app.services import catalog_service

BASE = datetime(2026, 10, 15, 9, 0, 0)


def test_create_item_auto_creates_v1_price(db, accounts):
    user = accounts["users"]["org_admin"]
    data = catalog_service.create_item(
        db,
        {
            "code": "S9001",
            "name": "助餐服务",
            "category": "meal",
            "unit_price": "35.50",
            "standard_duration_min": 45,
            "required_skills": ["cook"],
            "applicable_levels": [],
        },
        user=user,
    )
    assert data["code"] == "S9001"
    assert data["current_version"] == 1
    assert data["unit_price"] == 35.5
    version = db.get(ServicePriceVersion, data["current_price_version_id"])
    assert version is not None
    assert version.version == 1
    assert version.unit_price == Decimal("35.50")
    assert version.service_item_id == data["id"]
    assert version.effective_from is not None
    assert version.effective_to is None


def test_create_item_rejects_duplicate_code(db, accounts):
    user = accounts["users"]["org_admin"]
    payload = {"code": accounts["item"].code, "name": "重复编码", "unit_price": "10"}
    with pytest.raises(RuleViolation):
        catalog_service.create_item(db, payload, user=user)


def test_update_item_does_not_touch_price(db, accounts):
    user = accounts["users"]["org_admin"]
    item = accounts["item"]
    before_price = catalog_service.current_price(db, item)
    data = catalog_service.update_item(
        db,
        item.id,
        {"name": "助浴（升级版）", "standard_duration_min": 90, "unit_price": "999.00"},
        user=user,
    )
    assert data["name"] == "助浴（升级版）"
    assert data["standard_duration_min"] == 90
    assert data["unit_price"] == float(before_price)
    assert catalog_service.current_price(db, item) == before_price


def test_add_price_version_closes_previous_and_moves_pointer(db, accounts):
    user = accounts["users"]["org_admin"]
    item = accounts["item"]
    old_version_id = item.current_price_version_id
    start = BASE + timedelta(days=1)

    result = catalog_service.add_price_version(
        db, item.id, unit_price="150.00", effective_from=start, user=user
    )
    new_version = result["price"]
    assert new_version["version"] == 2
    assert new_version["unit_price"] == 150.0

    db.refresh(item)
    assert item.current_price_version_id == new_version["id"]
    old = db.get(ServicePriceVersion, old_version_id)
    assert old.effective_to == start
    assert old.unit_price == Decimal("120.00")
    assert catalog_service.current_price(db, item) == Decimal("150.00")


def test_price_snapshot_keeps_historical_order_amount(db, accounts, make_elder):
    """改价后历史工单金额不变（价格快照）。"""
    user = accounts["users"]["org_admin"]
    item = accounts["item"]
    elder = make_elder()
    version_id, unit_price = catalog_service.price_snapshot(db, item)
    order = WorkOrder(
        code="WO202610150001",
        elder_id=elder.id,
        service_item_id=item.id,
        nurse_id=accounts["staff"].id,
        scheduled_start=BASE,
        price_version_id=version_id,
        unit_price=unit_price,
        quantity=1,
        amount=unit_price,
        community_org_id=elder.community_org_id,
    )
    db.add(order)
    db.commit()

    catalog_service.add_price_version(db, item.id, unit_price="200.00", user=user)
    db.refresh(order)
    db.refresh(item)
    assert order.unit_price == Decimal("120.00")
    assert order.amount == Decimal("120.00")
    assert catalog_service.current_price(db, item) == Decimal("200.00")
    assert catalog_service.price_snapshot(db, item)[1] == Decimal("200.00")


def test_status_and_list_filters(db, accounts):
    user = accounts["users"]["org_admin"]
    created = catalog_service.create_item(
        db, {"code": "S9002", "name": "保洁", "category": "cleaning", "unit_price": "80"}, user=user
    )
    assert catalog_service.set_item_status(db, created["id"], "off", user=user)["status"] == "off"
    with pytest.raises(RuleViolation):
        catalog_service.set_item_status(db, created["id"], "paused", user=user)

    by_category = catalog_service.list_items(db, category="cleaning")
    assert [item.code for item in by_category] == ["S9002"]
    off_items = catalog_service.list_items(db, status="off")
    assert [item.code for item in off_items] == ["S9002"]
    on_items = catalog_service.list_items(db, status="on")
    assert accounts["item"].code in [item.code for item in on_items]
    assert all(item.status == "on" for item in on_items)


def test_price_history_and_missing_item(db, accounts):
    user = accounts["users"]["org_admin"]
    item = accounts["item"]
    catalog_service.add_price_version(db, item.id, unit_price="130.00", user=user)
    versions = catalog_service.list_price_versions(db, item.id)
    assert [row["version"] for row in versions] == [1, 2]
    assert versions[0]["is_current"] is False
    assert versions[1]["is_current"] is True
    assert versions[0]["effective_to"] == versions[1]["effective_from"]

    with pytest.raises(NotFound):
        catalog_service.list_price_versions(db, 999999)
    with pytest.raises(NotFound):
        catalog_service.update_item(db, 999999, {"name": "x"}, user=user)


def test_current_price_falls_back_without_pointer(db, accounts):
    item = db.get(ServiceItem, accounts["item"].id)
    item.current_price_version_id = None
    db.commit()
    assert catalog_service.current_price(db, item) == Decimal("120.00")
