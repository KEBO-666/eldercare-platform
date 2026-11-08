"""分级预警规则纯函数（阈值 / 连续异常 / 静默超时），不依赖数据库。

语义约定：
- ``match_threshold``：单次读数越界。支持 ``>= <= > <`` 四种算子；
  若给出 ``threshold2``，则把两个阈值视为闭区间 ``[low, high]`` 的上下限，
  读数需落在区间内（用于「血压区间」一类规则）。
- ``is_consecutive``：``values`` 按时间正序传入，只有**最近 window_size 次**
  读数全部满足算子才算命中（不足 window_size 次一律不命中）。
- ``is_silent``：距最近一次上报时长达到 ``hours`` 小时即静默；从未上报视为静默。
- ``rule_matches``：按 ``rule_type`` 分派，``recent_values`` **不含**当前读数，
  由本函数把当前 ``value`` 追加到末尾后再做连续判定。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.config import settings

OPERATORS: tuple[str, ...] = (">=", "<=", ">", "<")

_ALIASES = {"≥": ">=", "≤": "<=", "=>": ">=", "=<": "<="}


def _get(rule: Any, name: str, default: Any = None) -> Any:
    """同时兼容 ORM 规则对象与 dict。"""
    if isinstance(rule, dict):
        return rule.get(name, default)
    return getattr(rule, name, default)


def normalize_operator(operator: Any) -> str | None:
    text = str(operator) if operator is not None else ""
    text = text.strip()
    if not text:
        return None
    return _ALIASES.get(text, text)


def _compare(value: float, operator: Any, threshold: float) -> bool:
    op = normalize_operator(operator)
    if op == ">=":
        return value >= threshold
    if op == "<=":
        return value <= threshold
    if op == ">":
        return value > threshold
    if op == "<":
        return value < threshold
    return False


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def match_threshold(value: Any, operator: Any, threshold: Any, threshold2: Any = None) -> bool:
    """单值阈值判定；算子非法或参数缺失时返回 False（不抛异常，便于后台扫描）。"""
    number = _to_float(value)
    base = _to_float(threshold)
    if number is None or base is None:
        return False
    if not _compare(number, operator, base):
        return False
    other = _to_float(threshold2)
    if threshold2 is None or other is None:
        return True
    low, high = (base, other) if base <= other else (other, base)
    return low <= number <= high


def is_consecutive(values: Any, operator: Any, threshold: Any, window_size: Any) -> bool:
    """最近 window_size 次读数是否全部满足算子。"""
    if not values or window_size is None or threshold is None:
        return False
    try:
        window = int(window_size)
    except (TypeError, ValueError):
        return False
    if window <= 0:
        return False
    numbers: list[float] = []
    for item in values:
        number = _to_float(item)
        if number is None:
            return False
        numbers.append(number)
    if len(numbers) < window:
        return False
    limit = _to_float(threshold)
    if limit is None:
        return False
    return all(_compare(number, operator, limit) for number in numbers[-window:])


def is_silent(last_metric_at: Any, now: datetime, hours: Any = None) -> bool:
    """距最近一次上报是否已达静默时长；last_metric_at 为 None 表示从未上报。"""
    if last_metric_at is None:
        return True
    if now is None:
        now = datetime.now()
    limit_hours = settings.alert_silence_hours if hours is None else _to_float(hours)
    if limit_hours is None or limit_hours <= 0:
        return False
    try:
        elapsed = (now - last_metric_at).total_seconds()
    except TypeError:  # 时区感知与 naive 混用
        elapsed = (now.replace(tzinfo=None) - last_metric_at.replace(tzinfo=None)).total_seconds()
    return elapsed >= limit_hours * 3600


def rule_matches(
    rule: Any,
    value: Any,
    recent_values: Any = None,
    last_metric_at: Any = None,
    now: datetime | None = None,
) -> bool:
    """按规则类型分派匹配；recent_values 为时间正序的历史读数（不含当前值）。"""
    rule_type = str(_get(rule, "rule_type") or "threshold")
    if rule_type == "silence":
        return is_silent(last_metric_at, now, _get(rule, "silence_hours"))
    if rule_type == "consecutive":
        history = [v for v in (recent_values or []) if v is not None]
        if value is not None:
            history.append(value)
        return is_consecutive(
            history, _get(rule, "operator"), _get(rule, "threshold"), _get(rule, "window_size")
        )
    if rule_type == "threshold":
        return match_threshold(
            value, _get(rule, "operator"), _get(rule, "threshold"), _get(rule, "threshold2")
        )
    return False
