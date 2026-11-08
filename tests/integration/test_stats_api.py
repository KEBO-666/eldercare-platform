"""E2 统计看板 API 集成测试（权限、趋势、排行、分布、导出）。"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.enums import AlertLevel, AlertStatus, OrderStatus
from app.models import Alert, AuditLog, Bill, WorkOrder

TODAY = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
DAY = "%Y-%m-%d"
MONTH = TODAY.strftime("%Y-%m")
_SEQ = {"n": 0}


def _code(prefix: str) -> str:
    _SEQ["n"] += 1
    return f"{prefix}{_SEQ['n']:04d}"


def _order(
    db,
    elder,
    item,
    *,
    status=OrderStatus.COMPLETED.value,
    completed_at=TODAY,
    nurse_id=None,
    rating=None,
    community_org_id=None,
    code=None,
):
    order = WorkOrder(
        code=code or _code("WO"),
        elder_id=elder.id,
        service_item_id=item.id,
        nurse_id=nurse_id,
        status=status,
        scheduled_start=completed_at or TODAY,
        completed_at=completed_at,
        community_org_id=community_org_id or elder.community_org_id,
        unit_price=Decimal("120.00"),
        amount=Decimal("120.00"),
        rating=rating,
    )
    db.add(order)
    db.flush()
    return order


def _alert(db, elder, *, level=AlertLevel.BLUE.value, status=AlertStatus.PENDING.value,
           escalated_at=None, resolved_at=None):
    alert = Alert(
        elder_id=elder.id,
        level=level,
        status=status,
        escalated_at=escalated_at,
        resolved_at=resolved_at,
        summary="集成测试预警",
    )
    db.add(alert)
    db.flush()
    return alert


def _bill(db, org_id, *, subsidy="10.00", period=MONTH):
    bill = Bill(
        code=_code("BL"),
        community_org_id=org_id,
        period=period,
        total_amount=Decimal(subsidy),
        subsidy_amount=Decimal(subsidy),
        status="confirmed",
    )
    db.add(bill)
    db.flush()
    return bill


# ---------------------------------------------------------------- 1) 社区看板
def test_community_overview_api(client, db, accounts, headers):
    elder = accounts["elder"]
    item = accounts["item"]
    _order(db, elder, item)
    _order(db, elder, item, status=OrderStatus.IN_SERVICE.value, completed_at=None)
    db.commit()

    resp = client.get("/api/v1/stats/community/overview", headers=headers("street"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["service_count"] == 1
    assert data["pending_orders"] == 1
    assert data["coverage_rate"] == pytest.approx(1.0)
    for key in (
        "pending_alerts",
        "avg_alert_response_minutes",
        "median_alert_response_minutes",
        "satisfaction_avg",
    ):
        assert key in data

    denied = client.get("/api/v1/stats/community/overview", headers=headers("nurse"))
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN"


# ---------------------------------------------------------------- 2) 机构 / 街道
def test_org_and_street_overview_api(client, db, orgs, accounts, headers):
    elder = accounts["elder"]
    staff = accounts["staff"]
    item = accounts["item"]
    _order(db, elder, item, nurse_id=staff.id, rating=5.0)
    _bill(db, orgs["community1"].id, subsidy="100.00")
    _bill(db, orgs["community2"].id, subsidy="20.00")
    _alert(db, elder, level=AlertLevel.RED.value, escalated_at=TODAY)
    _alert(db, elder, level=AlertLevel.RED.value, status=AlertStatus.RESOLVED.value,
           resolved_at=TODAY)
    db.commit()

    org_resp = client.get("/api/v1/stats/org/overview", headers=headers("org_admin"))
    assert org_resp.status_code == 200, org_resp.text
    org_data = org_resp.json()["data"]
    assert org_data["nurse_count"] == 1
    assert org_data["avg_orders_per_nurse"] == pytest.approx(1.0)
    assert org_data["income"]["subsidy"] == 100.0
    assert org_data["income"]["ltc"] == 0.0
    assert org_data["income"]["self_pay"] == 0.0
    assert org_data["income"]["total"] == 100.0
    assert org_data["income"]["subsidy_ratio"] == pytest.approx(1.0)

    street_resp = client.get("/api/v1/stats/street/overview", headers=headers("street"))
    assert street_resp.status_code == 200, street_resp.text
    street_data = street_resp.json()["data"]
    assert [row["name"] for row in street_data["communities"]] == ["幸福社区", "安康社区"]
    assert street_data["alert_escalated"] == 1
    assert street_data["red_alerts"] == 1
    assert street_data["total_subsidy"] == pytest.approx(120.0)

    own_resp = client.get("/api/v1/stats/street/overview", headers=headers("org_admin"))
    assert own_resp.status_code == 200, own_resp.text
    assert len(own_resp.json()["data"]["communities"]) == 1


# ---------------------------------------------------------------- 3) 趋势
def test_trend_api_params_and_zero_fill(client, db, accounts, headers):
    elder = accounts["elder"]
    item = accounts["item"]
    day1 = TODAY - timedelta(days=2)
    _order(db, elder, item, completed_at=day1)
    _order(db, elder, item, completed_at=TODAY)
    db.commit()

    resp = client.get(
        "/api/v1/stats/trend",
        params={
            "metric": "orders",
            "from": day1.strftime(DAY),
            "to": TODAY.strftime(DAY),
            "granularity": "day",
        },
        headers=headers("street"),
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    assert [row["period"] for row in rows] == [
        day1.strftime(DAY),
        (day1 + timedelta(days=1)).strftime(DAY),
        TODAY.strftime(DAY),
    ]
    assert [row["value"] for row in rows] == [1, 0, 1]
    assert rows[1]["mom"] == pytest.approx(-1.0)
    assert rows[0]["last_year"] is None

    default_resp = client.get("/api/v1/stats/trend", headers=headers("street"))
    assert default_resp.status_code == 200, default_resp.text
    assert len(default_resp.json()["data"]) == 30  # 缺省近 30 天

    bad = client.get(
        "/api/v1/stats/trend", params={"metric": "bogus"}, headers=headers("street")
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "RULE_VIOLATION"


# ---------------------------------------------------------------- 4) 排行 / 分布
def test_ranking_and_distribution_api(client, db, orgs, accounts, headers, make_staff):
    elder = accounts["elder"]
    item = accounts["item"]
    staff1 = accounts["staff"]
    staff2 = make_staff()
    _order(db, elder, item, nurse_id=staff1.id, rating=5.0)
    _order(db, elder, item, nurse_id=staff1.id, rating=4.0)
    _order(db, elder, item, nurse_id=staff2.id, community_org_id=orgs["community2"].id)
    db.commit()

    ranking_resp = client.get(
        "/api/v1/stats/ranking",
        params={"type": "nurse", "period": MONTH},
        headers=headers("street"),
    )
    assert ranking_resp.status_code == 200, ranking_resp.text
    rows = ranking_resp.json()["data"]
    assert rows[0]["nurse_id"] == staff1.id
    assert rows[0]["orders"] == 2
    assert rows[0]["rank"] == 1
    assert rows[1]["orders"] == 1

    dist_resp = client.get(
        "/api/v1/stats/distribution",
        params={"type": "ability"},
        headers=headers("street"),
    )
    assert dist_resp.status_code == 200, dist_resp.text
    dist = dist_resp.json()["data"]
    assert sum(row["ratio"] for row in dist) == pytest.approx(1.0, abs=1e-3)
    assert dist[0]["name"] == "能力完好"

    bad = client.get(
        "/api/v1/stats/distribution", params={"type": "bogus"}, headers=headers("street")
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "RULE_VIOLATION"


# ---------------------------------------------------------------- 5) 导出
def test_export_api_bom_and_audit(client, db, orgs, accounts, headers):
    elder = accounts["elder"]
    item = accounts["item"]
    _order(db, elder, item, code="WO-EXPORT-API")
    _bill(db, orgs["community1"].id, subsidy="66.00")
    db.commit()

    params = {"report": "orders", "from": TODAY.strftime(DAY), "to": TODAY.strftime(DAY)}
    resp = client.get("/api/v1/stats/export", params=params, headers=headers("street"))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    text = resp.content.decode("utf-8-sig")
    assert "工单号" in text
    assert "WO-EXPORT-API" in text

    bill_resp = client.get(
        "/api/v1/stats/export",
        params={"report": "billing", "from": TODAY.strftime(DAY), "to": TODAY.strftime(DAY)},
        headers=headers("org_admin"),
    )
    assert bill_resp.status_code == 200, bill_resp.text
    assert "补贴(元)" in bill_resp.content.decode("utf-8-sig")

    audit_count = db.execute(
        select(func.count(AuditLog.id)).where(AuditLog.action == "export")
    ).scalar_one()
    assert audit_count == 2

    denied = client.get("/api/v1/stats/export", params=params, headers=headers("grid"))
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN"

    bad = client.get(
        "/api/v1/stats/export", params={"report": "bogus"}, headers=headers("street")
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "RULE_VIOLATION"
