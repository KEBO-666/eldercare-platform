"""Time a fresh source-copy installation without reusing the application venv/database.

python -m scripts.verify_reproduction
Preserves the original data and all run artifacts. This is not a remote-clone timing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ("app", "scripts", "tests", "docs", "docker")
FILES = ("README.md", "requirements.txt", "pyproject.toml", "pytest.ini", ".env.example", ".gitignore")
EXCLUDED = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "node_modules"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "chuanzhi-reproduction.json")
    arguments = parser.parse_args()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run = ROOT / "reports" / "chuanzhi-reproduction-runs" / stamp
    target = run / "source"
    run.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ)
    for name in ("DATABASE_URL", "UPLOAD_DIR", "BACKUP_DIR", "DATA_DIR", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(name, None)
    environment.update(PYTHONIOENCODING="utf-8", ENABLE_SCHEDULER="false", PIP_DISABLE_PIP_VERSION_CHECK="1")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    report: dict = {
        "started_at_utc": now(), "passed": False, "within_ten_minutes": False,
        "source_method": "allowlisted current-working-tree copy; not remote clone",
        "runtime": {"python": sys.version, "base_python": sys._base_executable,
                    "os": platform.platform(), "processor": platform.processor(), "logical_cpus": os.cpu_count()},
        "conditions": {"python_preinstalled": True, "source_venv_reused": False,
                       "source_database_reused": False, "pip_cache_policy": "default cache, network allowed",
                       "scheduler": False, "browser_visual_check": False},
        "run_directory": str(run), "stages": [], "checks": [],
    }
    report_path = arguments.report.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def command(name: str, args: list[str], timeout: int = 600) -> None:
        stage_started = time.monotonic()
        with (run / f"{name}.log").open("w", encoding="utf-8") as output:
            result = subprocess.run(args, cwd=target, env=environment, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=timeout, creationflags=flags)
        report["stages"].append({"name": name, "elapsed_seconds": round(time.monotonic() - stage_started, 3),
                                 "exit_code": result.returncode, "log": str(run / f"{name}.log")})
        result.check_returncode()

    try:
        target.mkdir()
        hashes = {}
        for filename in FILES:
            source = ROOT / filename
            shutil.copy2(source, target / filename)
            hashes[filename] = hashlib.sha256(source.read_bytes()).hexdigest()
        for directory in DIRECTORIES:
            for source in (ROOT / directory).rglob("*"):
                relative = source.relative_to(ROOT)
                if source.is_file() and not EXCLUDED.intersection(relative.parts) and source.suffix not in (".pyc", ".pyo"):
                    destination = target / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                    hashes[relative.as_posix()] = hashlib.sha256(destination.read_bytes()).hexdigest()
        (run / "source-sha256.json").write_text(json.dumps(hashes, ensure_ascii=False, indent=2), encoding="utf-8")
        report["initial_state"] = {"venv_exists": (target / ".venv").exists(),
                                   "database_exists": (target / "data/app.db").exists(),
                                   "env_exists": (target / ".env").exists(), "source_file_count": len(hashes)}
        report["stages"].append({"name": "source-copy", "elapsed_seconds": round(time.monotonic() - started, 3)})
        command("create-venv", [sys._base_executable, "-m", "venv", ".venv"], timeout=120)
        python = target / ".venv" / "Scripts" / "python.exe"
        command("pip-cache-before", [str(python), "-m", "pip", "cache", "info"], timeout=20)
        command("install-dependencies", [str(python), "-m", "pip", "install", "-r", "requirements.txt"])
        command("pip-check", [str(python), "-m", "pip", "check"], timeout=30)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        stage_started = time.monotonic()
        with (run / "launcher-server.log").open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(target / "scripts/run_dev.ps1"), "-Port", str(port), "-NoReload"],
                cwd=target, env=environment, stdout=output, stderr=subprocess.STDOUT, creationflags=flags,
            )
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15, trust_env=False) as client:
                    deadline = time.monotonic() + 120
                    while True:
                        try:
                            client.get("/health").raise_for_status()
                            break
                        except httpx.HTTPError:
                            if process.poll() is not None or time.monotonic() >= deadline:
                                raise RuntimeError("Launcher did not become healthy; see launcher-server.log") from None
                            time.sleep(0.2)
                    pages = {"super": ["/", "/billing", "/admin/audit"], "street": ["/stats", "/billing"],
                             "orgadmin": ["/quality", "/billing"], "grid1": ["/appointments", "/dispatch", "/billing"],
                             "nurse1": ["/orders"], "elder1": ["/appointments", "/billing"]}
                    for username, paths in pages.items():
                        client.cookies.clear()
                        client.post("/api/v1/auth/login", json={"username": username, "password": "Admin@123"}).raise_for_status()
                        for path in paths:
                            response = client.get(path)
                            response.raise_for_status()
                            if response.status_code != 200:
                                raise RuntimeError(f"Unexpected {response.status_code}: {username} {path}")
                            report["checks"].append({"user": username, "path": path, "status": response.status_code})
                report["elapsed_to_all_logins_seconds"] = round(time.monotonic() - started, 3)
                report["stages"].append({"name": "launcher-seed-start-and-six-role-http", "elapsed_seconds": round(time.monotonic() - stage_started, 3)})
            finally:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, check=False)
                else:
                    process.terminate()
                process.wait(timeout=15)
        report["passed"] = True
        report["within_ten_minutes"] = report["elapsed_to_all_logins_seconds"] <= 600
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["finished_at_utc"] = now()
        report["total_elapsed_seconds"] = round(time.monotonic() - started, 3)
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        report_path.write_text(serialized, encoding="utf-8")
        (run / "result.json").write_text(serialized, encoding="utf-8")
        print(serialized)


if __name__ == "__main__":
    main()
