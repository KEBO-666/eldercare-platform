"""Create an independent, allowlisted history export without changing source Git.

Requires git-filter-repo installed separately under reports/github-tools.
The export removes runtime/cache objects from every commit. It does not invent
development history; original and exported commit IDs are mapped explicitly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {"README.md", "requirements.txt", "pyproject.toml", "pytest.ini", ".env.example", ".gitignore"}
DIRECTORIES = {"app", "scripts", "tests", "docs", "docker"}
BLOCKED_PARTS = {"__pycache__", ".venv", "node_modules", ".pytest_cache", ".ruff_cache", "pip-cache", ".git"}
BLOCKED_SUFFIXES = (".pyc", ".pyo", ".whl", ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".zip")


def run(*args: str, cwd: Path = ROOT) -> bytes:
    result = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def allowed(path: str) -> bool:
    pure = PurePosixPath(path)
    return bool(
        not pure.is_absolute() and ".." not in pure.parts
        and not BLOCKED_PARTS.intersection(pure.parts)
        and pure.name != ".env" and not path.lower().endswith(BLOCKED_SUFFIXES)
        and (path in ROOT_FILES or pure.parts[0] in DIRECTORIES)
    )


def export(target: Path) -> dict:
    target = target.resolve()
    reports = (ROOT / "reports").resolve()
    if reports not in target.parents or target.exists():
        raise ValueError("Use a new export directory strictly inside project reports/")
    original_head = run("git", "rev-parse", "HEAD").decode().strip()
    original_commits = run("git", "rev-list", "--reverse", "HEAD").decode().splitlines()
    selected = set()
    excluded = set()
    for commit in original_commits:
        for raw in run("git", "ls-tree", "-rz", "--name-only", commit).split(b"\0"):
            if raw:
                path = raw.decode("utf-8")
                (selected if allowed(path) else excluded).add(path)
    paths = reports / "github-public-allowed-paths.txt"
    paths.write_text("".join("literal:" + path + "\n" for path in sorted(selected)), encoding="utf-8")
    run("git", "clone", "--no-local", "--no-hardlinks", str(ROOT), str(target))
    tool = reports / "github-tools/git_filter_repo.py"
    run(sys.executable, str(tool), "--force", "--paths-from-file", str(paths), "--prune-empty", "never",
        "--prune-degenerate", "never", "--preserve-commit-hashes", cwd=target)
    mapping = []
    for line in (target / ".git/filter-repo/commit-map").read_text(encoding="utf-8").splitlines()[1:]:
        old, new = line.split()
        if old in original_commits:
            original = run("git", "show", "-s", "--format=%aI%x00%cI%x00%s", old).decode("utf-8").strip().split("\0")
            mapping.append({"original": old, "public_export": new, "author_date": original[0],
                            "committer_date": original[1], "subject": original[2]})
    mapping.sort(key=lambda item: original_commits.index(item["original"]))
    if len(mapping) != len(original_commits):
        raise RuntimeError("An original commit is missing from export mapping")
    for record in mapping:
        for path in run("git", "ls-tree", "-rz", "--name-only", record["public_export"], cwd=target).split(b"\0"):
            if path and not allowed(path.decode("utf-8")):
                raise RuntimeError("Excluded path remains in exported history")
    exported_head = run("git", "rev-parse", "HEAD", cwd=target).decode().strip()
    for path in run("git", "ls-tree", "-rz", "--name-only", exported_head, cwd=target).split(b"\0"):
        if path:
            original = run("git", "rev-parse", original_head + ":" + path.decode("utf-8")).strip()
            exported = run("git", "rev-parse", exported_head + ":" + path.decode("utf-8"), cwd=target).strip()
            if original != exported:
                raise RuntimeError("Export changed retained file contents")
    if run("git", "rev-parse", "HEAD").decode().strip() != original_head:
        raise RuntimeError("Source HEAD unexpectedly changed during independent export")
    result = {
        "created_utc": datetime.now(timezone.utc).isoformat(), "source_head": original_head,
        "export_head": exported_head, "export_directory": str(target), "source_history_unchanged": True,
        "retained_file_count_union": len(selected), "excluded_path_count_union": len(excluded),
        "excluded_paths": sorted(excluded), "commits": mapping,
        "retained_head_blob_ids_equal": True,
        "notice": "Filtered derivative history: commit IDs change; actual metadata and order retained. No invented early iterations.",
    }
    report = reports / "github-public-export.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / ("github-public-" + datetime.now().strftime("%Y%m%d-%H%M%S")))
    args = parser.parse_args()
    result = export(args.output)
    print(json.dumps({key: value for key, value in result.items() if key not in {"commits", "excluded_paths"}}, ensure_ascii=False, indent=2))
