r"""Reproduce real ranking defects against the read-only b8fd6dd algorithm.

Run from project root:
    .\.venv\Scripts\python.exe -B reports/dispatch-iteration-baseline/compare_dispatch.py
No database, Git mutation or network access is performed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "app/algorithms/dispatch.py").is_file())
OUTPUT = Path(__file__).resolve().parent
REVISION = "b8fd6dd"
SOURCE_PATH = "app/algorithms/dispatch.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate(staff_id, **values):
    return {"staff_id": staff_id, "distance_km": 0.5, "today_count": 0,
            "skills": ["shower"], "rating": 4.0, **values}


def summarize(rows):
    return [{key: row[key] for key in (
        "staff_id", "score", "distance_km", "today_count", "rank", "score_precision",
    ) if key in row} for row in rows]


def main():
    old_path = OUTPUT / "dispatch_original_b8fd6dd.py"
    source = old_path.read_bytes()
    assert hashlib.sha256(source).hexdigest() == "2f413efbe1f12b389e8c2e61ad7bcff89dd901ab0d2a3c16e11e2ea74438c27d", "Archived baseline differs from original Git blob"
    old = load("baseline_dispatch", old_path)
    new = load("current_dispatch", ROOT / SOURCE_PATH)
    cases = [
        ("score_rounding_collision", (0, 0, 0, 1), [
            candidate(1, distance_km=0.1, rating=4.0),
            candidate(2, distance_km=0.2, rating=4.00001),
        ]),
        ("distance_rounding_collision", (1, 0, 0, 0), [
            candidate(1, distance_km=0.50004), candidate(2, distance_km=0.50003),
        ]),
        ("equal_score_distance_service_day_load", (1, 0, 0, 0), [
            candidate(1, today_count=5), candidate(2, today_count=1),
        ]),
    ]
    comparisons = [{
        "case": name, "weights": weights, "candidates": candidates,
        "old": summarize(old.score_candidates(candidates, weights=weights)),
        "new": summarize(new.score_candidates(candidates, weights=weights)),
    } for name, weights, candidates in cases]
    coordinates = [float("nan"), 121.0, 31.0, 121.0]
    old_distance = old.haversine(*coordinates)
    new_distance = new.haversine(*coordinates)
    comparisons.append({
        "case": "nan_latitude_not_zero_distance",
        "coordinates": ["NaN", 121, 31, 121],
        "old_distance_km": old_distance,
        "new_distance_km": "Infinity" if math.isinf(new_distance) else new_distance,
        "old_candidate_ids": [row["staff_id"] for row in old.score_candidates([candidate(1, distance_km=old_distance)])],
        "new_candidate_ids": [row["staff_id"] for row in new.score_candidates([candidate(1, distance_km=new_distance)])],
    })
    result = {
        "baseline_revision": REVISION,
        "baseline_source_sha256": hashlib.sha256(source).hexdigest(),
        "current_source_sha256": hashlib.sha256((ROOT / SOURCE_PATH).read_bytes()).hexdigest(),
        "comparisons": comparisons,
        "scope": "Original ranking and coordinate defects only; no assertion about previously absent APIs.",
    }
    (OUTPUT / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
