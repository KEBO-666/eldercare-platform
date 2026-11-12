"""智能派单评分算法（纯函数）。

设计要点：
- 本模块**不依赖数据库 / FastAPI**，输入 dict/list/数值，输出 dict/list/数值，便于单测；
- 四因子：技能匹配 skill、距离 distance、负荷 load、评分 rating，按权重加权求和；
- 权重可通过配置覆盖（默认 0.4/0.3/0.2/0.1），支持 "0.4,0.3,0.2,0.1"、JSON 数组、JSON 对象三种写法；
- 重点关注护理员（is_focus，3 次投诉成立）最终得分再乘 0.8 降权。

对外函数：
    haversine(lat1, lng1, lat2, lng2) -> float                # 直线距离（km）
    skill_factor(required, staff_skills) -> float             # 技能覆盖率 [0,1]
    distance_factor(distance_km, radius_km) -> float          # 距离得分 [0,1]
    load_factor(today_count, limit) -> float                  # 负荷得分 [0,1]
    rating_factor(rating, default) -> float                   # 评分得分 [0,1]
    weighted_score(factors, weights) -> float                 # 加权总分
    parse_weights(raw, default) -> tuple[float, float, float, float]
    score_candidates(candidates, weights, ...) -> list[dict]  # 过滤 + 打分 + 排序 + TopN
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

EARTH_RADIUS_KM = 6371.0088
DEFAULT_WEIGHTS: tuple[float, float, float, float] = (0.4, 0.3, 0.2, 0.1)
FACTOR_NAMES: tuple[str, str, str, str] = ("skill", "distance", "load", "rating")
FOCUS_PENALTY = 0.8
RATING_FULL_SCORE = 5.0
DEFAULT_RADIUS_KM = 3.0
DEFAULT_DAILY_LIMIT = 6
DEFAULT_RATING = 3.5
SCORE_DIGITS = 4

# 权重配置里允许的键名（兼容 xxx 与 xxx_factor）
_WEIGHT_ALIASES: dict[str, str] = {
    "skill": "skill",
    "skill_factor": "skill",
    "skills": "skill",
    "distance": "distance",
    "distance_factor": "distance",
    "load": "load",
    "load_factor": "load",
    "rating": "rating",
    "rating_factor": "rating",
    "score": "rating",
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _normalize(weight_values: Iterable[float]) -> tuple[float, float, float, float]:
    values = [float(v) for v in weight_values]
    total = sum(values)
    if len(values) != 4 or not math.isfinite(total) or total <= 0 or any(v < 0 for v in values):
        return DEFAULT_WEIGHTS
    return (values[0] / total, values[1] / total, values[2] / total, values[3] / total)


# ---------------- 基础因子 ----------------
def haversine(lat1: float | None, lng1: float | None, lat2: float | None, lng2: float | None) -> float:
    """两点间球面直线距离（单位 km）；坐标缺失返回 inf（视为不可达）。"""
    if lat1 is None or lng1 is None or lat2 is None or lng2 is None:
        return math.inf
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    d_phi = phi2 - phi1
    d_lambda = math.radians(float(lng2) - float(lng1))
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def skill_factor(required: Sequence[str] | None, staff_skills: Sequence[str] | None) -> float:
    """技能覆盖率：必需技能全部覆盖得 1.0，部分覆盖按比例，无要求得 1.0。"""
    required_list = [str(s) for s in (required or []) if str(s).strip()]
    if not required_list:
        return 1.0
    owned = {str(s) for s in (staff_skills or [])}
    covered = sum(1 for skill in required_list if skill in owned)
    return _clamp(covered / len(required_list))


def distance_factor(distance_km: float, radius_km: float = DEFAULT_RADIUS_KM) -> float:
    """距离得分：越近越高；超出服务半径得 0；半径非法时仅 0 距离得满分。"""
    distance = float(distance_km)
    radius = float(radius_km)
    if not math.isfinite(distance):
        return 0.0
    if radius <= 0:
        return 1.0 if distance <= 0 else 0.0
    return _clamp(1.0 - max(0.0, distance) / radius)


def load_factor(today_count: int, limit: int = DEFAULT_DAILY_LIMIT) -> float:
    """当日负荷得分：已派 0 单得 1.0，达到上限得 0。"""
    daily_limit = int(limit)
    if daily_limit <= 0:
        return 0.0
    return _clamp(1.0 - max(0, int(today_count)) / daily_limit)


def rating_factor(rating: float | None, default: float = DEFAULT_RATING) -> float:
    """评分得分：评分/5，缺失时用默认评分。"""
    value = default if rating is None else float(rating)
    return _clamp(value / RATING_FULL_SCORE)


# ---------------- 权重与加权总分 ----------------
def parse_weights(
    raw: Any = None,
    default: Sequence[float] = DEFAULT_WEIGHTS,
) -> tuple[float, float, float, float]:
    """解析权重配置，失败时回退默认权重；结果始终归一化（和为 1）。

    支持：
        (0.4, 0.3, 0.2, 0.1) / [0.4, 0.3, 0.2, 0.1]
        "0.4,0.3,0.2,0.1"（也允许中文逗号）
        "[0.4, 0.3, 0.2, 0.1]"
        {"skill":0.4,"distance":0.3,"load":0.2,"rating":0.1}
    """
    fallback = list(default) if default is not None else list(DEFAULT_WEIGHTS)
    if raw is None:
        return _normalize(fallback)

    values = raw
    if isinstance(values, str):
        text = values.strip()
        if not text:
            return _normalize(fallback)
        if text.startswith(("[", "{")):
            try:
                values = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return _normalize(fallback)
        else:
            parts = [p.strip() for p in text.replace("，", ",").split(",") if p.strip()]
            try:
                values = [float(p) for p in parts]
            except ValueError:
                return _normalize(fallback)

    if isinstance(values, Mapping):
        ordered: list[float] = []
        for name in FACTOR_NAMES:
            matched = None
            for key, value in values.items():
                if _WEIGHT_ALIASES.get(str(key).strip().lower()) == name:
                    matched = value
                    break
            if matched is None:
                return _normalize(fallback)
            try:
                ordered.append(float(matched))
            except (TypeError, ValueError):
                return _normalize(fallback)
        return parse_weights(ordered, fallback)

    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        return _normalize(fallback)
    try:
        seq = [float(v) for v in values]
    except (TypeError, ValueError):
        return _normalize(fallback)
    if len(seq) != 4 or any(not math.isfinite(v) or v < 0 for v in seq) or sum(seq) <= 0:
        return _normalize(fallback)
    return _normalize(seq)


def weighted_score(
    factors: Mapping[str, float],
    weights: Any = None,
) -> float:
    """四项因子加权求和（因子缺失按 0 计）。"""
    parsed = parse_weights(weights)
    total = 0.0
    for index, name in enumerate(FACTOR_NAMES):
        try:
            value = float(factors.get(name, 0.0))
        except (TypeError, ValueError):
            value = 0.0
        total += _clamp(value) * parsed[index]
    return total


# ---------------- 候选打分 ----------------
def _build_reason(
    *,
    required: Sequence[str],
    distance_km: float,
    today_count: int,
    rating: float | None,
    default_rating: float,
    is_focus: bool,
) -> str:
    if required:
        skill_text = "技能匹配 " + "/".join(str(s) for s in required)
    else:
        skill_text = "项目无技能要求"
    rating_value = default_rating if rating is None else float(rating)
    parts = [
        skill_text,
        f"距离{float(distance_km):.2f}km",
        f"今日已派{int(today_count)}单",
        f"评分{rating_value:.1f}",
    ]
    if is_focus:
        parts.append("重点关注降权×0.8")
    return "；".join(parts)


def score_candidates(
    candidates: Iterable[Mapping[str, Any]],
    weights: Any = None,
    *,
    radius_km: float | None = None,
    daily_limit: int | None = None,
    rating_default: float = DEFAULT_RATING,
    required_skills: Sequence[str] | None = None,
    top_n: int | None = None,
) -> list[dict[str, Any]]:
    """筛选并打分候选护理员，返回按 score 倒序的明细列表。

    每个候选 dict 支持的键：
        staff_id, name, distance_km, today_count, skills, rating,
        is_focus, service_radius_km, status（可选，非 active 直接剔除）

    过滤规则（不满足直接剔除，便于"缺技能/超半径/已达上限被过滤"的断言）：
        1. status 若提供且不是 active；
        2. 必需技能未全部覆盖；
        3. 距离超出服务半径（取调用方 radius_km 与本人 service_radius_km 的较小值）；
        4. 当日已派工单数 >= 每日上限。
    """
    parsed_weights = parse_weights(weights)
    required = [str(s) for s in (required_skills or []) if str(s).strip()]
    default_limit = DEFAULT_DAILY_LIMIT if daily_limit is None else int(daily_limit)
    default_radius = DEFAULT_RADIUS_KM if radius_km is None else float(radius_km)

    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        status = candidate.get("status")
        if status is not None and str(status) != "active":
            continue

        skills = candidate.get("skills") or []
        skill_value = skill_factor(required, skills)
        if required and skill_value < 1.0 - 1e-9:
            continue

        raw_distance = candidate.get("distance_km")
        distance = math.inf if raw_distance is None else float(raw_distance)
        staff_radius = candidate.get("service_radius_km")
        allowed_radius = default_radius
        if staff_radius is not None:
            allowed_radius = min(allowed_radius, float(staff_radius))
        if not math.isfinite(distance) or distance > allowed_radius:
            continue

        today_count = int(candidate.get("today_count") or 0)
        limit = default_limit
        if candidate.get("daily_limit") is not None:
            limit = int(candidate["daily_limit"])
        if limit <= 0 or today_count >= limit:
            continue

        rating = candidate.get("rating")
        factors = {
            "skill": round(skill_value, SCORE_DIGITS),
            "distance": round(distance_factor(distance, allowed_radius), SCORE_DIGITS),
            "load": round(load_factor(today_count, limit), SCORE_DIGITS),
            "rating": round(rating_factor(rating, rating_default), SCORE_DIGITS),
        }
        score = weighted_score(factors, parsed_weights)
        is_focus = bool(candidate.get("is_focus"))
        if is_focus:
            score *= FOCUS_PENALTY

        scored.append(
            {
                "staff_id": candidate.get("staff_id"),
                "name": candidate.get("name") or "",
                "distance_km": round(distance, SCORE_DIGITS),
                "score": round(score, SCORE_DIGITS),
                "factors": factors,
                "contributions": {
                    name: round(factors[name] * parsed_weights[index], SCORE_DIGITS)
                    for index, name in enumerate(FACTOR_NAMES)
                },
                "penalty": FOCUS_PENALTY if is_focus else 1.0,
                "reason": _build_reason(
                    required=required,
                    distance_km=distance,
                    today_count=today_count,
                    rating=rating,
                    default_rating=rating_default,
                    is_focus=is_focus,
                ),
                "today_count": today_count,
                "rating": None if rating is None else round(float(rating), SCORE_DIGITS),
                "is_focus": is_focus,
            }
        )

    scored.sort(key=lambda row: (-row["score"], row["distance_km"], row["staff_id"] or 0))
    if top_n is not None and top_n >= 0:
        return scored[: int(top_n)]
    return scored
