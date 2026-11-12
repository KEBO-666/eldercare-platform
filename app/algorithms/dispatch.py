"""智能派单评分算法（纯函数）。

设计要点：
- 本模块**不依赖数据库 / FastAPI**，输入 dict/list/数值，输出 dict/list/数值，便于单测；
- 四因子：技能匹配 skill、距离 distance、负荷 load、评分 rating，按权重加权求和；
- 权重可通过配置覆盖（默认 0.4/0.3/0.2/0.1），支持 "0.4,0.3,0.2,0.1"、JSON 数组、JSON 对象三种写法；
- 重点关注护理员（is_focus，3 次投诉成立）最终得分再乘 0.8 降权。
- 技能先作为硬约束过滤，合格者 skill=1，因此技能权重对他们是等量贡献。
- 排序使用未舍入总分、距离、服务当日负荷及护理员编号；显示舍入不影响名次。

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


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _integer_number(value: Any) -> int | None:
    number = _finite_number(value)
    return int(number) if number is not None and number.is_integer() else None


def _normalize(weight_values: Iterable[float]) -> tuple[float, float, float, float]:
    values = [float(v) for v in weight_values]
    total = sum(values)
    if len(values) != 4 or not math.isfinite(total) or total <= 0 or any(v < 0 for v in values):
        return DEFAULT_WEIGHTS
    return (values[0] / total, values[1] / total, values[2] / total, values[3] / total)


# ---------------- 基础因子 ----------------
def haversine(lat1: float | None, lng1: float | None, lat2: float | None, lng2: float | None) -> float:
    """两点间球面直线距离（单位 km）；缺失、非有限或越界坐标返回 inf。"""
    coordinates = [_finite_number(value) for value in (lat1, lng1, lat2, lng2)]
    if any(value is None or abs(value) > bound for value, bound in zip(coordinates, (90, 180, 90, 180), strict=True)):
        return math.inf
    lat1, lng1, lat2, lng2 = coordinates
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(float(lng2) - float(lng1))
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def normalize_skills(skills: Sequence[str] | None) -> set[str]:
    """统一技能标签的首尾空格与空值，供推荐和最终派单共用。"""
    return {value for skill in (skills or []) if (value := str(skill).strip())}


def skill_factor(required: Sequence[str] | None, staff_skills: Sequence[str] | None) -> float:
    """技能覆盖率：必需技能全部覆盖得 1.0，部分覆盖按比例，无要求得 1.0。"""
    required_set = normalize_skills(required)
    if not required_set:
        return 1.0
    owned = normalize_skills(staff_skills)
    return _clamp(len(required_set & owned) / len(required_set))


def distance_factor(distance_km: float, radius_km: float = DEFAULT_RADIUS_KM) -> float:
    """距离得分：越近越高；超出服务半径得 0；半径非法时仅 0 距离得满分。"""
    distance = _finite_number(distance_km)
    radius = _finite_number(radius_km)
    if distance is None or distance < 0 or radius is None:
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
    """评分得分：评分/5，缺失或非法时用有效默认评分。"""
    value = _finite_number(rating)
    if value is None:
        value = _finite_number(default)
    if value is None:
        value = DEFAULT_RATING
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
    if len(seq) != 4 or any(not math.isfinite(v) or v < 0 for v in seq) or not math.isfinite(sum(seq)) or sum(seq) <= 0:
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
        value = _finite_number(factors.get(name, 0.0))
        if value is None:
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
        f"服务当日已派{int(today_count)}单",
        f"近30天无有效评价，采用默认评分{rating_value:.1f}" if rating is None else f"近30天评分{rating_value:.1f}",
        "技能为硬约束，合格者技能项等量计分",
    ]
    if is_focus:
        parts.append("重点关注降权×0.8")
    return "；".join(parts)


def _allowed_radius(candidate: Mapping[str, Any], radius_km: float | None) -> float | None:
    radius = _finite_number(DEFAULT_RADIUS_KM if radius_km is None else radius_km)
    if radius is None or radius <= 0:
        return None
    if candidate.get("service_radius_km") is not None:
        staff_radius = _finite_number(candidate["service_radius_km"])
        if staff_radius is None or staff_radius <= 0:
            return None
        radius = min(radius, staff_radius)
    return radius


def _daily_limit(candidate: Mapping[str, Any], daily_limit: int | None) -> int | None:
    configured = DEFAULT_DAILY_LIMIT if daily_limit is None else daily_limit
    return _integer_number(candidate.get("daily_limit") if candidate.get("daily_limit") is not None else configured)


def candidate_rejection_reason(
    candidate: Mapping[str, Any],
    *,
    required_skills: Sequence[str] | None = None,
    radius_km: float | None = None,
    daily_limit: int | None = None,
) -> str | None:
    """共享硬筛选，按固定顺序返回首个原因；服务层可据此解释淘汰数量。"""
    status = candidate.get("status")
    if status is not None and str(status) != "active":
        return "inactive"
    if skill_factor(required_skills, candidate.get("skills") or []) < 1.0:
        return "missing_skills"
    distance = _finite_number(candidate.get("distance_km"))
    radius = _allowed_radius(candidate, radius_km)
    if distance is None or distance < 0 or radius is None:
        return "invalid_location"
    if distance > radius:
        return "out_of_radius"
    count = _integer_number(candidate.get("today_count", 0))
    limit = _daily_limit(candidate, daily_limit)
    if count is None or count < 0 or limit is None or limit <= 0 or count >= limit:
        return "daily_limit"
    return None


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
    """先硬筛选，再按精确总分降序、距离/服务当日负荷/编号升序排名。

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
    required = sorted(normalize_skills(required_skills))
    default_rating = _finite_number(rating_default)
    default_rating = _clamp(DEFAULT_RATING if default_rating is None else default_rating, 0.0, RATING_FULL_SCORE)

    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate_rejection_reason(candidate, required_skills=required, radius_km=radius_km, daily_limit=daily_limit):
            continue

        distance = _finite_number(candidate["distance_km"])
        allowed_radius = _allowed_radius(candidate, radius_km)
        today_count = _integer_number(candidate.get("today_count", 0))
        limit = _daily_limit(candidate, daily_limit)
        rating = _finite_number(candidate.get("rating"))
        rating_used = _clamp(default_rating if rating is None else rating, 0.0, RATING_FULL_SCORE)
        factors_precision = {
            "skill": 1.0,
            "distance": distance_factor(distance, allowed_radius),
            "load": load_factor(today_count, limit),
            "rating": rating_factor(rating_used),
        }
        contributions = {name: factors_precision[name] * parsed_weights[index] for index, name in enumerate(FACTOR_NAMES)}
        base_score = sum(contributions.values())
        is_focus = bool(candidate.get("is_focus"))
        penalty = FOCUS_PENALTY if is_focus else 1.0
        score = base_score * penalty

        scored.append(
            {
                "staff_id": candidate.get("staff_id"),
                "name": candidate.get("name") or "",
                "distance_km": round(distance, SCORE_DIGITS),
                "distance_km_precision": distance,
                "score": round(score, SCORE_DIGITS),
                "score_precision": score,
                "base_score": base_score,
                "factors": {name: round(value, SCORE_DIGITS) for name, value in factors_precision.items()},
                "factors_precision": factors_precision,
                "weights": [round(weight, SCORE_DIGITS) for weight in parsed_weights],
                "weights_precision": list(parsed_weights),
                "contributions": contributions,
                "penalty": penalty,
                "allowed_radius_km": allowed_radius,
                "daily_limit": limit,
                "rating_used": rating_used,
                "rating_is_default": rating is None,
                "reason": _build_reason(
                    required=required,
                    distance_km=distance,
                    today_count=today_count,
                    rating=None if rating is None else rating_used,
                    default_rating=default_rating,
                    is_focus=is_focus,
                ),
                "today_count": today_count,
                "rating": None if rating is None else round(rating, SCORE_DIGITS),
                "is_focus": is_focus,
            }
        )

    scored.sort(key=lambda row: (-row["score_precision"], row["distance_km_precision"], row["today_count"], row["staff_id"] or 0))
    for rank, row in enumerate(scored, 1):
        row["rank"] = rank
    if top_n is not None and top_n >= 0:
        return scored[: int(top_n)]
    return scored
