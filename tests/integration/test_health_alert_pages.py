"""真实页面链接、权限与闭环计数保持接口一致。"""
from __future__ import annotations

import json
import re

from app.models import AlertRule
from app.services import alert_service


def login(client, username):
    client.cookies.clear()
    response = client.post("/login", data={"username": username, "password": "Passw0rd!", "next": "/"}, follow_redirects=False)
    assert response.status_code == 303


def report(db, accounts):
    return alert_service.report_alert(db, accounts["users"]["super"], elder_id=accounts["elder"].id,
                                      summary="上门发现身体不适", note="已联系家属协助核实", level="yellow")


def test_referral_page_embeds_real_timeline_and_does_not_claim_closed(client, db, accounts):
    item = report(db, accounts)
    alert_service.ack_alert(db, accounts["users"]["grid"], item["id"])
    alert_service.handle_alert(db, accounts["users"]["grid"], item["id"], resolution_type="referred", measure="已联系接收机构并交接")
    login(client, "grid")
    response = client.get(f"/alerts/{item['id']}")
    assert response.status_code == 200
    snapshot = json.loads(re.search(r'<script type="application/json" id="alert-snapshot">(.*?)</script>', response.text, re.S).group(1))
    assert snapshot["lifecycle_status"] == "awaiting_followup" and snapshot["closed"] is False
    assert [a["action_type"] for a in snapshot["actions"]].count("ack") == 1
    assert "data-alert-operation=\"close\"" in response.text
    assert "已联系接收机构并交接" in response.text
    board = client.get("/alerts")
    assert board.status_code == 200
    assert f'href="/alerts/{item["id"]}"' in board.text
    assert "转诊待随访" in board.text
    assert "已处置 / 误报 / 已完成随访的转诊" in board.text


def test_alert_page_scope_and_login(client, db, accounts):
    item = report(db, accounts)
    response = client.get(f"/alerts/{item['id']}", follow_redirects=False)
    assert response.status_code == 303
    login(client, "grid2")
    response = client.get(f"/alerts/{item['id']}")
    assert response.status_code == 404
    assert "上门发现身体不适" not in response.text


def test_health_page_uses_enabled_rules_and_persistent_feedback(client, db, accounts):
    db.add(AlertRule(code="CUSTOM_SILENCE", name="自定义静默检测", rule_type="silence", silence_hours=12,
                     level="blue", cooldown_minutes=15, escalate_minutes=20))
    db.commit()
    login(client, "super")
    response = client.get(f"/monitoring/{accounts['elder'].id}")
    assert response.status_code == 200
    assert "12小时无数据" in response.text
    assert "连续 3 天数据缺失" not in response.text
    for selector in ("metric-feedback", "report-form", "report-feedback", "/static/js/health-alert.js"):
        assert selector in response.text


def test_readonly_health_page_has_no_write_form(client, accounts):
    login(client, "grid")
    response = client.get(f"/monitoring/{accounts['elder'].id}")
    assert response.status_code == 200
    assert 'id="metric-form"' not in response.text
    assert 'id="report-form"' not in response.text
