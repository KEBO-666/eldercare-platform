"""Record a transparent, interactive replay of real visible development evidence."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reports/chuanzhi-tools/python"))


def main() -> None:
    from playwright.sync_api import sync_playwright

    run = ROOT / "reports/chuanzhi-ai-recording" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run.mkdir(parents=True)
    (run / "tmp").mkdir()
    os.environ["TEMP"] = os.environ["TMP"] = str(run / "tmp")
    evidence_dir = ROOT / "docs/chuanzhi/evidence/history"
    history = [json.loads(line) for line in (evidence_dir / "2026-11-12-optimization/visible-conversation.jsonl").read_text(encoding="utf-8").splitlines()]
    current = [json.loads(line) for line in (evidence_dir / "current-root/visible-conversation.jsonl").read_text(encoding="utf-8").splitlines()]
    excerpts = json.loads((evidence_dir / "video-excerpts.json").read_text(encoding="utf-8"))["excerpts"]
    records = {record["source_line"]: record for record in history}

    def card(title: str, record: dict, text: str | None = None, explanation: str = ""):
        return {"title": title, "text": text if text is not None else record["text"],
                "meta": f"原记录 L{record['source_line']} · {record.get('timestamp', '')} · {record.get('role', record.get('type'))}",
                "hash": record["source_record_sha256"], "explanation": explanation}

    cards = [
        card("01 用户真实意图", next(record for record in current if record.get("role") == "user"), explanation="直接读取本轮可见原始请求；未重写或模拟输入。"),
        card("02 助手如何拆解任务", next(record for record in current if record.get("role") == "assistant"), explanation="选手提出目标后，助手先验证环境，再按责任范围执行。"),
        card("03 助手定位真实业务缺口", records[114], explanation="读取早先优化轮的实际可见回复，展示当时如何说明约束。"),
        card("04 真实失败反馈", records[185], excerpts[2]["text"], "从真实工具输出解码并筛选FAILED行；不是本次重新制造失败。"),
        card("05 查看AI生成的真实补丁", records[173], excerpts[0]["text"], "由真实工具输入解码，连续摘录事务与四周创建代码。"),
        card("06 查看失败后的修复说明", records[477], explanation="读取当时实际SQLite连接占用的修复说明。"),
    ]

    def source_card(title: str, relative: str, needle: str, before: int, count: int):
        path = ROOT / relative
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        anchor = next(i for i, line in enumerate(lines) if needle in line)
        start = max(0, anchor - before)
        return {"title": title, "text": "\n".join(f"{i+1:>4}  {lines[i]}" for i in range(start, min(len(lines), start + count))),
                "meta": f"当前工作树实际文件：{relative} · 行{start+1}",
                "hash": hashlib.sha256(path.read_bytes()).hexdigest(),
                "explanation": "直接读取本轮实际代码；代码状态由文件哈希定位，未宣称AI现场重新生成。"}

    baseline = ROOT / "reports/chuanzhi-browser/20261112-170748/result.json"
    before = json.loads(baseline.read_text(encoding="utf-8"))
    cards.append({"title": "07 本轮浏览器基线发现", "text": json.dumps({"started_at": before["started_at"], "toast_type": before["toast_type"], "method": before["method"], "meaning": "页面调用App.toast，但公共脚本未导出，HTTP200无法发现此错误。"}, ensure_ascii=False, indent=2),
                  "meta": str(baseline.relative_to(ROOT)), "hash": hashlib.sha256(baseline.read_bytes()).hexdigest(),
                  "explanation": "读取未修复时的真实Edge探测结果，之后切换实际修复代码。"})
    cards.append(source_card("08 直接核对修复：公开提示方法", "app/static/js/app.js", "App.toast = toast", 7, 17))
    cards.append(source_card("09 直接核对修复：等待态与防重入", "app/static/js/app.js", "App.action = function", 0, 25))
    cards.append(source_card("10 直接核对复现脚本", "scripts/run_dev.ps1", "BasePython", 2, 22))
    failed_run = ROOT / "reports/chuanzhi-ai-recording/20261112-172754/real-verification.json"
    failure = json.loads(failed_run.read_text(encoding="utf-8"))["commands"][-1]
    cards.append({"title": "11 新反馈：录制时发现清理失败", "text": "本次实际命令：python -m scripts.verify_demo\n真实退出码：" + str(failure["exit_code"]) + "\n\n" + "\n".join(line for line in failure["stderr"].splitlines() if "PermissionError" in line) + "\n\n主代理把实际错误分派给复现分支排查；保留失败记录，修复后重新运行完整验收。",
                  "meta": str(failed_run.relative_to(ROOT)), "hash": hashlib.sha256(failed_run.read_bytes()).hexdigest(),
                  "explanation": "录屏过程中实际出现的新问题；不是为了演示而制造的错误。"})
    final_browser = ROOT / "reports/chuanzhi-browser/20261112-171450/result.json"
    verified = json.loads(final_browser.read_text(encoding="utf-8"))
    cards.append({"title": "12 本轮回归与现场重新验证", "text": json.dumps({"started_at": verified["started_at"], "passed": verified["passed"], "checks": len(verified["checks"]), "browser_errors": verified["browser_errors"], "result": "预约—派单—签到执行—异常健康指标—预警闭环全部通过；注入项独立标明。"}, ensure_ascii=False, indent=2),
                  "meta": str(final_browser.relative_to(ROOT)), "hash": hashlib.sha256(final_browser.read_bytes()).hexdigest(),
                  "explanation": "这是本轮实际自动化验收，不是参赛人原始手工开发录像。"})
    state = {"running": False, "output": "点击执行：本次真实运行Ruff、JS语法和隔离HTTP冒烟。", "commands": []}

    def verify():
        state["running"] = True
        state["output"] = ""
        commands = [[sys.executable, "-m", "ruff", "check", "."], ["node", "--check", "app/static/js/app.js"], [sys.executable, "-m", "scripts.verify_demo"]]
        for command in commands:
            state["output"] += "\n> " + subprocess.list2cmdline(command) + "\n"
            started = time.monotonic()
            process = subprocess.run(command, cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"}, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=150, creationflags=subprocess.CREATE_NO_WINDOW)
            item = {"command": command, "exit_code": process.returncode, "seconds": round(time.monotonic() - started, 3), "stdout": process.stdout, "stderr": process.stderr}
            state["commands"].append(item)
            state["output"] += process.stdout + process.stderr + f"\n[真实退出码 {process.returncode}；{item['seconds']} 秒]\n"
        state["running"] = False
        (run / "real-verification.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    data = json.dumps(cards, ensure_ascii=False).replace("</", "<\\/")
    page_html = """<!doctype html><meta charset='utf-8'><title>真实AI协同记录复现</title>
