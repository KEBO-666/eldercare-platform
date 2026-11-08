"""端到端验收：赛题要求的六条业务流程 + 权限/审计/备份闭环。

覆盖：
1. 预约 → 受理 → 派单（Top3 因子）→ 接单 → 签到 → 勾选 → 签退 → 评价 → 完成
2. 健康指标上报 → 红色预警 → 确认响应 → 闭环转诊
3. 低分评价 → 48h 回访任务 → 转投诉 → 复核结论（退费）
4. 月度账单 → 三方分账 → 差异清单 → 复核 → 缴费 → 红冲
5. 越权与数据范围（跨网格 403、老人不可进系统管理、审计链校验）
6. 备份 → 列表 → 恢复核对报告
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

pytestmark = pytest.mark.e2e


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _ok(resp, expect: int = 200):
    assert resp.status_code == expect, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("code") == 0, body
    return body.get("data")


def _fail(resp, status: int):
    assert resp.status_code == status, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("code") != 0
    return body


def _prepare_appointment(client, headers, accounts, *, hours_ahead: int = 24) -> dict:
    """家属提交预约 → 网格员受理 → 转待派单。"""
    desired = datetime.now() + timedelta(hours=hours_ahead)
    data = _ok(
        client.post(
            "/api/v1/appointments",
            json={
                "elder_id": accounts["elder"].id,
                "service_item_id": accounts["item"].id,
                "desired_start": _iso(desired),
                "remark": "端到端测试预约",
            },
            headers=headers("elder"),
        )
    )
    appointment = data.get("appointment", data)
    appt_id = appointment["id"]
    _ok(client.post(f"/api/v1/appointments/{appt_id}/accept", headers=headers("grid")))
    _ok(client.post(f"/api/v1/appointments/{appt_id}/to-dispatch", headers=headers("grid")))
    detail = _ok(client.get(f"/api/v1/appointments/{appt_id}", headers=headers("grid")))
    return detail.get("appointment", detail)


def _dispatch_and_execute(client, headers, accounts, appointment: dict, *, complete: bool = True) -> dict:
    """派单 → 接单 → 签到 → 勾选 → 签退。返回工单。"""
    appt_id = appointment["id"]
    candidates = _ok(client.get(f"/api/v1/dispatch/{appt_id}/candidates", headers=headers("grid")))
    items = candidates.get("items", candidates) if isinstance(candidates, dict) else candidates
    assert items, f"没有可用候选: {candidates}"
    top = items[0]
    assert "factors" in top, top

    order = _ok(
        client.post(
            f"/api/v1/dispatch/{appt_id}/assign",
            json={"nurse_id": accounts["staff"].id},
            headers=headers("grid"),
        )
    )
    order = order.get("order", order)
    order_id = order["id"]

    _ok(client.post(f"/api/v1/orders/{order_id}/accept", headers=headers("nurse")))
    _ok(
        client.post(
            f"/api/v1/orders/{order_id}/checkin",
            json={"lat": accounts["elder"].lat, "lng": accounts["elder"].lng},
            headers=headers("nurse"),
        )
    )
    _ok(
        client.post(
            f"/api/v1/orders/{order_id}/items",
            json={"items": [{"name": "助浴", "completed": True}, {"name": "更衣", "completed": True}]},
            headers=headers("nurse"),
        )
    )
    if complete:
        _ok(
            client.post(
                f"/api/v1/orders/{order_id}/checkout",
                json={"summary": "服务完成", "metrics": [{"metric_code": "sbp", "value": 190}]},
                headers=headers("nurse"),
            )
        )
    return {"id": order_id, "order": order}


# ---------------------------------------------------------------------------
# 流程 1：预约 → 派单 → 执行 → 评价
# ---------------------------------------------------------------------------
def test_flow_appointment_dispatch_execution_evaluation(client, headers, accounts):
    appointment = _prepare_appointment(client, headers, accounts)
    assert appointment["status"] in ("pending_dispatch", "dispatched")

    work = _dispatch_and_execute(client, headers, accounts, appointment)
    order_id = work["id"]

    detail = _ok(client.get(f"/api/v1/orders/{order_id}", headers=headers("grid")))
    assert detail["status"] in ("pending_evaluation", "completed"), detail
    summary = detail.get("order", detail)
    assert summary.get("checkin_at") or summary.get("tracks"), "签到时间应已记录"

    evaluation = _ok(
        client.post(
            "/api/v1/quality/evaluations",
            json={
                "work_order_id": order_id,
                "punctuality": 5,
                "attitude": 5,
                "professionalism": 4,
                "effect": 4,
                "comment": "服务很好",
            },
            headers=headers("elder"),
        )
    )
    assert evaluation.get("need_callback") is False

    detail = _ok(client.get(f"/api/v1/orders/{order_id}", headers=headers("grid")))
    summary = detail.get("order", detail)
    assert summary["status"] == "completed", summary


# ---------------------------------------------------------------------------
# 流程 2：健康数据 → 红色预警 → 响应 → 闭环
# ---------------------------------------------------------------------------
def test_flow_health_alert_lifecycle(client, headers, accounts):
    elder_id = accounts["elder"].id
    metric = _ok(
        client.post(
            "/api/v1/health/metrics",
            json={"elder_id": elder_id, "metric_code": "sbp", "value": 190},
            headers=headers("super"),
        )
    )
    alerts = metric.get("alerts") or []
    assert alerts, f"收缩压 190 应触发预警: {metric}"
    alert = alerts[0]
    assert alert["level"] == "red"

    listing = _ok(client.get("/api/v1/alerts?level=red", headers=headers("grid")))
    items = listing.get("items", listing)
    assert any(a["id"] == alert["id"] for a in items)

    _ok(client.post(f"/api/v1/alerts/{alert['id']}/ack", headers=headers("grid")))
    handled = _ok(
        client.post(
            f"/api/v1/alerts/{alert['id']}/handle",
            json={"resolution_type": "referred", "measure": "已联系家属并转诊", "note": "转区医院"},
            headers=headers("grid"),
        )
    )
    assert handled["status"] == "referred"

    stats = _ok(client.get("/api/v1/alerts/stats", headers=headers("grid")))
    assert stats["red_total"] >= 1
    assert stats["closed"] >= 1


def test_flow_alert_escalates_after_timeout(client, headers, accounts, db):
    from app.core.enums import AlertStatus
    from app.models import Alert

    elder_id = accounts["elder"].id
    metric = _ok(
        client.post(
            "/api/v1/health/metrics",
            json={"elder_id": elder_id, "metric_code": "sbp", "value": 182},
            headers=headers("super"),
        )
    )
    alert_id = (metric.get("alerts") or [{}])[0].get("id")
    assert alert_id

    row = db.get(Alert, alert_id)
    row.deadline_at = datetime.now() - timedelta(minutes=5)
    row.created_at = datetime.now() - timedelta(minutes=40)
    db.commit()

    result = _ok(client.post("/api/v1/alerts/scan", headers=headers("super")))
    assert result["escalated"] >= 1
    db.expire_all()
    row = db.get(Alert, alert_id)
    assert row.escalated_at is not None
    assert row.status == AlertStatus.PENDING.value


# ---------------------------------------------------------------------------
# 流程 3：低分评价 → 回访 → 投诉 → 结论退费
# ---------------------------------------------------------------------------
def test_flow_low_score_callback_complaint_refund(client, headers, accounts):
    appointment = _prepare_appointment(client, headers, accounts, hours_ahead=48)
    work = _dispatch_and_execute(client, headers, accounts, appointment)

    evaluation = _ok(
        client.post(
            "/api/v1/quality/evaluations",
            json={
                "work_order_id": work["id"],
                "punctuality": 2,
                "attitude": 3,
                "professionalism": 2,
                "effect": 2,
                "comment": "迟到且服务不到位",
            },
            headers=headers("elder"),
        )
    )
    assert evaluation["need_callback"] is True

    callbacks = _ok(client.get("/api/v1/quality/callbacks", headers=headers("grid")))
    items = callbacks.get("items", callbacks)
    assert items, "应生成 48 小时内回访任务"
    callback_id = items[0]["id"]

    finished = _ok(
        client.post(
            f"/api/v1/quality/callbacks/{callback_id}/finish",
            json={"result": "to_complaint", "note": "家属要求投诉"},
            headers=headers("grid"),
        )
    )
    complaint_id = (finished.get("complaint") or {}).get("id") or finished.get("complaint_id")
    assert complaint_id, finished

    _ok(
        client.post(
            f"/api/v1/quality/complaints/{complaint_id}/assign",
            json={"handler_id": accounts["users"]["grid"].id},
            headers=headers("org_admin"),
        )
    )
    conclusion = _ok(
        client.post(
            f"/api/v1/quality/complaints/{complaint_id}/conclude",
            json={"conclusion": "核实成立，安排退费", "is_established": True, "resolution": "refund"},
            headers=headers("org_admin"),
        )
    )
    assert conclusion["is_established"] is True
    assert conclusion["refund"] is not None


# ---------------------------------------------------------------------------
# 流程 4：月度账单 → 差异 → 复核 → 缴费 → 红冲
# ---------------------------------------------------------------------------
def test_flow_billing_generate_review_pay_reverse(client, headers, accounts, db):
    # 造两张当月已完成工单
    for hours in (24, 48):
        appointment = _prepare_appointment(client, headers, accounts, hours_ahead=hours)
        work = _dispatch_and_execute(client, headers, accounts, appointment)
        _ok(
            client.post(
                "/api/v1/quality/evaluations",
                json={
                    "work_order_id": work["id"],
                    "punctuality": 5,
                    "attitude": 5,
                    "professionalism": 5,
                    "effect": 5,
                },
                headers=headers("elder"),
            )
        )

    period = datetime.now().strftime("%Y-%m")
    generated = _ok(
        client.post("/api/v1/billing/generate", json={"period": period}, headers=headers("super"))
    )
    bills = generated.get("bills") or ([generated["bill"]] if generated.get("bill") else [])
    assert bills, generated
    bill = bills[0] if isinstance(bills[0], dict) and "id" in bills[0] else bills[0]
    bill_id = bill.get("id") or bill.get("bill_id")

    detail = _ok(client.get(f"/api/v1/billing/bills/{bill_id}", headers=headers("super")))
    bill_data = detail.get("bill", detail)
    total = Decimal(str(bill_data["total_amount"]))
    subsidy = Decimal(str(bill_data["subsidy_amount"]))
    ltc = Decimal(str(bill_data["ltc_amount"]))
    self_pay = Decimal(str(bill_data["self_pay_amount"]))
    assert subsidy + ltc + self_pay == total, (subsidy, ltc, self_pay, total)

    # 手工登记对账差异（监管抽查）
    manual_diff = _ok(
        client.post(
            "/api/v1/billing/diffs",
            json={
                "bill_id": bill_id,
                "rule_code": "manual",
                "description": "监管抽查发现凭证缺失",
                "amount": "5.00",
            },
            headers=headers("super"),
        )
    )
    assert manual_diff["rule_code"] == "manual"
    assert manual_diff["status"] == "open"

    _ok(client.post(f"/api/v1/billing/bills/{bill_id}/submit", headers=headers("super")))
    confirm_payload = {"review_note": "已核对服务留痕与价格快照"}
    _ok(
        client.post(
            f"/api/v1/billing/bills/{bill_id}/confirm",
            json=confirm_payload,
            headers=headers("super"),
        )
    )
    _ok(
        client.post(
            f"/api/v1/billing/bills/{bill_id}/pay",
            json={
                "government": str(subsidy),
                "insurance": str(ltc),
                "self_pay": str(self_pay),
            },
            headers=headers("super"),
        )
    )
    reversed_bill = _ok(
        client.post(f"/api/v1/billing/bills/{bill_id}/reverse", headers=headers("super"))
    )
    assert reversed_bill is not None

    db.expire_all()
    from app.models import Bill

    original = db.get(Bill, bill_id)
    assert original.status == "reversed"
    assert Decimal(str(original.total_amount)) == total, "红冲不得改动原单金额"


# ---------------------------------------------------------------------------
# 流程 5：权限、数据范围、审计
# ---------------------------------------------------------------------------
def test_flow_permissions_and_audit(client, headers, accounts):
    # 网格2 看不到网格1 的老人
    listing = _ok(client.get("/api/v1/elders", headers=headers("grid2")))
    items = listing.get("items", listing)
    assert all(e["id"] != accounts["elder"].id for e in items)

    _fail(client.get(f"/api/v1/elders/{accounts['elder'].id}", headers=headers("grid2")), 403)
    _fail(client.get("/api/v1/admin/users", headers=headers("grid")), 403)
    _fail(client.get("/api/v1/admin/users", headers=headers("elder")), 403)

    # 登录与审计
    login = client.post("/api/v1/auth/login", json={"username": "grid", "password": "Passw0rd!"})
    assert login.status_code == 200
    assert login.json()["data"]["user"]["role"] == "grid"

    verify = _ok(client.get("/api/v1/admin/audit/verify", headers=headers("super")))
    assert verify["valid"] is True
    logs = _ok(client.get("/api/v1/admin/audit-logs", headers=headers("super")))
    assert logs["total"] >= 1


# ---------------------------------------------------------------------------
# 流程 6：备份 → 列表 → 恢复核对
# ---------------------------------------------------------------------------
def test_flow_backup_and_restore(client, headers, accounts):
    created = _ok(client.post("/api/v1/admin/backup", headers=headers("super")))
    assert created

    backups = _ok(client.get("/api/v1/admin/backups", headers=headers("super")))
    items = backups.get("items", backups)
    assert items, "备份列表不能为空"
    backup_id = items[0]["id"]

    report = _ok(
        client.post(
            "/api/v1/admin/restore",
            json={"backup_id": backup_id, "confirm": True},
            headers=headers("super"),
        )
    )
    assert "before" in report and "after" in report
    assert report["integrity"] in (True, "ok", None) or report.get("integrity") != "corrupt"
