"""E1 模块集成测试：账单生成 / 提交 / 复核 / 缴费 / 红冲 / 差异 / 导出 / 权限。"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.enums import OrderStatus
from app.models import AuditLog, PaymentRecord, ServiceItem, WorkOrder

API = "/api/v1/billing"
BASE_TIME = datetime(2026, 10, 15, 9, 0, 0)
PERIOD = "2026-10"


@pytest.fixture()
def make_order(db, accounts):
    counter = {"n": 0}

    def _make(**kwargs) -> WorkOrder:
        counter["n"] += 1
        defaults = dict(
            code=f"IB{counter['n']:04d}",
            elder_id=accounts["elder"].id,
            service_item_id=accounts["item"].id,
            nurse_id=accounts["staff"].id,
            status=OrderStatus.COMPLETED,
            scheduled_start=BASE_TIME,
            scheduled_end=BASE_TIME + timedelta(hours=1),
            price_version_id=accounts["price"].id,
            unit_price=Decimal("120.00"),
            quantity=1,
            amount=Decimal("120.00"),
            community_org_id=accounts["elder"].community_org_id,
        )
        defaults.update(kwargs)
        order = WorkOrder(**defaults)
        db.add(order)
        db.flush()
        return order

    return _make


def _generate(client, headers) -> dict:
    resp = client.post(f"{API}/generate", json={"period": PERIOD}, headers=headers("super"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0
    return body["data"]


def _submit_and_confirm(client, headers, bill_id: int) -> dict:
    resp = client.post(f"{API}/bills/{bill_id}/submit", headers=headers("super"))
    assert resp.status_code == 200, resp.text
    resp = client.post(f"{API}/bills/{bill_id}/confirm", json={}, headers=headers("street"))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_generate_bill_and_full_lifecycle(client, db, headers, accounts, make_order):
    order = make_order()

    data = _generate(client, headers)
    assert data["period"] == PERIOD
    assert data["details_count"] == 1
    assert data["diffs_count"] == 0
    bill = data["bill"]
    assert bill["status"] == "draft"
    assert bill["code"].startswith("BILL-202610-")
    assert bill["total_amount"] == pytest.approx(120.0)
    assert bill["subsidy_amount"] == pytest.approx(60.0)
    assert bill["ltc_amount"] == pytest.approx(24.0)
    assert bill["self_pay_amount"] == pytest.approx(36.0)
    assert bill["needs_review"] is False

    detail = client.get(f"{API}/bills/{bill['id']}", headers=headers("super")).json()["data"]
    assert detail["details_count"] == 1
    assert detail["diffs"] == []
    assert detail["details"][0]["work_order_id"] == order.id
    assert detail["details"][0]["work_order_code"] == order.code
    assert detail["details"][0]["subsidy_amount"] == pytest.approx(60.0)

    submitted = client.post(f"{API}/bills/{bill['id']}/submit", headers=headers("super"))
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["status"] == "pending_review"
    assert submitted.json()["data"]["available_actions"] == ["confirm", "reject"]

    confirmed_resp = client.post(
        f"{API}/bills/{bill['id']}/confirm", json={}, headers=headers("street")
    )
    assert confirmed_resp.status_code == 200, confirmed_resp.text
    confirmed = confirmed_resp.json()["data"]
    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_by"] == accounts["users"]["street"].id

    pay_body = {
        "government": confirmed["subsidy_amount"],
        "insurance": confirmed["ltc_amount"],
        "self_pay": confirmed["self_pay_amount"],
    }
    paid_resp = client.post(f"{API}/bills/{bill['id']}/pay", json=pay_body, headers=headers("super"))
    assert paid_resp.status_code == 200, paid_resp.text
    paid = paid_resp.json()["data"]
    assert paid["status"] == "paid"
    assert len(paid["payments"]) == 3
    assert sum(item["amount"] for item in paid["payments"]) == pytest.approx(120.0)
    assert {item["payer_type"] for item in paid["payments"]} == {"government", "insurance", "self"}

    # 已缴费账单不能再次红冲前跳状态
    second_pay = client.post(f"{API}/bills/{bill['id']}/pay", json=pay_body, headers=headers("super"))
    assert second_pay.status_code == 409

    assert db.query(PaymentRecord).count() == 3


def test_pay_amount_mismatch_rejected(client, db, headers, make_order):
    make_order()
    data = _generate(client, headers)
    bill_id = data["bill"]["id"]
    _submit_and_confirm(client, headers, bill_id)

    wrong = client.post(
        f"{API}/bills/{bill_id}/pay",
        json={"government": 61.0, "insurance": 24.0, "self_pay": 36.0},
        headers=headers("super"),
    )
    assert wrong.status_code == 400
    assert wrong.json()["code"] == "RULE_VIOLATION"
    assert db.query(PaymentRecord).count() == 0

    correct = client.post(
        f"{API}/bills/{bill_id}/pay",
        json={"government": 60.0, "insurance": 24.0, "self_pay": 36.0},
        headers=headers("super"),
    )
    assert correct.status_code == 200
    assert correct.json()["data"]["status"] == "paid"


def test_pay_single_payer_mode(client, db, headers, make_order):
    """兼容前端"逐个付款方登记"：累计到足额才置为已缴费。"""
    make_order()
    data = _generate(client, headers)
    bill_id = data["bill"]["id"]
    _submit_and_confirm(client, headers, bill_id)

    first = client.post(
        f"{API}/bills/{bill_id}/pay",
        json={"payer_type": "government", "amount": 60.0, "remark": "街道代缴"},
        headers=headers("super"),
    )
    assert first.status_code == 200, first.text
    assert first.json()["data"]["status"] == "confirmed"

    second = client.post(
        f"{API}/bills/{bill_id}/pay",
        json={"payer_type": "insurance", "amount": 24.0},
        headers=headers("super"),
    )
    assert second.json()["data"]["status"] == "confirmed"

    third = client.post(
        f"{API}/bills/{bill_id}/pay",
        json={"payer_type": "self", "amount": 36.0},
        headers=headers("super"),
    )
    assert third.json()["data"]["status"] == "paid"
    assert db.query(PaymentRecord).count() == 3


def test_confirm_blocked_when_diff_ratio_over_threshold(client, db, headers, accounts, make_order):
    item = ServiceItem(
        code="S9901",
        name="自费陪诊",
        category="other",
        pricing_mode="per_time",
        standard_duration_min=60,
        is_subsidized=False,
        subsidy_ratio=0.0,
    )
    db.add(item)
    db.flush()
    make_order(
        service_item_id=item.id,
        unit_price=Decimal("1000.00"),
        quantity=10,
        amount=Decimal("10000.00"),
    )
    make_order(
        status=OrderStatus.IN_SERVICE,
        service_item_id=item.id,
        unit_price=Decimal("110.00"),
        quantity=1,
        amount=Decimal("110.00"),
        scheduled_start=BASE_TIME + timedelta(hours=3),
        scheduled_end=BASE_TIME + timedelta(hours=4),
    )

    data = _generate(client, headers)
    assert data["bill"]["needs_review"] is True
    assert data["bill"]["diff_ratio"] == pytest.approx(0.011, rel=1e-4)
    assert data["diffs_count"] == 1

    bill_id = data["bill"]["id"]
    assert client.post(f"{API}/bills/{bill_id}/submit", headers=headers("super")).status_code == 200

    blocked = client.post(f"{API}/bills/{bill_id}/confirm", json={}, headers=headers("street"))
    assert blocked.status_code == 400
    assert blocked.json()["code"] == "RULE_VIOLATION"

    passed = client.post(
        f"{API}/bills/{bill_id}/confirm",
        json={"review_note": "已核实：未完成工单不计费，差异可接受"},
        headers=headers("street"),
    )
    assert passed.status_code == 200, passed.text
    assert passed.json()["data"]["status"] == "confirmed"


def test_diff_list_and_handle(client, db, headers, make_order):
    unfinished = make_order(
        status=OrderStatus.IN_SERVICE,
        scheduled_start=BASE_TIME + timedelta(hours=3),
        scheduled_end=BASE_TIME + timedelta(hours=4),
    )

    resp = client.get(f"{API}/diffs", headers=headers("street"))
    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 0

    _generate(client, headers)

    listed = client.get(f"{API}/diffs", params={"status": "open"}, headers=headers("street"))
    assert listed.status_code == 200
    payload = listed.json()["data"]
    assert payload["total"] == 1
    diff = payload["items"][0]
    assert diff["rule_code"] == "unfinished"
    assert diff["work_order_id"] == unfinished.id
    assert diff["amount"] == pytest.approx(120.0)
    assert diff["rule_name"] == "未完成计费"

    empty = client.post(
        f"{API}/diffs/{diff['id']}/handle",
        json={"status": "explained", "explanation": ""},
        headers=headers("street"),
    )
    assert empty.status_code == 400

    handled = client.post(
        f"{API}/diffs/{diff['id']}/handle",
        json={"status": "explained", "explanation": "护理员漏签退，已线下核实"},
        headers=headers("street"),
    )
    assert handled.status_code == 200, handled.text
    assert handled.json()["data"]["status"] == "explained"
    assert handled.json()["data"]["handler_id"] == accounts_user_id(db, "street")

    after = client.get(
        f"{API}/diffs", params={"status": "explained"}, headers=headers("street")
    ).json()["data"]
    assert after["total"] == 1


def accounts_user_id(db, username: str) -> int:
    from app.models import User

    return db.query(User).filter(User.username == username).one().id


def test_reverse_creates_negative_bill_and_keeps_original(client, db, headers, make_order):
    make_order()
    data = _generate(client, headers)
    bill_id = data["bill"]["id"]
    original = _submit_and_confirm(client, headers, bill_id)
    original_total = original["total_amount"]

    resp = client.post(
        f"{API}/bills/{bill_id}/reverse",
        json={"reason": "老人投诉重复计费"},
        headers=headers("super"),
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()["data"]
    assert payload["status"] == "reversed"
    assert payload["total_amount"] == pytest.approx(original_total)

    red = payload["reverse_bill"]
    assert red["total_amount"] == pytest.approx(-original_total)
    assert red["subsidy_amount"] == pytest.approx(-original["subsidy_amount"])
    assert red["status"] == "confirmed"
    assert red["code"].endswith("-R")

    detail = client.get(f"{API}/bills/{bill_id}", headers=headers("super")).json()["data"]
    assert detail["total_amount"] == pytest.approx(original_total)
    assert detail["status"] == "reversed"
    assert detail["details_count"] == 1

    assert db.query(AuditLog).filter(AuditLog.action == "refund").count() == 1

    # 已红冲账单不可再次红冲
    again = client.post(f"{API}/bills/{bill_id}/reverse", json={}, headers=headers("super"))
    assert again.status_code == 409


def test_export_xlsx(client, db, headers, make_order):
    make_order()
    data = _generate(client, headers)
    bill_id = data["bill"]["id"]

    resp = client.get(f"{API}/bills/{bill_id}/export", headers=headers("street"))
    assert resp.status_code == 200
    assert resp.content[:2] == b"PK"
    assert "spreadsheetml" in resp.headers["content-type"]
    assert ".xlsx" in resp.headers["content-disposition"]
    assert db.query(AuditLog).filter(AuditLog.action == "export").count() == 1

    # 导出不存在账单
    missing = client.get(f"{API}/bills/999999/export", headers=headers("street"))
    assert missing.status_code == 404


def test_generate_empty_period(client, headers):
    resp = client.post(f"{API}/generate", json={"period": "2020-01"}, headers=headers("super"))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["bill"] is None
    assert data["details_count"] == 0


def test_generate_invalid_period(client, headers):
    resp = client.post(f"{API}/generate", json={"period": "2026/10"}, headers=headers("super"))
    assert resp.status_code == 400
    assert resp.json()["code"] == "BAD_REQUEST"
    assert "账期" in resp.json()["message"]


def test_permissions(client, headers, make_order):
    make_order()

    assert client.post(
        f"{API}/generate", json={"period": PERIOD}, headers=headers("elder")
    ).status_code == 403
    assert client.get(f"{API}/bills", headers=headers("nurse")).status_code == 403
    assert client.get(f"{API}/bills").status_code == 401
    assert client.get(f"{API}/bills", headers=headers("elder")).status_code == 200

    data = _generate(client, headers)
    bill_id = data["bill"]["id"]
    # 老人只读：不能复核、不能缴费、不能红冲
    assert client.post(
        f"{API}/bills/{bill_id}/confirm", json={}, headers=headers("elder")
    ).status_code == 403
    assert client.post(
        f"{API}/bills/{bill_id}/pay",
        json={"payer_type": "self", "amount": 36.0},
        headers=headers("elder"),
    ).status_code == 403
    assert client.post(
        f"{API}/bills/{bill_id}/reverse", json={}, headers=headers("elder")
    ).status_code == 403


def test_bill_list_filters_and_scope(client, db, headers, make_order):
    make_order()
    _generate(client, headers)

    listed = client.get(f"{API}/bills", params={"period": PERIOD}, headers=headers("super"))
    assert listed.status_code == 200
    data = listed.json()["data"]
    assert data["total"] == 1
    assert data["page"] == 1 and data["size"] == 20
    assert data["items"][0]["period"] == PERIOD

    other = client.get(f"{API}/bills", params={"period": "2020-01"}, headers=headers("super"))
    assert other.json()["data"]["total"] == 0

    assert client.get(f"{API}/bills/999999", headers=headers("super")).status_code == 404
