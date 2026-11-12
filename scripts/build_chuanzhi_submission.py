"""Build and verify a deterministic whitelist package for the Vibe Coding entry.

The package is a local review artifact, not a repository publication or a contest
upload receipt. Missing PDF/video are allowed only for an explicitly named draft.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ENTRY = "2132417"
NAME = "唐克波"
SOURCE_DIRS = ("app", "scripts", "tests", "docs", "docker")
SOURCE_FILES = ("README.md", "requirements.txt", "pyproject.toml", "pytest.ini", ".env.example", ".gitignore")
EXCLUDED_PARTS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    "node_modules", "uploads", "backups", "pip-cache", "tmp", "authoring-venv",
}
EXCLUDED_NAMES = {".env", "auth.json", "credentials.json", ".coverage", ".ds_store", "thumbs.db"}
EXCLUDED_ENDINGS = (
    ".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3",
    ".db-journal", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal", ".sqlite3-wal",
    ".sqlite3-shm", ".sqlite3-journal", ".pem", ".key", ".whl", ".zip", ".exe", ".dll",
)
REPORT_EXTENSIONS = {".json", ".xml", ".log", ".md", ".txt", ".html", ".css", ".js", ".png", ".svg"}
FIXED_ZIP_TIME = (2026, 11, 12, 0, 0, 0)


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(name: str) -> bool:
    path = PurePosixPath(name)
    parts = tuple(part.casefold() for part in path.parts)
    return bool(
        name and not path.is_absolute() and ".." not in parts
        and "\\" not in name and ":" not in name
        and not EXCLUDED_PARTS.intersection(parts)
        and path.name.casefold() not in EXCLUDED_NAMES
        and not path.name.casefold().endswith(EXCLUDED_ENDINGS)
    )


def git_head(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
        timeout=15, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def collect_sources(root: Path, browser_run: Path | None = None) -> tuple[dict[str, Path], dict[str, Any]]:
    selected: dict[str, Path] = {}

    def add(path: Path) -> None:
        relative = path.relative_to(root).as_posix()
        if path.is_file() and not path.is_symlink() and safe_path(relative):
            resolved = path.resolve()
            if root.resolve() not in resolved.parents:
                raise ValueError("Source resolves outside the project: " + relative)
            selected["eldercare-platform/" + relative] = path

    for name in SOURCE_FILES:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError("Missing required source configuration: " + name)
        add(path)
    for directory in SOURCE_DIRS:
        for path in (root / directory).rglob("*"):
            add(path)
    reports = root / "reports"
    for name in ("chuanzhi-reproduction.json", "chuanzhi-reproduction-errors.json", "chuanzhi-reproduction-final.json",
                 "chuanzhi-history-audit.json", "chuanzhi-publication-review.json",
                 "github-publication.json", "github-remote-reproduction.json"):
        add(reports / name)
    for path in reports.glob("chuanzhi-evidence-*.json"):
        add(path)
    for directory in (reports / "chuanzhi-final",):
        for path in directory.rglob("*"):
            if path.suffix.lower() in REPORT_EXTENSIONS:
                add(path)
    browser_root = reports / "chuanzhi-browser"
    if browser_run is None:
        runs = sorted(path.parent for path in browser_root.glob("*/result.json"))
        browser_run = runs[-1] if runs else None
    if browser_run is not None:
        browser_run = browser_run.resolve()
        if browser_root.resolve() not in browser_run.parents:
            raise ValueError("Browser run must be inside reports/chuanzhi-browser")
        if not (browser_run / "result.json").is_file():
            raise FileNotFoundError("Browser run has no completed result.json")
        for path in browser_run.iterdir():
            if path.name == "result.json" or path.suffix.lower() == ".png":
                add(path)
    # The PDF cites the earlier image paths; preserve those seven PNGs as sources.
    image_sources = reports / "chuanzhi-browser" / "20261112-171224"
    for path in image_sources.glob("*.png"):
        add(path)
    # Only top-level logs/metadata from each cold run, never its copied source/venv/data.
    replication_names = {
        "source-sha256.json", "result.json", "pip-cache-before.log", "installation.log",
        "install-dependencies.log", "create-venv.log", "pip-check.log", "launcher.log", "server.log",
    }
    for run in (reports / "chuanzhi-reproduction-runs").glob("*"):
        if run.is_dir():
            for path in run.iterdir():
                if path.name in replication_names:
                    add(path)
    add(reports / "chuanzhi-pdf" / "qa.json")
    add(reports / "chuanzhi-artifacts-qa.json")
    # New appointment iteration: only explicit review evidence, never exported source/DB/venv.
    add(reports / "appointment-iteration-summary.json")
    for name in ("appointment-iteration-targeted.xml", "appointment-iteration-regression.xml"):
        add(reports / name)
    for name in ("junit.xml", "coverage.xml", "result.json"):
        add(reports / "appointment-iteration-full" / name)
    for run in (reports / "appointment-iteration-baseline").glob("*"):
        if run.is_dir():
            for name in ("result.json", "pytest.log", "junit.xml", "source-sha256.json", "review.md"):
                add(run / name)
    iteration_runs = sorted(path.parent for path in (reports / "appointment-iteration-browser").glob("*/result.json"))
    if iteration_runs:
        for path in iteration_runs[-1].iterdir():
            if path.name == "result.json" or path.suffix.lower() == ".png":
                add(path)
    for name in ("appointment-feedback-summary.json", "appointment-feedback-page-smoke.xml",
                 "appointment-feedback-unit.tap"):
        add(reports / name)
    feedback_runs = sorted(path.parent for path in (reports / "appointment-feedback-browser").glob("*/result.json"))
    if feedback_runs:
        for path in feedback_runs[-1].iterdir():
            if path.name == "result.json" or path.suffix.lower() == ".png":
                add(path)
    for name in ("dispatch-iteration-summary.json", "dispatch-iteration-frontend.tap"):
        add(reports / name)
    for name in ("junit.xml", "coverage.xml"):
        add(reports / "dispatch-iteration-full" / name)
    for name in ("comparison.json", "compare_dispatch.py", "dispatch_original_b8fd6dd.py"):
        add(reports / "dispatch-iteration-baseline" / name)
    dispatch_runs = sorted(path.parent for path in (reports / "dispatch-feedback-browser").glob("*/result.json"))
    if dispatch_runs:
        for path in dispatch_runs[-1].iterdir():
            if path.name == "result.json" or path.suffix.lower() == ".png":
                add(path)
    for name in ("dispatch-consistency-summary.json", "dispatch-consistency-frontend.tap",
                 "dispatch-calculation-before.xml", "dispatch-calculation-before.log"):
        add(reports / name)
    for name in ("junit.xml", "coverage.xml"):
        add(reports / "dispatch-consistency-full" / name)
    consistency_runs = sorted(path.parent for path in (reports / "dispatch-explanation-browser").glob("*/result.json"))
    if consistency_runs:
        for path in consistency_runs[-1].iterdir():
            if path.name in {"result.json", "weights-A-report.json", "weights-B-report.json", "weights-A-dom.json", "weights-B-dom.json"} or path.suffix.lower() == ".png":
                add(path)
    ai_runs = sorted(path.parent for path in (reports / "chuanzhi-ai-recording").glob("*/result.json"))
    if ai_runs:
        ai_run = ai_runs[-1]
        for path in ai_run.iterdir():
            if path.name in {"result.json", "cards.json", "real-verification.json", "evidence-viewer.html"} or path.suffix.lower() == ".png":
                add(path)
    add(root / "output" / "video" / "2132417-唐克波-演示视频.json")
    metadata = {"browser_run": browser_run.relative_to(root).as_posix() if browser_run else None}
    return selected, metadata


def instructions(missing: list[str]) -> str:
    status = "草稿：缺少以下材料，不能作为最终提交：" + "、".join(missing) if missing else "本地材料已打包并校验；仍须完成下列参赛核对。"
    return f"""# 传智杯材料说明