<style>*{box-sizing:border-box}body{margin:0;background:#edf3f7;color:#17364d;font-family:'Microsoft YaHei',sans-serif}header{padding:24px 46px;background:#15364d;color:white;border-top:10px solid #0c8c97}h1{font-size:34px;margin:0}header p{font-size:20px;color:#c9e4e8;margin:12px 0 0}main{display:grid;grid-template-columns:340px 1fr;height:860px;gap:24px;padding:24px 42px}nav{display:flex;flex-direction:column;gap:8px}button{font-family:inherit;font-size:19px;text-align:left;padding:15px;border:1px solid #acc2cd;border-radius:8px;background:white;color:#17364d;cursor:pointer}button.active{background:#087f8c;color:white}article{background:white;border-radius:12px;border:1px solid #c3d3df;padding:28px 34px;overflow:auto}h2{font-size:31px;margin:0 0 16px}#meta{font-size:17px;color:#477385;overflow-wrap:anywhere}#note{font-size:21px;background:#e5f5f3;padding:14px;border-left:6px solid #087f8c;margin:16px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:22px/1.7 Consolas,'Microsoft YaHei',monospace;color:#19384b;background:#f5f8fa;padding:20px;margin:12px 0}#hash{font-size:15px;word-break:break-all;color:#647b88}footer{padding:10px 46px;background:#15364d;color:#e4eff3;font-size:19px}#execute{background:#087f8c;color:white}#live{max-height:560px;overflow:auto;font-size:21px}</style>
<header><h1>颐邻 · 真实 AI 协同记录复现</h1><p>本科 B 组 · 唐克波 · 2132417 ｜ 本轮记录重放与实际验证；非AI现场重新生成，不冒充三条独立完整Prompt链。</p></header>
<main><nav id='nav'></nav><article id='article'><h2 id='title'></h2><div id='meta'></div><div id='note'></div><pre id='text'></pre><div id='hash'></div><button id='execute' hidden>执行本次真实验证</button><pre id='live' hidden></pre></article></main>
<footer>可见原始记录 → 失败反馈 → 实际补丁 → 本轮代码 → 点击执行与真实退出码；完整证据位置见技术文档。</footer>
<script>const cards=__DATA__;const nav=document.querySelector('#nav');function show(i){document.querySelectorAll('nav button').forEach((b,n)=>b.classList.toggle('active',n===i));document.querySelector('#title').textContent=cards[i].title;document.querySelector('#meta').textContent=cards[i].meta;document.querySelector('#note').textContent=cards[i].explanation;document.querySelector('#text').textContent=cards[i].text;document.querySelector('#hash').textContent='原始记录/文件 SHA-256: '+cards[i].hash;document.querySelector('#execute').hidden=i!==cards.length-1;document.querySelector('#live').hidden=i!==cards.length-1;document.querySelector('#article').scrollTop=0;}cards.forEach((c,i)=>{let b=document.createElement('button');b.id='step-'+i;b.textContent=c.title;b.onclick=()=>show(i);nav.appendChild(b)});show(0);document.querySelector('#execute').onclick=async()=>{document.querySelector('#execute').disabled=true;await fetch('/run',{method:'POST'});};setInterval(async()=>{let s=await(await fetch('/status')).json();document.querySelector('#live').textContent=s.output;document.querySelector('#live').scrollTop=document.querySelector('#live').scrollHeight;},600)</script>""".replace("__DATA__", data)
    (run / "evidence-viewer.html").write_text(page_html, encoding="utf-8")
    (run / "cards.json").write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = json.dumps(state, ensure_ascii=False).encode("utf-8") if self.path == "/status" else page_html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8" if self.path == "/status" else "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            if self.path == "/run" and not state["commands"] and not state["running"]:
                threading.Thread(target=verify, daemon=True).start()
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080}, record_video_dir=str(run / "video"), record_video_size={"width": 1920, "height": 1080})
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{port}")
        started = time.monotonic()
        for index in range(len(cards)):
            page.locator(f"#step-{index}").click()
            if index == len(cards) - 1:
                page.locator("#execute").click()
                page.locator("#execute").scroll_into_view_if_needed()
            page.wait_for_timeout(7000)
            if index in (0, 3, 7, 10):
                page.screenshot(path=str(run / f"evidence-step-{index:02}.png"))
        page.wait_for_function("async () => { const state = await (await fetch('/status')).json(); return state.commands.length === 3 && !state.running; }", timeout=45000)
        page.evaluate("document.querySelector('#article').scrollTop = document.querySelector('#article').scrollHeight")
        remaining = 122 - (time.monotonic() - started)
        if remaining > 0:
            page.wait_for_timeout(remaining * 1000)
        page.screenshot(path=str(run / "evidence-final-verification.png"))
        context.close()
        browser.close()
    server.shutdown()
    report = {"recording_seconds_target": 122, "actual_seconds": round(time.monotonic() - started, 3), "cards": len(cards), "commands": state["commands"], "all_commands_passed": bool(state["commands"]) and all(item["exit_code"] == 0 for item in state["commands"]), "video": [str(path) for path in (run / "video").glob("*.webm")], "notice": "Interactive replay of authentic visible records plus actual current command execution; not online regeneration or 3 independent full prompt chains."}
    (run / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
