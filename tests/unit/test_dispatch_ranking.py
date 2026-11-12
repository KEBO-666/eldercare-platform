"""派单真实排序边界：显示舍入、硬约束、默认评价与解释可复算。"""
from __future__ import annotations

import itertools
import math

import pytest

from app.algorithms.dispatch import (
    FACTOR_NAMES,
    FOCUS_PENALTY,
    candidate_rejection_reason,
    haversine,
    normalize_skills,
    parse_weights,
    score_candidates,
    skill_factor,
)


def candidate(staff_id=1, **values):
    return {
        "staff_id": staff_id, "name": f"护理员{staff_id}", "skills": ["shower"],
        "distance_km": 0.5, "today_count": 0, "rating": 4.0, **values,
    }


def ids(rows):
    return [row["staff_id"] for row in rows]


def test_overflowing_weight_sum_preserves_caller_fallback_and_invalid_source_detection():
    # Service detects parse failure with two distinct fallback configurations.
    # Returning the built-in default for both would label this as valid runtime config.
    weights = "1e308,1e308,1e308,1e308"
    skill_fallback = parse_weights(weights, (1, 0, 0, 0))
    distance_fallback = parse_weights(weights, (0, 1, 0, 0))
    assert skill_fallback == (1, 0, 0, 0)
    assert distance_fallback == (0, 1, 0, 0)
    assert skill_fallback != distance_fallback


def test_score_rounding_collision_cannot_promote_the_lower_scoring_nearer_candidate():
    rows = score_candidates([
        candidate(1, distance_km=0.1, rating=4.0),
        candidate(2, distance_km=0.2, rating=4.00001),
    ], weights=(0, 0, 0, 1), top_n=1)
    assert ids(rows) == [2]
    assert rows[0]["score"] == 0.8
    assert rows[0]["score_precision"] == pytest.approx(4.00001 / 5, abs=1e-15)
    assert rows[0]["rank"] == 1


def test_raw_factor_precision_changes_winner_even_when_a_lower_score_is_nearer():
    # Each displayed factor and final score collides, but the exact weighted
    # rating advantage outweighs the distance disadvantage of candidate 2.
    rows = score_candidates([
        candidate(1, distance_km=0.30001, rating=4.0),
        candidate(2, distance_km=0.30002, rating=4.0001),
    ], weights=(0, 0.5, 0, 0.5))
    assert ids(rows) == [2, 1]
    assert rows[0]["factors"] == rows[1]["factors"]
    assert rows[0]["score"] == rows[1]["score"]
    assert rows[0]["score_precision"] > rows[1]["score_precision"]


def test_exact_score_tie_prefers_unrounded_distance_before_id():
    rows = score_candidates([candidate(1, distance_km=0.50004), candidate(2, distance_km=0.50003)], weights=(1, 0, 0, 0))
    assert ids(rows) == [2, 1]
    assert rows[0]["distance_km"] == rows[1]["distance_km"]


def test_equal_score_and_distance_use_service_day_load_then_staff_id_stably():
    candidates = [candidate(2, today_count=1), candidate(1, today_count=5), candidate(3, today_count=1)]
    for permutation in itertools.permutations(candidates):
        rows = score_candidates(permutation, weights=(1, 0, 0, 0))
        assert ids(rows) == [2, 3, 1]
        assert [row["rank"] for row in rows] == [1, 2, 3]
        assert ids(score_candidates(permutation, weights=(1, 0, 0, 0), top_n=2)) == [2, 3]


def test_explanation_reconstructs_unrounded_base_score_and_focus_penalty():
    rows = score_candidates([
        candidate(1, distance_km=0.731234567, today_count=2, rating=4.123456789, is_focus=True),
        candidate(2, distance_km=0.731234567, today_count=2, rating=4.123456789),
    ], weights=(1, 2, 3, 7), required_skills=["shower"])
    assert ids(rows) == [2, 1]
    normal, focus = rows
    for row in rows:
        expected_factors = {
            "skill": 1.0, "distance": 1 - 0.731234567 / 3,
            "load": 1 - 2 / 6, "rating": 4.123456789 / 5,
        }
        assert row["factors_precision"] == pytest.approx(expected_factors, abs=1e-15)
        assert row["weights_precision"] == pytest.approx([1 / 13, 2 / 13, 3 / 13, 7 / 13], abs=1e-15)
        for name, weight in zip(FACTOR_NAMES, row["weights_precision"], strict=True):
            assert row["contributions"][name] == row["factors_precision"][name] * weight
        assert row["base_score"] == sum(row["contributions"].values())
        assert row["score_precision"] == row["base_score"] * row["penalty"]
        assert row["score"] == round(row["score_precision"], 4)
    assert focus["penalty"] == FOCUS_PENALTY
    assert focus["score_precision"] == normal["score_precision"] * FOCUS_PENALTY
    assert "重点关注降权" in focus["reason"]


def test_new_staff_default_rating_is_explicit_and_equal_to_the_same_real_rating():
    rows = score_candidates([candidate(1, rating=None), candidate(2, rating=3.75)], rating_default=3.75)
    newcomer, evaluated = rows
    assert newcomer["rating"] is None
    assert newcomer["rating_used"] == 3.75
    assert newcomer["rating_is_default"] is True
    assert newcomer["score_precision"] == evaluated["score_precision"]
    assert evaluated["rating_is_default"] is False
    assert "近30天无有效评价" in newcomer["reason"]
    assert "默认评分3.8" in newcomer["reason"]
    assert "服务当日已派0单" in newcomer["reason"]
    assert "今日已派" not in newcomer["reason"]


