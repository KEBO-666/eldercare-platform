"""Refresh only this project's known current sessions after the agents finish.

Run from project root using .venv/Scripts/python.exe. The evidence agent's early
metadata-discovery records are excluded because they list other thread titles.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))
from scripts.export_project_evidence import export_evidence  # noqa: E402

SESSIONS = Path(r"C:\Users\Administrator\.codex\sessions\2026\11\12")
SPECS = (
    ("*16-15-40*", "current-root", 212),
    ("*17-02-30*", "current-reproduction", 59),
    ("*17-02-48*", "current-browser", 62),
    ("*17-03-06*", "current-evidence", 91),
)
summary = []
for pattern, name, start in SPECS:
    matches = list(SESSIONS.glob(pattern))
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one known session: {pattern}")
    source = matches[0]
    end = len(source.read_bytes().splitlines())
    manifest = export_evidence(source, start, end, HERE / name)
    summary.append({
        "name": name, "source_file": source.name,
        "selected_lines": manifest["selected_lines"],
        "exported_records": manifest["exported_records"],
        "exclusions": manifest["exclusions"], "redactions": manifest["redactions"],
    })
result = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "snapshots": summary}
(ROOT / "reports" / "chuanzhi-evidence-current-snapshot.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps(result, ensure_ascii=True, indent=2))