作品：颐邻——社区智慧养老服务与健康监测平台
参赛人：{NAME}；本科B组；参赛编号：{ENTRY}。
状态：{status}

用户于2026年11月12日确认以上组别、编号及允许使用已有项目；未独立核验主办方答复原文。

## 内容与运行

- `eldercare-platform/`：源码、测试、文档、依赖清单、环境变量模板和选定验证证据。
- `技术文档/2132417-唐克波-技术文档.pdf`：技术文档（存在时）。
- `演示视频/2132417-唐克波-演示视频.mp4`：演示视频（存在时）。
- `manifest.json`：除清单自身外全部成员的字节数与SHA-256。ZIP旁附整个归档的SHA-256。

解压后进入`eldercare-platform`，执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run_dev.ps1
```

Python需预安装；首次运行安装依赖并初始化合成演示数据。详细前提与故障处理见README及`docs/chuanzhi/快速开始.md`。
浏览器访问http://127.0.0.1:8000；演示账号super、grid1、nurse1、elder1等，密码Admin@123，仅用于演示。

## 必须保留的说明

公开仓库：https://github.com/KEBO-666/eldercare-platform 。发布状态、匿名访问实测和版本关系见`docs/chuanzhi/GitHub发布记录.md`。公开仓库采用独立过滤副本，排除全部历史中的运行数据与缓存，原本地仓库保留；本包不含`.git`或Git bundle，不能替代真实细粒度历史。旧PDF/视频中的未发布表述是发布前快照，以发布记录为准。

已取得真实优化轮及三个核心主题的索引，尚未证实三条独立完整人类Prompt链。部分委派正文在本地会话中为加密内容；导出未解密、未伪造。请阅读`docs/chuanzhi/AI协同与演进索引.md`。

设备数据及短信通知含模拟实现；实际验证范围、未完成功能和规则事项见`docs/chuanzhi/来源与能力边界.md`、`规则核实与时间线.md`及`提交核对.md`。

源码与报告采用白名单，排除实际`.env`、数据库/WAL/SHM、上传附件、依赖缓存、虚拟环境和认证文件。字节校验仅证明包内文件完整，不能证明比赛资格、视频内容或所有功能合规。

本包是本地审阅材料，未上传赛事平台，不是正式提交回执。原Python赛事归档未覆盖。
"""


def verify_submission(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate ZIP members")
        for name in names:
            if not safe_path(name):
                raise ValueError("Excluded or unsafe ZIP member: " + name)
        bad = archive.testzip()
        if bad is not None:
            raise ValueError("ZIP CRC failure: " + bad)
        manifest = json.loads(archive.read("manifest.json"))
        records = manifest["files"]
        expected = {record["path"] for record in records} | {"manifest.json"}
        if set(names) != expected or len(records) != len(expected) - 1:
            raise ValueError("Manifest members differ from ZIP members")
        for record in records:
            info = archive.getinfo(record["path"])
            with archive.open(info) as stream:
                value = hashlib.file_digest(stream, "sha256").hexdigest()
            if info.file_size != record["bytes"] or value != record["sha256"]:
                raise ValueError("Member hash or size mismatch: " + record["path"])
        return {"archive_members": len(names), "hashed_files": len(records),
                "missing_artifacts": manifest["missing_artifacts"], "crc_and_sha256": "passed"}


def build_submission(
    output: Path, *, root: Path = ROOT, pdf: Path | None = None, video: Path | None = None,
    browser_run: Path | None = None, allow_missing: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    selected, metadata = collect_sources(root, browser_run)
    pdf = pdf or root / "output" / "pdf" / "2132417-唐克波-技术文档.pdf"
    video = video or root / "output" / "video" / "2132417-唐克波-演示视频.mp4"
    missing = []
    for label, path, folder, suffix in (("PDF", pdf, "技术文档", ".pdf"), ("MP4", video, "演示视频", ".mp4")):
        if not path.is_file():
            missing.append(label)
            continue
        if path.suffix.lower() != suffix or path.stat().st_size == 0:
            raise ValueError("Invalid artifact: " + str(path))
        with path.open("rb") as stream:
            header = stream.read(32)
        if suffix == ".pdf" and not header.startswith(b"%PDF-"):
            raise ValueError("PDF header missing: " + str(path))
        if suffix == ".mp4" and header[4:8] != b"ftyp":
            raise ValueError("MP4 ftyp header missing: " + str(path))
        selected[folder + "/" + path.name] = path
    if missing and not allow_missing:
        raise FileNotFoundError("Missing required artifacts: " + ", ".join(missing))
    if missing and "草稿" not in output.stem and "draft" not in output.stem.casefold():
        raise ValueError("A missing-artifact preview must use a 草稿 or draft filename")
    payloads: dict[str, bytes | Path] = {**selected, "提交说明.md": instructions(missing).encode("utf-8")}
    records = [{"path": name, "bytes": value.stat().st_size if isinstance(value, Path) else len(value),
                "sha256": file_hash(value) if isinstance(value, Path) else hashlib.sha256(value).hexdigest()}
               for name, value in sorted(payloads.items())]
    manifest = {
        "format_version": 1, "entry": ENTRY, "name": NAME, "group": "本科B组",
        "purpose": "local review material; not a contest upload receipt",
        "determinism": "sorted paths, fixed ZIP timestamps, stable manifest for identical inputs",
        "git_head_at_packaging": git_head(root), "git_history_included": False,
        "missing_artifacts": missing, "selection": metadata, "files": records,
    }
    payloads["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".zip.building")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, value in sorted(payloads.items()):
            if not safe_path(name):
                raise ValueError("Unsafe selected member: " + name)
            info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED if name.lower().endswith(".mp4") else zipfile.ZIP_DEFLATED
            if isinstance(value, Path):
                with value.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(block)
            else:
                archive.writestr(info, value)
    result = verify_submission(temporary)
    temporary.replace(output)
    result.update(path=str(output.resolve()), bytes=output.stat().st_size, sha256=file_hash(output))
    output.with_suffix(".zip.sha256").write_text(result["sha256"] + "  " + output.name + "\n", encoding="utf-8")
    return result


def self_check() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="chuanzhi-package-selfcheck-") as directory:
        root = Path(directory)
        for name in SOURCE_FILES:
            (root / name).write_text("demo\n", encoding="utf-8")
        (root / "app").mkdir()
        (root / "app" / "main.py").write_text("print('demo')\n", encoding="utf-8")
        for name in (".env", "secret.db", "secret.db-wal", "auth.json"):
            (root / "app" / name).write_text("EXCLUDED", encoding="utf-8")
        first = build_submission(root / "draft-one.zip", root=root, allow_missing=True)
        second = build_submission(root / "draft-two.zip", root=root, allow_missing=True)
        assert first["sha256"] == second["sha256"], "Non-deterministic ZIP"
        rejected = False
        try:
            build_submission(root / "final.zip", root=root)
        except FileNotFoundError:
            rejected = True
        assert rejected, "Missing final artifacts accepted"
        with zipfile.ZipFile(root / "draft-one.zip") as archive:
            assert "eldercare-platform/app/main.py" in archive.namelist()
            assert not any("secret.db" in name or name.endswith("auth.json") for name in archive.namelist())
        return {"status": "passed", "checks": ["deterministic bytes", "whitelist exclusions", "missing final artifacts rejected",
                                                   "ZIP CRC", "per-member SHA-256", "manifest exact membership"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "2132417-唐克波-传智杯材料.zip")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--video", type=Path)
    parser.add_argument("--browser-run", type=Path)
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        result = self_check()
    elif args.verify:
        result = verify_submission(args.verify)
    else:
        result = build_submission(args.output, pdf=args.pdf, video=args.video,
                                  browser_run=args.browser_run, allow_missing=args.allow_missing)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
