"""Isolated Edge browser verification; requires optional Playwright tooling.

Install Playwright separately, or use reports/chuanzhi-tools/python.
No existing demonstration database is modified.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reports/chuanzhi-tools/python"))


def main() -> None:
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--slow-demo", action="store_true", help="Readable automated demonstration with pauses")
    args = parser.parse_args()
    run = ROOT / "reports/chuanzhi-browser" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run.mkdir(parents=True, exist_ok=True)
    (run / "tmp").mkdir()
    os.environ["TEMP"] = os.environ["TMP"] = str(run / "tmp")
    report = {"started_at": datetime.now().isoformat(), "method": "automated Edge / Playwright", "checks": []}
    environment = {
        **os.environ, "DATABASE_URL": "sqlite:///" + (run / "demo.db").as_posix(),
        "UPLOAD_DIR": str(run / "uploads"), "BACKUP_DIR": str(run / "backups"),
        "DATA_DIR": str(run), "ENABLE_SCHEDULER": "false", "PYTHONIOENCODING": "utf-8",
    }
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (run / "seed.log").open("w", encoding="utf-8") as output:
        subprocess.run([sys.executable, "-m", "scripts.seed"], cwd=ROOT, env=environment,
                       stdout=output, stderr=subprocess.STDOUT, check=True, timeout=120, creationflags=flags)
    with closing(sqlite3.connect(run / "demo.db")) as db:
        db.row_factory = sqlite3.Row
        elder = dict(db.execute("SELECT id, lat, lng FROM elder ORDER BY id LIMIT 1").fetchone())
        nurses = {row["id"]: row["username"] for row in db.execute(
            "SELECT s.id, u.username FROM staff s JOIN user u ON u.id=s.user_id")}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    server_log = (run / "server.log").open("w", encoding="utf-8")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                              cwd=ROOT, env=environment, stdout=server_log, stderr=subprocess.STDOUT, creationflags=flags)
    context = browser = None
    try:
        import httpx
        for _ in range(150):
            try:
                if httpx.get(base + "/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("Server startup failed")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", headless=True)
            video_options = {} if args.probe else {"record_video_dir": str(run / "video"),
                                                  "record_video_size": {"width": 1920, "height": 1080}}
            context = browser.new_context(viewport={"width": 1920, "height": 1080},
                                          geolocation={"latitude": elder["lat"], "longitude": elder["lng"]},
                                          permissions=["geolocation"], **video_options)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def reading_pause():
                if args.slow_demo:
                    page.wait_for_timeout(7000)

            def check(name: str, result: bool, detail=None):
                report["checks"].append({"name": name, "passed": bool(result), "detail": detail})
                if not result:
                    raise AssertionError(f"{name}: {detail}")

            def login(username: str):
                page.goto(base + "/logout")
                page.goto(base + "/login")
                page.locator("#username").fill(username)
                page.locator("#password").fill("Admin@123")
                page.locator('button[type="submit"]').click()
                page.wait_for_url(base + "/")

            login("elder1")
            page.goto(base + "/appointments/new")
            toast_type = page.evaluate("typeof App.toast")
            report["toast_type"] = toast_type
            page.screenshot(path=str(run / "appointment-desktop.png"), full_page=True)
            reading_pause()
            layout = []
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 1000})
                for size in ("standard", "large", "extra"):
                    page.locator("#font-size").select_option(size)
                    page.wait_for_timeout(100)
                    dimensions = page.evaluate("({width: innerWidth, scroll: document.documentElement.scrollWidth, bodyFont: getComputedStyle(document.body).fontSize})")
                    layout.append({"viewport": width, "font": size, **dimensions})
            report["layout"] = layout
            page.screenshot(path=str(run / "appointment-mobile-extra.png"), full_page=True)
            if args.probe:
                report["probe_only"] = True
                return
            check("public toast helper", toast_type == "function", toast_type)
            check("appointment responsive width", all(x["scroll"] <= x["width"] + 1 for x in layout), layout)
            page.reload()
            check("font preference persists", page.locator("#font-size").input_value() == "extra")
            page.set_viewport_size({"width": 1920 if args.slow_demo else 1440, "height": 1080 if args.slow_demo else 1000})
            page.locator("#font-size").select_option("standard")
            page.locator("#btn-submit").click()
            check("required-field feedback", page.locator(".toast.warn").count() > 0)
            check("feedback is visually displayed", page.locator(".toast.warn").last.is_visible())
            page.locator("#service_item_id").select_option("10")
            page.route("**/api/v1/appointments", lambda route: route.fulfill(status=409, content_type="application/json", body=json.dumps({"message": "验收注入：时段冲突，请调整时间"})))
            page.locator("#btn-submit").click()
            page.wait_for_function("document.querySelector('#form-tip').textContent.includes('时段冲突')")
            check("failed appointment restores submit", page.locator("#btn-submit").is_enabled())
            page.unroute("**/api/v1/appointments")
            page.evaluate("""async () => {
                var count = 0;
                var original = App.api.post;
                var release;
                App.api.post = function () { count += 1; return new Promise(resolve => { release = resolve; }); };
                var button = document.createElement('button');
                button.textContent = '验收动作'; document.body.appendChild(button);
                var first = App.action(button, '/verification-only', {}, '', {reload:false});
                App.action(button, '/verification-only', {}, '', {reload:false});
                window.__actionProbe = {requests:count, disabled:button.disabled, busy:button.getAttribute('aria-busy'), label:button.textContent};
                release({code:0}); await first;
                window.__actionProbe.restored = !button.disabled && button.textContent === '验收动作';
                App.api.post = original; button.remove();
            }""")
            action = page.evaluate("window.__actionProbe")
            check("action prevents repeated click and shows loading", action["requests"] == 1 and action["disabled"] and action["busy"] == "true" and action["restored"], action)
            page.locator("#service_item_id").select_option("10")
            start = (datetime.now() + timedelta(days=30)).replace(hour=9, minute=0, second=0, microsecond=0)
            page.locator("#desired_start").fill(start.strftime("%Y-%m-%dT%H:%M"))
            page.locator("#remark").fill("传智杯隔离浏览器验收")
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/v1/appointments")) as created:
                page.locator("#btn-submit").click()
            result = created.value.json()
            check("elder creates appointment", created.value.ok, result)
            appointment_id = result["data"]["id"]
            report["appointment_id"] = appointment_id
            page.wait_for_url(base + "/appointments")
            reading_pause()
            login("grid1")
            page.goto(base + "/appointments")
            page.locator(f'[data-act="accept"][data-id="{appointment_id}"]').click()
            page.wait_for_timeout(900)
            page.locator(f'[data-act="to-dispatch"][data-id="{appointment_id}"]').click()
            page.wait_for_timeout(900)
            reading_pause()
            page.goto(base + "/dispatch")
            trigger = page.locator(f'[data-dispatch="{appointment_id}"]')
            trigger.focus()
            trigger.click()
            page.locator("[data-assign][data-nurse]:not([data-nurse=''])").first.wait_for()
            check("modal focus inside", page.evaluate("!!document.activeElement.closest('[role=dialog]')"))
            page.locator(".modal-foot button").last.focus()
            page.keyboard.press("Tab")
            check("modal forward tab loop", page.evaluate("document.activeElement.classList.contains('modal-close')"))
            page.keyboard.press("Shift+Tab")
            check("modal backward tab loop", page.evaluate("document.activeElement.closest('.modal-foot') !== null"))
            page.keyboard.press("Escape")
            check("modal escape and focus restore", page.locator(".modal-mask").count() == 0 and trigger.evaluate("el => el === document.activeElement"))
            candidate_url = base + f"/api/v1/dispatch/{appointment_id}/candidates"
            candidate_fixture = page.request.get(candidate_url).json()
            fixture_list = candidate_fixture["data"]
            if isinstance(fixture_list, dict):
                fixture_list = fixture_list.get("candidates", [])
            for candidate in fixture_list:
                candidate["weights"] = [0.1, 0.2, 0.3, 0.4]
            page.route("**/dispatch/*/candidates", lambda route: route.fulfill(json=candidate_fixture))
            trigger.click()
            page.wait_for_function("document.querySelector('#cand-box').textContent.includes('权重 10%')")
            check("candidate uses response weights instead of fixed defaults", "权重 40%" in page.locator("#cand-box").inner_text())
            page.keyboard.press("Escape")
            page.unroute("**/dispatch/*/candidates")
            trigger.click()
            assign = page.locator("[data-assign][data-nurse]:not([data-nurse=''])").first
            assign.wait_for()
            nurse_id = int(assign.get_attribute("data-nurse"))
            page.screenshot(path=str(run / "dispatch-candidates.png"), full_page=True)
            check("candidate explains actual weights and contributions", page.locator("#cand-box").inner_text().find("加权贡献") >= 0)
            reading_pause()
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(f"/dispatch/{appointment_id}/assign")) as assigned:
                assign.click()
            assignment = assigned.value.json()
            check("candidate dispatch succeeds", assigned.value.ok, assignment)
            report["assignment"] = assignment["data"]
            with closing(sqlite3.connect(run / "demo.db")) as db:
                order_id = db.execute("SELECT id FROM work_order WHERE appointment_id=?", (appointment_id,)).fetchone()[0]
            report["order_id"] = order_id
            login(nurses[nurse_id])
            page.goto(base + f"/orders/{order_id}")
            page.locator("#btn-accept").click()
            page.wait_for_timeout(900)
            page.locator("#btn-checkin").click()
            page.locator("#btn-checkout-open").wait_for(timeout=12000)
            reading_pause()
            for checkbox in page.locator(".item-check").all():
                checkbox.check()
            if page.locator("#btn-save-items").count():
                page.locator("#btn-save-items").click()
                page.wait_for_timeout(900)
            page.locator("#btn-checkout-open").click()
            page.locator("#summary").fill("浏览器隔离验收：完成健康监测，异常数据用于规则演示。")
            page.locator("#m-sbp").fill("200")
            page.locator("#m-hr").fill("125")
            page.locator("#m-spo2").fill("85")
            page.screenshot(path=str(run / "nurse-checkout.png"), full_page=True)
            reading_pause()
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(f"/orders/{order_id}/checkout")) as checkout:
                page.locator("#btn-checkout").click()
            check("nurse checkout with abnormal metrics", checkout.value.ok, checkout.value.json())
            page.wait_for_timeout(900)
            login("grid1")
            page.goto(base + "/alerts")
            page.screenshot(path=str(run / "health-alerts.png"), full_page=True)
            reading_pause()
            alerts = page.request.get(base + f"/api/v1/alerts?elder_id={elder['id']}&status=pending&size=200").json()["data"]["items"]
            check("health metrics trigger pending alert", bool(alerts), alerts)
            alert_id = alerts[0]["id"]
            report["alert_id"] = alert_id
            ack = page.locator(f'[data-ack="{alert_id}"]')
            if ack.count():
                ack.click()
                page.wait_for_timeout(900)
            page.locator(f'[data-handle="{alert_id}"]').click()
            page.get_by_role("button", name="提交闭环").click()
            check("resolution required-field feedback", page.locator(".toast.warn").count() > 0 and page.locator(".modal-mask").count() == 1)
            page.locator("#h-measure").fill("演示处置：已联系家属并记录转诊建议，模拟数据不用于医疗判断。")
            page.route("**/alerts/*/handle", lambda route: route.fulfill(status=500, json={"message": "验收注入：服务暂不可用，请重试"}))
            page.get_by_role("button", name="提交闭环").click()
            page.wait_for_function("document.querySelector('.toast.error') && Array.from(document.querySelectorAll('.toast.error')).some(x => x.textContent.includes('服务暂不可用'))")
            check("failed resolution preserves input and retry", page.locator("#h-measure").input_value().startswith("演示处置") and page.get_by_role("button", name="提交闭环").is_enabled())
            page.unroute("**/alerts/*/handle")
            page.screenshot(path=str(run / "alert-resolution.png"), full_page=True)
            reading_pause()
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(f"/alerts/{alert_id}/handle")) as handled:
                page.get_by_role("button", name="提交闭环").click()
            check("alert resolution succeeds", handled.value.ok, handled.value.json())
            page.wait_for_timeout(900)
            page.goto(base + "/appointments?status=verification-empty")
            check("empty list feedback", page.locator(".empty-state").is_visible())
            for path in ("/", "/appointments", "/dispatch", "/orders", "/alerts", "/stats"):
                page.goto(base + path)
                page.set_viewport_size({"width": 390, "height": 900})
                dimensions = page.evaluate("({width:innerWidth,scroll:document.documentElement.scrollWidth})")
                check("mobile width " + path, dimensions["scroll"] <= 391, dimensions)
            page.goto(base + "/alerts?status=resolved")
            page.screenshot(path=str(run / "alerts-mobile.png"), full_page=True)
            check("no uncaught browser errors", not errors, errors)
            report["passed"] = True
            report["browser_errors"] = errors
            context.close()
            context = None
            browser.close()
            browser = None
    except Exception as error:
        report["passed"] = False
        report["error"] = str(error)
        raise
    finally:
        if context:
            try:
                context.close()
            except Exception:
                pass
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        server.terminate()
        server.wait(timeout=15)
        server_log.close()
        report["finished_at"] = datetime.now().isoformat()
        (run / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(run / "result.json"))


if __name__ == "__main__":
    main()