def test_hard_skills_filter_is_not_relaxed_by_rating_or_skill_weight():
    rows = score_candidates([
        candidate(1, skills=[], rating=5.0), candidate(2, rating=3.0),
    ], weights=(0, 0, 0, 1), required_skills=["shower"])
    assert ids(rows) == [2]
    assert rows[0]["factors_precision"]["skill"] == 1.0
    assert "合格者技能项等量计分" in rows[0]["reason"]


def test_skill_normalization_removes_blank_and_duplicate_tags_without_changing_coverage():
    required = [" shower ", "", "   ", "shower", "bedcare "]
    assert normalize_skills(required) == {"shower", "bedcare"}
    assert skill_factor(required, [" shower ", ""]) == 0.5
    assert skill_factor(required, [" shower ", " bedcare ", ""]) == 1.0


def test_whitespace_skill_tags_are_consistent_in_filter_scoring_and_reason():
    row = candidate(skills=[" shower ", "bedcare", ""])
    required = ["  shower", " ", "bedcare ", "shower"]
    assert candidate_rejection_reason(row, required_skills=required) is None
    result = score_candidates([row], required_skills=required)[0]
    assert result["factors_precision"]["skill"] == 1.0
    assert "技能匹配 bedcare/shower" in result["reason"]
    assert score_candidates([candidate(skills=[])], required_skills=["", "   "])[0]["factors_precision"]["skill"] == 1.0


def test_weight_changes_ranking_after_the_same_hard_filter():
    candidates = [candidate(1, distance_km=0.1, rating=2.0), candidate(2, distance_km=2.5, rating=5.0)]
    assert ids(score_candidates(candidates, weights=(0, 1, 0, 0))) == [1, 2]
    assert ids(score_candidates(candidates, weights=(0, 0, 0, 1))) == [2, 1]


def test_allowed_radius_and_daily_limit_explain_the_effective_constraints():
    row = score_candidates([candidate(service_radius_km=1.5, daily_limit=4, today_count=1)], radius_km=2.0, daily_limit=6)[0]
    assert row["allowed_radius_km"] == 1.5
    assert row["daily_limit"] == 4
    assert row["factors_precision"]["distance"] == pytest.approx(1 - 0.5 / 1.5)
    assert row["factors_precision"]["load"] == 0.75


@pytest.mark.parametrize(("values", "expected"), [
    ({"status": "inactive", "skills": [], "distance_km": None}, "inactive"),
    ({"skills": [], "distance_km": None}, "missing_skills"),
    ({"distance_km": None, "today_count": 9}, "invalid_location"),
    ({"distance_km": 4.0, "today_count": 9}, "out_of_radius"),
    ({"today_count": 6}, "daily_limit"),
    ({"today_count": -1}, "daily_limit"),
    ({"daily_limit": 0}, "daily_limit"),
    ({"today_count": float("nan")}, "daily_limit"),
    ({"distance_km": 3.0, "today_count": 5}, None),
])
def test_shared_rejection_reason_matches_filter_and_first_failure(values, expected):
    row = candidate(**values)
    assert candidate_rejection_reason(row, required_skills=["shower"], radius_km=3, daily_limit=6) == expected
    assert bool(score_candidates([row], required_skills=["shower"], radius_km=3, daily_limit=6)) is (expected is None)


@pytest.mark.parametrize("distance", [None, float("nan"), float("inf"), -float("inf"), -0.01, "bad", {}])
def test_invalid_distance_cannot_enter_candidates(distance):
    row = candidate(distance_km=distance)
    assert candidate_rejection_reason(row) == "invalid_location"
    assert score_candidates([row]) == []


@pytest.mark.parametrize("radius", [0, -1, float("nan"), float("inf"), "bad"])
def test_invalid_caller_or_staff_radius_cannot_bypass_filter(radius):
    assert candidate_rejection_reason(candidate(), radius_km=radius) == "invalid_location"
    assert candidate_rejection_reason(candidate(service_radius_km=radius)) == "invalid_location"
    assert score_candidates([candidate()], radius_km=radius) == []
    assert score_candidates([candidate(service_radius_km=radius)]) == []


@pytest.mark.parametrize("coordinates", [
    (None, 121, 31, 121), (float("nan"), 121, 31, 121),
    (31, float("inf"), 31, 121), (31, 121, float("nan"), 121),
    (31, 121, 31, -float("inf")), (91, 121, 31, 121),
    (31, -181, 31, 121), (31, 121, -91, 121),
    (31, 121, 31, 181), ("not a latitude", 121, 31, 121),
    (True, 121, 31, 121),
])
def test_invalid_coordinates_are_unreachable_instead_of_zero_distance(coordinates):
    distance = haversine(*coordinates)
    assert math.isinf(distance)
    assert score_candidates([candidate(distance_km=distance)]) == []


def test_haversine_preserves_valid_coordinate_boundaries():
    assert haversine(90, 180, 90, -180) == pytest.approx(0, abs=1e-9)
    assert haversine(0, 0, 0, 180) == pytest.approx(math.pi * 6371.0088)


@pytest.mark.parametrize("rating", [float("nan"), float("inf"), "bad"])
def test_invalid_rating_uses_documented_default_without_nan_in_scores(rating):
    row = score_candidates([candidate(rating=rating)], rating_default=3.5)[0]
    assert row["rating_is_default"] is True
    assert row["rating_used"] == 3.5
    assert math.isfinite(row["score_precision"])
    assert row["factors_precision"]["rating"] == 0.7
