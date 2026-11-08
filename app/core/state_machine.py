"""状态机：集中定义全部单据的合法流转（评分点：状态不可跳跃）。

用法：
    from app.core.state_machine import APPOINTMENT, OrderStatus
    next_status = APPOINTMENT.next(AppointmentStatus.PENDING_ACCEPT, "accept")
    APPOINTMENT.assert_can(current, action)   # 非法则抛 Conflict
"""
from __future__ import annotations

from typing import Iterable

from app.core.exceptions import Conflict


class StateMachine:
    """由 {from_state: {action: to_state}} 驱动的通用状态机。"""

    def __init__(self, name: str, transitions: dict[str, dict[str, str]]) -> None:
        self.name = name
        self.transitions = transitions

    def can(self, from_state: str, action: str) -> bool:
        return action in self.transitions.get(str(from_state), {})

    def next(self, from_state: str, action: str) -> str:
        target = self.transitions.get(str(from_state), {}).get(action)
        if target is None:
            raise Conflict(
                f"{self.name}不允许从「{from_state}」执行动作「{action}」",
                code="INVALID_TRANSITION",
            )
        return target

    def actions(self, from_state: str) -> list[str]:
        return sorted(self.transitions.get(str(from_state), {}).keys())

    def targets(self, from_state: str) -> list[str]:
        return sorted(set(self.transitions.get(str(from_state), {}).values()))

    def is_terminal(self, state: str) -> bool:
        return not self.transitions.get(str(state))


# ---------------- 预约单 ----------------
APPOINTMENT = StateMachine("预约单", {
    "pending_accept": {"accept": "accepted", "cancel": "cancelled", "to_manual": "cancelled"},
    "accepted": {"to_dispatch": "pending_dispatch", "cancel": "cancelled"},
    "pending_dispatch": {"dispatch": "dispatched", "cancel": "cancelled"},
    "dispatched": {"start": "in_service", "cancel": "cancelled", "no_show": "no_show"},
    "in_service": {"complete": "completed"},
    "completed": {},
    "cancelled": {},
    "no_show": {},
})

# ---------------- 工单 ----------------
ORDER = StateMachine("工单", {
    "pending_accept": {"accept": "accepted", "cancel": "cancelled", "reject": "pending_accept"},
    "accepted": {"checkin": "in_service", "cancel": "cancelled", "report_anomaly": "anomaly"},
    "in_service": {"checkout": "pending_evaluation", "report_anomaly": "anomaly"},
    "pending_evaluation": {"complete": "completed", "report_anomaly": "anomaly"},
    "anomaly": {"resolve": "completed", "cancel": "cancelled"},
    "completed": {},
    "cancelled": {},
})

# ---------------- 预警 ----------------
ALERT = StateMachine("预警", {
    "pending": {"resolve": "resolved", "false_alarm": "false_alarm", "refer": "referred"},
    "resolved": {},
    "false_alarm": {},
    "referred": {},
})

# ---------------- 投诉 ----------------
COMPLAINT = StateMachine("投诉单", {
    "new": {"assign": "assigned", "archive": "archived"},
    "assigned": {"review": "reviewing"},
    "reviewing": {"conclude": "concluded"},
    "concluded": {"archive": "archived"},
    "archived": {},
})

# ---------------- 账单 ----------------
BILL = StateMachine("账单", {
    "draft": {"submit": "pending_review"},
    "pending_review": {"confirm": "confirmed", "reject": "draft"},
    "confirmed": {"pay": "paid", "reverse": "reversed"},
    "paid": {"reverse": "reversed"},
    "reversed": {},
})


MACHINES = {m.name: m for m in (APPOINTMENT, ORDER, ALERT, COMPLAINT, BILL)}


def validate_transitions(machine: StateMachine, known_states: Iterable[str]) -> list[str]:
    """自检：迁移表引用的状态必须都在已知枚举内，返回问题清单。"""
    known = {str(s) for s in known_states}
    problems: list[str] = []
    for src, actions in machine.transitions.items():
        if src not in known:
            problems.append(f"{machine.name}: 未知起始状态 {src}")
        for act, dst in actions.items():
            if dst not in known:
                problems.append(f"{machine.name}: {src} -{act}-> 未知目标状态 {dst}")
    return problems
