"""独立临时库验收：种子幂等、真实 HTTP 启动、六角色登录与页面访问。

python -m scripts.verify_demo
不会修改当前演示数据库；报告写入 reports/demo-smoke.json。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def stop_owned_server(process: subprocess.Popen, port: int) -> dict:
    """Stop only our Popen tree, then verify its listening socket is released."""
    result = {"pid": process.pid, "port": port, "method": "terminate", "port_released": False}
    if os.name == "nt" and process.poll() is None:
        stopped = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
            timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        result.update(method="taskkill-owned-tree", taskkill_exit_code=stopped.returncode)
        if stopped.returncode != 0 and process.poll() is None:
            raise RuntimeError(f"Cannot stop owned verification process tree PID {process.pid}")
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    deadline = time.monotonic() + 5
    while True:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                result["port_released"] = True
                break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Verification server port {port} remains open after owned process shutdown")
        time.sleep(0.1)
    result["exit_code"] = process.returncode
    return result


def cleanup_owned_directory(target: Path, expected_parent: Path) -> dict:
    """Allow bounded Windows handle-release delay; never suppress a persistent failure."""
    resolved = target.resolve()
    if target.is_symlink() or resolved.parent != expected_parent.resolve() or not resolved.name.startswith("eldercare-verify-"):
        raise RuntimeError(f"Refusing cleanup outside the newly created verification directory: {target}")
    started = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        try:
            shutil.rmtree(resolved)
            return {"passed": True, "attempts": attempts, "elapsed_seconds": round(time.monotonic() - started, 3)}
        except PermissionError:
            if time.monotonic() - started >= 5:
                raise
            time.sleep(0.2)


def counts(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: connection.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                for name in names}


def main() -> None:
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    report = {"seed_idempotent": False, "checks": [], "app_http_passed": False, "passed": False}
    report_path = reports / "demo-smoke.json"
    target = Path(tempfile.mkdtemp(prefix="eldercare-verify-"))
    expected_temp_parent = target.parent.resolve()
    report["temporary_directory"] = str(target)

    def save_report() -> None:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        db_path = target / "demo.db"
        environment = {**os.environ, "DATABASE_URL": "sqlite:///" + db_path.as_posix(),
                       "UPLOAD_DIR": str(target / "uploads"), "BACKUP_DIR": str(target / "backups"),
                       "DATA_DIR": str(target), "ENABLE_SCHEDULER": "false", "PYTHONIOENCODING": "utf-8"}
        process_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        for iteration in range(2):
            with (reports / f"seed-{iteration + 1}.log").open("w", encoding="utf-8") as output:
                subprocess.run([sys.executable, "-m", "scripts.seed"], cwd=ROOT, env=environment,
                               stdout=output, stderr=subprocess.STDOUT, check=True, timeout=120,
                               creationflags=process_flags)
            if iteration == 0:
                first = counts(db_path)
        second = counts(db_path)
        if first != second:
            raise RuntimeError("重复运行种子脚本改变了表记录数")
        report["seed_idempotent"] = True
        report["table_counts"] = second
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        with (reports / "demo-server.log").open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=ROOT, env=environment, stdout=output, stderr=subprocess.STDOUT, creationflags=process_flags,
            )
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as client:
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            response = client.get("/health")
                            response.raise_for_status()
                            break
                        except httpx.HTTPError:
                            if process.poll() is not None or time.monotonic() >= deadline:
                                raise RuntimeError("服务未能启动，请查看 reports/demo-server.log") from None
                            time.sleep(0.2)
                    checks = {"super": ["/", "/billing", "/admin/audit"],
                              "street": ["/stats", "/billing"], "orgadmin": ["/quality", "/billing"],
                              "grid1": ["/appointments", "/dispatch", "/billing"],
                              "nurse1": ["/orders"], "elder1": ["/appointments", "/billing"]}
                    for username, pages in checks.items():
                        client.cookies.clear()
                        login = client.post("/api/v1/auth/login", json={"username": username, "password": "Admin@123"})
                        login.raise_for_status()
                        for page in pages:
                            response = client.get(page)
                            if response.status_code != 200:
                                raise RuntimeError(f"{username} 访问 {page} 返回 {response.status_code}")
                            report["checks"].append({"user": username, "path": page, "status": response.status_code})
                    report["app_http_passed"] = True
                    save_report()
            finally:
                report["server_shutdown"] = stop_owned_server(process, port)
                save_report()
    finally:
        try:
            report["temporary_cleanup"] = cleanup_owned_directory(target, expected_temp_parent)
        except Exception as exc:
            report["temporary_cleanup"] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
            raise
        finally:
            report["passed"] = bool(report["app_http_passed"] and report.get("server_shutdown", {}).get("port_released")
                                    and report.get("temporary_cleanup", {}).get("passed"))
            save_report()
    print(f"演示验收通过：种子幂等，六角色登录，{len(report['checks'])} 次页面访问；报告 reports/demo-smoke.json")


if __name__ == "__main__":
    main()
