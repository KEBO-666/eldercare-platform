"""F 模块页面冒烟测试：登录保护 / 角色菜单 / 页面渲染（不依赖其它模块接口）。

覆盖：
1. /login 可访问且包含演示账号提示（离线可用的本地静态资源）；
2. 未登录访问任一页面 → 303 跳转 /login?next=...；
3. 页面登录写 access_token Cookie，登录后首页 200 且包含当前角色菜单；
4. 不同角色导航菜单差异（老人/护理员/网格员/超管）；
5. 关键业务页面渲染 200（老人档案、预约、派单、工单、预警、账单、统计、管理页）；
6. 系统健康检查 /health 仍返回 JSON（页面路由未占用该路径）；
7. 找不到数据时返回友好错误页而不是 500。
"""
from __future__ import annotations

import pytest

TEST_PASSWORD = "Passw0rd!"

PROTECTED_PAGES = [
    "/",
    "/elders",
    "/appointments",
    "/appointments/new",
    "/orders",
    "/alerts",
    "/quality",
    "/billing",
    "/stats",
    "/admin/users",
]


def page_login(client, username: str, password: str = TEST_PASSWORD):
    """通过页面表单登录（会写入 access_token Cookie）。"""
    client.cookies.clear()
    return client.post(
        "/login",
        data={"username": username, "password": password, "next": "/"},
        follow_redirects=False,
    )


def test_login_page_renders(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    body = resp.text
    assert "社区智慧养老服务与健康监测平台" in body
    assert "Admin@123" in body                       # 演示账号提示
    assert "/static/css/app.css" in body             # 本地静态资源，不依赖外网 CDN
    assert "cdn." not in body


def test_local_static_assets_are_served(client):
    for path in ("/static/css/app.css", "/static/js/app.js", "/static/js/charts.js", "/static/vendor/echarts.min.js"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert len(resp.content) > 500, path


def test_unauthenticated_pages_redirect_to_login(client):
    for path in PROTECTED_PAGES:
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303, f"{path} 未做登录保护"
        assert resp.headers["location"].startswith("/login"), path


def test_system_health_endpoint_not_shadowed(client):
    """基座自检 GET /health 必须仍返回 JSON（页面健康路由已改为 /monitoring）。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["code"] == 0
    assert payload["data"]["status"] == "healthy"


def test_login_sets_cookie_and_dashboard_renders(client, accounts):
    resp = page_login(client, "super")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "access_token" in resp.cookies

    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "系统超级管理员" in body                  # 顶部用户角色
    for keyword in ("首页看板", "老人档案", "预警看板", "统计看板", "用户管理", "系统配置", "退出"):
        assert keyword in body, f"超管首页缺少菜单关键字：{keyword}"


def test_role_menus_differ(client, accounts):
    # 网格员：有派单与统计，没有系统配置/组织管理
    resp = page_login(client, "grid")
    assert resp.status_code == 303
    body = client.get("/").text
    assert "社区网格员" in body
    assert "智能派单" in body
    assert "统计看板" in body
    assert "系统配置" not in body
    assert "组织管理" not in body

    # 护理员：有我的工单、健康录入，没有派单/账单/统计
    resp = page_login(client, "nurse")
    assert resp.status_code == 303
    body = client.get("/").text
    assert "护理员" in body
    assert "我的工单" in body
    assert "健康录入" in body
    assert "智能派单" not in body
    assert "统计看板" not in body
    assert "费用账单" not in body

    # 老人/家属：我的预约、我的账单、我的健康，没有老人档案库与系统管理
    resp = page_login(client, "elder")
    assert resp.status_code == 303
    body = client.get("/").text
    assert "老人/家属" in body
    assert "我的预约" in body
    assert "我的账单" in body
    assert "我的健康" in body
    assert "老人档案" not in body
    assert "用户管理" not in body


@pytest.mark.parametrize(
    "path",
    [
        "/elders",
        "/appointments",
        "/appointments/new",
        "/orders",
        "/alerts",
        "/quality",
        "/billing",
        "/stats",
        "/admin/users",
        "/admin/orgs",
        "/admin/audit",
        "/admin/backup",
        "/admin/config",
    ],
)
def test_business_pages_render_for_super(client, accounts, path):
    assert page_login(client, "super").status_code == 303
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} 渲染失败：{resp.status_code}"
    assert "页面渲染失败" not in resp.text
    assert "页面数据加载异常" not in resp.text


def test_elder_list_and_detail_are_masked(client, accounts):
    assert page_login(client, "super").status_code == 303
    resp = client.get("/elders")
    assert resp.status_code == 200
    assert "老人档案库" in resp.text
    assert "默认脱敏展示" in resp.text

    elder = accounts["elder"]
    resp = client.get(f"/elders/{elder.id}")
    assert resp.status_code == 200
    body = resp.text
    assert "档案详情" in body
    assert "二次授权" in body                     # 敏感字段二次授权入口
    assert elder.code in body


def test_monitoring_page_redirects_to_elder(client, accounts):
    assert page_login(client, "super").status_code == 303
    resp = client.get("/monitoring", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/monitoring/")

    elder_id = resp.headers["location"].rsplit("/", 1)[-1]
    resp = client.get(f"/monitoring/{elder_id}")
    assert resp.status_code == 200
    assert "健康监测" in resp.text


def test_missing_elder_returns_friendly_page(client, accounts):
    assert page_login(client, "super").status_code == 303
    resp = client.get("/elders/999999")
    assert resp.status_code == 404
    assert "页面渲染失败" not in resp.text
    assert "档案不存在" in resp.text


def test_logout_clears_cookie(client, accounts):
    assert page_login(client, "super").status_code == 303
    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")
