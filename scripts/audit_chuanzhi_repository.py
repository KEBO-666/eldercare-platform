"""Inspect repository publication risks without emitting credential contents.

Scans first-party text in every reachable commit and the current working tree.
Runtime DB sidecars are classified by path/size only; caches/vendor assets are
inventoried, not parsed as code. Findings are lexical signals, not a clearance.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIRST_PARTY = {"app", "scripts", "tests", "docs", "docker"}
ROOT_FILES = {"README.md", "requirements.txt", "pyproject.toml", "pytest.ini", ".env.example", ".gitignore"}
TEXT_EXTENSIONS = {".py", ".md", ".txt", ".json", ".jsonl", ".yml", ".yaml", ".toml", ".ini", ".ps1", ".html", ".css", ".js", ".patch"}
KNOWN_DEMO = {
    "Admin@123", "dev-secret-change-me-in-production", "dev-hash-key-change-me",
    "ZGV2LWtleS1jaGFuZ2UtbWUtaW4tcHJvZHVjdGlvbi0zMi1i", "postgres", "test", "test-secret",
}
PATTERNS = {
    "private_key_header": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "jwt_literal": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
    "api_key_literal": re.compile(r"\b(?:sk|ghp|github_pat)[-_][A-Za-z0-9_-]{20,}\b"),
    "credential_url": re.compile(r"\b(?:postgres(?:ql)?(?:\+[A-Za-z0-9]+)?|mysql|https?)://[A-Za-z0-9_.%+-]+:([A-Za-z0-9_.%+!~*-]+)@"),
    "credential_assignment": re.compile(
        r"(?i)\b(?:SECRET_KEY|ENCRYPTION_KEY|FIELD_HASH_KEY|API_KEY|ACCESS_TOKEN|PASSWORD)\b"
        r"\s*(?::\s*str)?\s*=\s*([\"']?)([^\s\"'#,;\r\n]+)"
    ),
}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL)


def classify(path: str) -> str:
    lower = path.casefold()
    parts = Path(path).parts
    if "pip-cache" in parts or lower.endswith((".whl", ".pyc")) or "__pycache__" in parts:
        return "dependency_cache"
    if lower.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm")):
        return "runtime_database_or_sidecar"
    if Path(path).name.casefold() in {".env", "auth.json", "credentials.json"} or lower.endswith((".pem", ".key")):
        return "private_config_path"
    if lower.startswith("app/static/vendor/") or "/licenses/" in lower:
        return "third_party_notice_or_vendor"
    if (parts and parts[0] in FIRST_PARTY or path in ROOT_FILES) and (
        Path(path).suffix.casefold() in TEXT_EXTENSIONS or path in ROOT_FILES or Path(path).name == "Dockerfile"
    ):
        return "first_party_text"
    return "other"


def scan(text: str, path: str) -> list[dict[str, Any]]:
    findings = []
    for name, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            if name == "credential_assignment":
                value = match.group(2)
            elif name == "credential_url":
                value = match.group(1)
            else:
                value = match.group(0)
            demo = value in KNOWN_DEMO
            if name == "credential_assignment" and not demo:
                # Expressions, annotations and placeholders are not literal secrets.
                if value.startswith(("os.", "settings.", "get_", "str(", "Field(", "f\"", "{", "$", "None")):
                    continue
            findings.append({
                "path": path, "line": text.count("\n", 0, match.start()) + 1,
                "signal": name, "classification": "known_demo_value" if demo else "review_required_lexical_signal",
                "credential_value_in_report": False,
            })
    return findings


def inventory(entries: list[dict[str, Any]]) -> dict[str, Any]:
    categories: dict[str, Any] = {}
    for entry in entries:
        group = categories.setdefault(entry["category"], {"count": 0, "bytes": 0, "examples": []})
        group["count"] += 1
        group["bytes"] += entry["bytes"]
        if len(group["examples"]) < 8:
            group["examples"].append({"path": entry["path"], "bytes": entry["bytes"]})
    return categories


def audit() -> dict[str, Any]:
    commits = git("rev-list", "--reverse", "HEAD").decode().splitlines()
    scans: dict[str, list[dict[str, Any]]] = {}
    versions = []
    for commit in commits:
        entries = []
        findings = []
        skipped_large = []
        for record in git("ls-tree", "-r", "-l", "-z", commit).split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, object_id, raw_size = metadata.split()
            path = raw_path.decode("utf-8")
            size = int(raw_size)
            category = classify(path)
            entries.append({"path": path, "bytes": size, "category": category})
            if kind != b"blob" or category != "first_party_text":
                continue
            if size > 2_000_000:
                skipped_large.append(path)
                continue
            oid = object_id.decode()
            cache_key = oid + ":" + path
            if cache_key not in scans:
                scans[cache_key] = scan(git("cat-file", "blob", oid).decode("utf-8", errors="replace"), path)
            findings.extend(scans[cache_key])
        versions.append({
            "commit": commit, "metadata": git("show", "-s", "--format=%aI %s", commit).decode("utf-8").strip(),
            "inventory": inventory(entries), "findings": findings, "unscanned_large_text": skipped_large,
        })
    current_findings = []
    current_entries = []
    current_large = []
    for directory in FIRST_PARTY:
        for path in (ROOT / directory).rglob("*"):
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(ROOT).as_posix()
                category = classify(relative)
                size = path.stat().st_size
                current_entries.append({"path": relative, "bytes": size, "category": category})
                if category == "first_party_text":
                    if size <= 2_000_000:
                        current_findings.extend(scan(path.read_text(encoding="utf-8", errors="replace"), relative))
                    else:
                        current_large.append(relative)
    for name in ROOT_FILES:
        path = ROOT / name
        if path.is_file():
            current_entries.append({"path": name, "bytes": path.stat().st_size, "category": classify(name)})
            current_findings.extend(scan(path.read_text(encoding="utf-8", errors="replace"), name))
    for path in ROOT.iterdir():
        if path.is_file() and classify(path.name) == "private_config_path":
            current_entries.append({"path": path.name, "bytes": path.stat().st_size, "category": "private_config_path"})
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "review_required_not_publication_clearance",
        "remote_names": git("remote").decode().splitlines(), "commit_count": len(commits),
        "historical_versions": versions, "current_worktree_findings": current_findings,
        "current_selected_inventory": inventory(current_entries), "current_unscanned_large_text": current_large,
        "signal_summary": dict(Counter(f["classification"] for f in current_findings)),
        "credential_contents_emitted": False, "history_changed": False, "published": False,
        "limitations": [
            "Regex signals need contextual review; absence of matches does not prove no secret exists.",
            "Known demo values are explicitly classified, not certified suitable for production.",
            "Database/WAL/SHM are classified by path and bytes only; contents are not inspected or declared safe.",
            "Dependency caches and vendor assets are inventoried, not searched as first-party code.",
            "Report omits all credential bodies, matched snippets and private database records.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "chuanzhi-history-audit.json")
    args = parser.parse_args()
    result = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.output), "status": result["status"],
                      "commits": result["commit_count"], "current_signal_summary": result["signal_summary"],
                      "credential_contents_emitted": False}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
