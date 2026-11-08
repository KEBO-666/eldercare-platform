"""服务目录接口集成测试（B 模块）：CRUD、上下架、价格版本化、权限。"""
from __future__ import annotations


def test_create_and_list_catalog_items(client, headers, accounts):
    admin = headers("org_admin")
    resp = client.post(
        "/api/v1/catalog/items",
        json={
            "code": "S8001",
            "name": "康复训练",
            "category": "rehab",
            "unit_price": "88.00",
            "standard_duration_min": 40,
            "required_skills": ["rehab"],
            "applicable_levels": ["mild", "moderate"],
            "subsidy_ratio": 0.4,
        },
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0
    item = body["data"]
    assert item["current_version"] == 1
    assert item["unit_price"] == 88.0
    assert item["current_price_version_id"] is not None
    assert item["status"] == "on"

    listed = client.get("/api/v1/catalog/items", params={"category": "rehab"}, headers=admin)
    assert listed.status_code == 200, listed.text
    payload = listed.json()["data"]
    assert payload["total"] == 1
    assert payload["items"][0]["code"] == "S8001"

    all_items = client.get("/api/v1/catalog/items", headers=admin).json()["data"]
    assert all_items["total"] == 2  # 夹具自带 S0001 + 新建 S8001


def test_edit_item_does_not_change_price(client, headers, accounts):
    admin = headers("org_admin")
    item = accounts["item"]
    resp = client.put(
        f"/api/v1/catalog/items/{item.id}",
        json={"name": "助浴（新名）", "standard_duration_min": 90, "unit_price": "999.00"},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["name"] == "助浴（新名）"
    assert data["standard_duration_min"] == 90
    assert data["unit_price"] == 120.0


def test_toggle_item_status_and_filter(client, headers, accounts):
    admin = headers("org_admin")
    item = accounts["item"]
    resp = client.post(f"/api/v1/catalog/items/{item.id}/status", json={"status": "off"}, headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "off"

    off = client.get("/api/v1/catalog/items", params={"status": "off"}, headers=admin).json()["data"]
    assert [row["code"] for row in off["items"]] == [item.code]

    on = client.get("/api/v1/catalog/items", params={"status": "on"}, headers=admin).json()["data"]
    assert on["total"] == 0

    bad = client.post(f"/api/v1/catalog/items/{item.id}/status", json={"status": "paused"}, headers=admin)
    assert bad.status_code == 400
    assert bad.json()["code"] == "RULE_VIOLATION"


def test_price_version_history_and_current_pointer(client, headers, accounts):
    admin = headers("org_admin")
    item = accounts["item"]
    created = client.post(
        f"/api/v1/catalog/items/{item.id}/prices",
        json={"unit_price": "150.00", "effective_from": "2026-11-01T00:00:00", "remark": "年度调价"},
        headers=admin,
    )
    assert created.status_code == 200, created.text
    data = created.json()["data"]
    assert data["price"]["version"] == 2
    assert data["price"]["unit_price"] == 150.0
    assert data["item"]["current_price_version_id"] == data["price"]["id"]
    assert data["item"]["unit_price"] == 150.0

    history = client.get(f"/api/v1/catalog/items/{item.id}/prices", headers=admin).json()["data"]
    assert [row["version"] for row in history] == [1, 2]
    assert history[0]["effective_to"] == "2026-11-01T00:00:00"
    assert history[0]["is_current"] is False
    assert history[1]["is_current"] is True


def test_catalog_permissions(client, headers):
    denied = client.get("/api/v1/catalog/items", headers=headers("nurse"))
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN"

    denied_write = client.post(
        "/api/v1/catalog/items",
        json={"code": "S0002", "name": "x", "unit_price": "1"},
        headers=headers("grid"),
    )
    assert denied_write.status_code == 403


def test_missing_item_returns_404(client, headers):
    resp = client.get("/api/v1/catalog/items/999999/prices", headers=headers("org_admin"))
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"
