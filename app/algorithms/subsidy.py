"""补贴政策匹配与三方分账（纯函数 + 政策种子）。

分账口径（与 docs/design.md §5.3 一致）：
    政府补贴 = min(金额 × 补贴比例, 月度上限 - 本月已用补贴)
    长护险   = 金额 × 长护险比例
    个人自付 = 金额 - 政府补贴 - 长护险
金额一律 Decimal 四舍五入到分，且自付不为负。

`pick_policy` 只做「精确优先、逐级退化」的匹配，不访问数据库，便于单测；
`ensure_default_policies` 是幂等的政策矩阵种子（6 条）。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable

from app.core.enums import AbilityLevel, DifficultyType

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")

# 能力等级高低（退化匹配时优先取更低等级，避免"轻度失能按重度补贴"）
ABILITY_RANK: dict[str, int] = {
    AbilityLevel.INTACT.value: 0,
    AbilityLevel.MILD.value: 1,
    AbilityLevel.MODERATE.value: 2,
    AbilityLevel.SEVERE.value: 3,
}

# (ability_level, difficulty_type, subsidy_ratio, monthly_limit, ltc_ratio, note)
DEFAULT_POLICY_MATRIX: tuple[tuple[str, str, float, str, float, str], ...] = (
    (AbilityLevel.MODERATE.value, DifficultyType.NORMAL.value, 0.5, "300.00", 0.2, "中度失能 · 普通"),
    (AbilityLevel.MODERATE.value, DifficultyType.LOW_INCOME.value, 0.5, "300.00", 0.2, "中度失能 · 低保"),
    (AbilityLevel.MODERATE.value, DifficultyType.EXTREME.value, 0.5, "300.00", 0.2, "中度失能 · 特困"),
    (AbilityLevel.SEVERE.value, DifficultyType.NORMAL.value, 0.7, "500.00", 0.3, "重度失能 · 普通"),
    (AbilityLevel.SEVERE.value, DifficultyType.LOW_INCOME.value, 0.7, "500.00", 0.3, "重度失能 · 低保"),
    (AbilityLevel.SEVERE.value, DifficultyType.EXTREME.value, 0.7, "500.00", 0.3, "重度失能 · 特困"),
)


# ---------------------------------------------------------------- 通用工具
def money(value: Any) -> Decimal:
    """转换为「分」精度的 Decimal；非法输入按 0 处理。"""
    if value is None:
        return ZERO
    try:
        raw = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO
    if not raw.is_finite():
        return ZERO
    return raw.quantize(CENTS, rounding=ROUND_HALF_UP)


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _normalize(value: Any) -> str | None:
    if value is None:
        return None
    text = str(getattr(value, "value", value)).strip().lower()
    return text or None


def _ratio(policy: Any, name: str) -> Decimal:
    """比例值（0~1），不做「分」量化，避免 0.125 被舍成 0.13。"""
    raw = _attr(policy, name, 0) or 0
    try:
        value = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")
    return value if value.is_finite() else Decimal("0")


# ---------------------------------------------------------------- 政策匹配
def pick_policy(
    policies: Iterable[Any],
    ability_level: Any,
    difficulty_type: Any,
) -> Any | None:
    """按 (能力等级, 困难类型) 匹配补贴政策；无精确项时逐级退化。

    匹配优先级：
        1. 精确 (ability, difficulty)
        2. (ability, normal)               —— 困难类型退化为普通
        3. 任意 ability + 原 difficulty    —— 能力等级退化
        4. 任意 ability + normal           —— 双重退化
    同一优先级内按能力等级从低到高取第一条，保证结果稳定可测。
    """
    ability = _normalize(ability_level)
    difficulty = _normalize(difficulty_type) or DifficultyType.NORMAL.value

    candidates: list[Any] = []
    for policy in policies or []:
        if _attr(policy, "enabled", True) is False:
            continue
        candidates.append(policy)
    if not candidates:
        return None

    tiers: list[tuple[str | None, str]] = [(ability, difficulty)]
    if difficulty != DifficultyType.NORMAL.value:
        tiers.append((ability, DifficultyType.NORMAL.value))
    tiers.append((None, difficulty))
    if difficulty != DifficultyType.NORMAL.value:
        tiers.append((None, DifficultyType.NORMAL.value))

    for want_ability, want_difficulty in tiers:
        matched: list[Any] = []
        for policy in candidates:
            p_ability = _normalize(_attr(policy, "ability_level"))
            p_difficulty = _normalize(_attr(policy, "difficulty_type"))
            if p_difficulty != want_difficulty:
                continue
            if want_ability is not None and p_ability != want_ability:
                continue
            matched.append(policy)
        if matched:
            matched.sort(key=lambda p: ABILITY_RANK.get(_normalize(_attr(p, "ability_level")) or "", 99))
            return matched[0]
    return None


# ---------------------------------------------------------------- 三方分账
def split_amount(
    amount: Any,
    policy: Any | None,
    used_subsidy: Any = ZERO,
) -> dict[str, Decimal]:
    """把一笔服务金额拆成 {subsidy, ltc, self_pay}，三者之和恒等于金额。

    - `used_subsidy` 为该老人本月**已用**政府补贴（跨账单累计）；
    - 政府补贴受 `monthly_limit` 截断；
    - 未匹配到政策时 subsidy=ltc=0，全部自付。
    """
    total = money(amount)
    if total < 0:  # 红冲场景：负金额同样按比例拆分
        sign, total_abs = Decimal("-1"), -total
    else:
        sign, total_abs = Decimal("1"), total

    if policy is None:
        return {"subsidy": ZERO, "ltc": ZERO, "self_pay": money(total)}

    ratio = _ratio(policy, "subsidy_ratio")
    ltc_ratio = _ratio(policy, "ltc_ratio")
    limit = money(_attr(policy, "monthly_limit", 0))
    used = money(used_subsidy)

    remaining = limit - used
    if remaining < 0:
        remaining = ZERO

    subsidy = money(total_abs * ratio)
    if subsidy > remaining:
        subsidy = remaining
    if subsidy < 0:
        subsidy = ZERO

    ltc = money(total_abs * ltc_ratio)

    # 自付兜底：极端政策（比例之和 > 1）时不为负
    self_pay = money(total_abs - subsidy - ltc)
    if self_pay < 0:
        self_pay = ZERO

    return {
        "subsidy": money(subsidy * sign),
        "ltc": money(ltc * sign),
        "self_pay": money(self_pay * sign),
    }


__all__ = [
    "ABILITY_RANK",
    "CENTS",
    "DEFAULT_POLICY_MATRIX",
    "money",
    "pick_policy",
    "split_amount",
]
