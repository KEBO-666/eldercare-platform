"""D2 分级预警服务：默认规则、指标评估、推送留痕、静默检测与超时升级。

对外契约（见 docs/CONTRACTS.md 6.5）：
- ``ensure_default_rules(db) -> int``  幂等写入默认规则，返回本次新建数量；
- ``evaluate_metric(db, metric, operator_id=None) -> list[Alert]``  指标入库后调用；
- ``scan_alerts(db, now=None) -> dict`` 静默检测 + 超时升级。

处置口径：
- 预警按 level（red > yellow > blue）+ 创建时间倒序；
- 同老人同规则在 cooldown_minutes 内只生成一条（冷却去重）；
- 红色预警同时写 ``data/logs/sms_sim.log`` 模拟短信网关留痕。
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.algorithms.alert_rules import is_silent, rule_matches
from app.core.audit import write_audit
from app.core.config import settings
from app.core.deps import apply_scope, visible_elder_ids
from app.core.enums import (
    ALERT_LEVEL_NAMES,
    METRIC_NAMES,
    AlertLevel,
    AlertRuleType,
    AlertStatus,
    RoleCode,
)
from app.core.exceptions import BadRequest, Conflict, NotFound, RuleViolation
from app.core.state_machine import ALERT
from app.db.base import utcnow
from app.models import (
    Alert,
    AlertAction,
    AlertRule,
    Elder,
    HealthMetric,
    Notification,
    Org,
    Role,
    User,
)

logger = logging.getLogger("eldercare.alerts")

SILENCE_CODE = "SILENCE"
RULE_TYPES: tuple[str, ...] = (
    AlertRuleType.THRESHOLD.value,
    AlertRuleType.CONSECUTIVE.value,
    AlertRuleType.SILENCE.value,
)
ALERT_LEVELS: tuple[str, ...] = (AlertLevel.RED.value, AlertLevel.YELLOW.value, AlertLevel.BLUE.value)
ALERT_STATUSES: tuple[str, ...] = (
    AlertStatus.PENDING.value,
    AlertStatus.RESOLVED.value,
    AlertStatus.FALSE_ALARM.value,
    AlertStatus.REFERRED.value,
)
RESOLUTION_ACTIONS: dict[str, str] = {
    AlertStatus.RESOLVED.value: "resolve",
    AlertStatus.FALSE_ALARM.value: "false_alarm",
    AlertStatus.REFERRED.value: "refer",
}
RULE_FIELDS: tuple[str, ...] = (
    "code",
    "name",
    "rule_type",
    "metric_code",
    "operator",
    "threshold",
    "threshold2",
    "window_size",
    "silence_hours",
    "level",
    "cooldown_minutes",
    "escalate_minutes",
    "push_minutes",
    "enabled",
    "description",
)
METRIC_UNITS: dict[str, str] = {
    "sbp": "mmHg",
    "dbp": "mmHg",
    "hr": "次/分",
    "spo2": "%",
    "steps": "步",
    "motion": "分",
}

DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "code": "SBP_HIGH",
        "name": "收缩压过高",
        "rule_type": AlertRuleType.THRESHOLD.value,
        "metric_code": "sbp",
        "operator": ">=",
        "threshold": 180.0,
        "level": AlertLevel.RED.value,
        "cooldown_minutes": 30,
        "escalate_minutes": 30,
        "push_minutes": settings.alert_red_push_minutes,
        "description": "收缩压 ≥180mmHg，红色预警（立即推送并 30 分钟升级）",
    },
    {
        "code": "SBP_LOW",
        "name": "收缩压过低",
        "rule_type": AlertRuleType.THRESHOLD.value,
        "metric_code": "sbp",
        "operator": "<=",
        "threshold": 90.0,
        "level": AlertLevel.RED.value,
        "cooldown_minutes": 30,
        "escalate_minutes": 30,
        "push_minutes": settings.alert_red_push_minutes,
        "description": "收缩压 ≤90mmHg，红色预警",
    },
    {
        "code": "SPO2_LOW",
        "name": "血氧过低",
        "rule_type": AlertRuleType.THRESHOLD.value,
        "metric_code": "spo2",
        "operator": "<",
        "threshold": 90.0,
        "level": AlertLevel.RED.value,
        "cooldown_minutes": 30,
        "escalate_minutes": 30,
        "push_minutes": settings.alert_red_push_minutes,
        "description": "血氧饱和度 <90%，红色预警",
    },
    {
        "code": "HR_CONSEC",
        "name": "心率连续过高",
        "rule_type": AlertRuleType.CONSECUTIVE.value,
        "metric_code": "hr",
        "operator": ">",
        "threshold": 100.0,
        "window_size": 3,
        "level": AlertLevel.YELLOW.value,
        "cooldown_minutes": 30,
        "escalate_minutes": 30,
        "push_minutes": 5,
        "description": "心率连续 3 次 >100 次/分，黄色预警",
    },
    {
        "code": SILENCE_CODE,
        "name": "长时间未上报健康数据",
        "rule_type": AlertRuleType.SILENCE.value,
        "silence_hours": settings.alert_silence_hours,
        "level": AlertLevel.BLUE.value,
        "cooldown_minutes": 30,
        "escalate_minutes": 30,
        "push_minutes": 5,
        "description": "48 小时无健康数据上报，蓝色预警（含从未上报）",
    },
]


# ---------------------------------------------------------------- 通用小工具
def sms_log_path() -> Path:
    """红色预警短信模拟日志路径（测试可 monkeypatch）。"""
    return settings.abs_path(settings.data_dir) / "logs" / "sms_sim.log"


def level_name(level: Any) -> str:
    name = ALERT_LEVEL_NAMES.get(level)
    if name:
        return name
    try:
        return ALERT_LEVEL_NAMES[AlertLevel(str(level))]
    except (ValueError, KeyError):
        return str(level)


def metric_name(metric_code: Any) -> str:
    code = str(metric_code) if metric_code is not None else ""
    name = METRIC_NAMES.get(code)
    if name:
        return name
    try:
        return METRIC_NAMES[code]
    except KeyError:
        return code or "指标"


def _format_number(value: Any) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:g}"


def _window_brief(rule: AlertRule, value: Any) -> str:
    metric = metric_name(rule.metric_code)
    unit = METRIC_UNITS.get(str(rule.metric_code or ""), "")
    if str(rule.rule_type) == AlertRuleType.CONSECUTIVE.value:
        return (
            f"连续{rule.window_size or 0}次{metric}"
            f"{rule.operator or ''}{_format_number(rule.threshold)}{unit}（最近 {_format_number(value)}{unit}）"
        )
    return f"{metric} {_format_number(value)}{unit}（阈值 {rule.operator or ''}{_format_number(rule.threshold)}{unit}）"


def _page_bounds(page: int, size: int) -> tuple[int, int]:
    page = max(1, int(page or 1))
    size = max(1, min(int(size or 20), 200))
    return (page - 1) * size, size


# ---------------------------------------------------------------- 默认规则
def ensure_default_rules(db: Session) -> int:
    """按 code 幂等写入默认规则，返回本次新建数量。"""
    existing = set(db.execute(select(AlertRule.code)).scalars().all())
    created = 0
    for spec in DEFAULT_RULES:
        if spec["code"] in existing:
            continue
        db.add(AlertRule(**spec))
        existing.add(spec["code"])
        created += 1
    if created:
        db.commit()
    return created


def list_rules(db: Session) -> list[dict[str, Any]]:
    ensure_default_rules(db)
    rules = db.execute(select(AlertRule).order_by(AlertRule.id.asc())).scalars().all()
    return [serialize_rule(rule) for rule in rules]


def serialize_rule(rule: AlertRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "code": rule.code,
        "name": rule.name,
        "rule_type": str(rule.rule_type),
        "metric_code": rule.metric_code,
        "operator": rule.operator,
        "threshold": rule.threshold,
        "threshold2": rule.threshold2,
        "window_size": rule.window_size,
        "silence_hours": rule.silence_hours,
        "level": str(rule.level),
        "cooldown_minutes": rule.cooldown_minutes,
        "escalate_minutes": rule.escalate_minutes,
        "push_minutes": rule.push_minutes,
        "enabled": bool(rule.enabled),
        "description": rule.description,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
    }


def _validate_rule(data: dict[str, Any]) -> dict[str, Any]:
    """校验（并补全）规则字段，返回规范化后的字典。"""
    normalized = dict(data)
    rule_type = str(normalized.get("rule_type") or AlertRuleType.THRESHOLD.value)
    if rule_type not in RULE_TYPES:
        raise RuleViolation("rule_type 只能是 threshold / consecutive / silence")
    normalized["rule_type"] = rule_type

    level = str(normalized.get("level") or AlertLevel.YELLOW.value)
    if level not in ALERT_LEVELS:
        raise RuleViolation("level 只能是 red / yellow / blue")
    normalized["level"] = level

    operator = normalized.get("operator")
    if operator is not None:
        operator = str(operator).strip()
        if operator and operator not in (">=", "<=", ">", "<"):
            raise RuleViolation("operator 仅支持 >= / <= / > / <")
        normalized["operator"] = operator or None

    if rule_type in (AlertRuleType.THRESHOLD.value, AlertRuleType.CONSECUTIVE.value):
        if not normalized.get("metric_code"):
            raise RuleViolation("阈值/连续规则必须指定 metric_code")
        if normalized.get("operator") is None or normalized.get("threshold") is None:
            raise RuleViolation("阈值/连续规则必须指定 operator 与 threshold")
    if rule_type == AlertRuleType.CONSECUTIVE.value:
        window = normalized.get("window_size")
        if window is None or int(window) < 2:
            raise RuleViolation("连续规则的 window_size 至少为 2")
    if rule_type == AlertRuleType.SILENCE.value:
        if not normalized.get("silence_hours"):
            normalized["silence_hours"] = settings.alert_silence_hours

    for key in ("cooldown_minutes", "escalate_minutes", "push_minutes"):
        if normalized.get(key) is not None and int(normalized[key]) < 0:
            raise RuleViolation(f"{key} 不能为负数")
    return normalized


def create_rule(
    db: Session, user: User, payload: dict[str, Any], request: Request | None = None
) -> dict[str, Any]:
    data = dict(payload or {})
    code = str(data.get("code") or "").strip()
    if not code:
        raise RuleViolation("规则编码 code 不能为空")
    if db.execute(select(AlertRule.id).where(AlertRule.code == code).limit(1)).scalar_one_or_none():
        raise Conflict(f"规则编码已存在：{code}")
    data["code"] = code[:32]
    data["name"] = str(data.get("name") or code)[:64]
    data = _validate_rule(data)
    rule = AlertRule(**{key: data[key] for key in RULE_FIELDS if key in data})
    db.add(rule)
    db.flush()
    write_audit(
        db,
        user=user,
        action="create",
        entity_type="alert_rule",
        entity_id=rule.id,
        after=serialize_rule(rule),
        detail=f"新建预警规则 {rule.code}",
        request=request,
    )
    db.commit()
    db.refresh(rule)
    return serialize_rule(rule)


def update_rule(
    db: Session,
    user: User,
    rule_id: int,
    payload: dict[str, Any],
    request: Request | None = None,
) -> dict[str, Any]:
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise NotFound("预警规则不存在")
    changes = {key: value for key, value in dict(payload or {}).items() if key in RULE_FIELDS}
    if not changes:
        return serialize_rule(rule)

    before = serialize_rule(rule)
    merged = {field: getattr(rule, field) for field in RULE_FIELDS}
    merged.update(changes)
    merged["name"] = str(merged.get("name") or rule.code)[:64]
    merged["code"] = str(merged.get("code") or rule.code)[:32]
    if merged["code"] != rule.code:
        duplicated = db.execute(
            select(AlertRule.id).where(AlertRule.code == merged["code"], AlertRule.id != rule.id).limit(1)
        ).scalar_one_or_none()
        if duplicated:
            raise Conflict(f"规则编码已存在：{merged['code']}")
    merged = _validate_rule(merged)

    for field in changes:
        value = merged.get(field)
        if field in ("operator", "metric_code") and value == "":
            value = None
        setattr(rule, field, value)
    db.flush()
    write_audit(
        db,
        user=user,
        action="update",
        entity_type="alert_rule",
        entity_id=rule.id,
        before=before,
        after=serialize_rule(rule),
        detail=f"修改预警规则 {rule.code}",
        request=request,
    )
    db.commit()
    db.refresh(rule)
    return serialize_rule(rule)


# ---------------------------------------------------------------- 预警序列化
def serialize_alert(
    db: Session, alert: Alert, *, include_actions: bool = False, now: datetime | None = None
) -> dict[str, Any]:
    now = now or utcnow()
    elder = db.get(Elder, alert.elder_id) if alert.elder_id else None
    rule = db.get(AlertRule, alert.rule_id) if alert.rule_id else None
    pending = str(alert.status) == AlertStatus.PENDING.value
    overdue = bool(pending and alert.deadline_at is not None and alert.deadline_at < now)
    data: dict[str, Any] = {
        "id": alert.id,
        "elder_id": alert.elder_id,
        "elder_name": elder.name if elder is not None else None,
        "elder_code": elder.code if elder is not None else None,
        "rule_id": alert.rule_id,
        "rule_code": rule.code if rule is not None else None,
        "rule_name": rule.name if rule is not None else None,
        "work_order_id": alert.work_order_id,
        "level": str(alert.level),
        "level_name": level_name(alert.level),
        "status": str(alert.status),
        "metric_code": alert.metric_code,
        "metric_name": metric_name(alert.metric_code) if alert.metric_code else None,
        "metric_value": alert.metric_value,
        "summary": alert.summary,
        "detail": alert.detail,
        "pushed_at": alert.pushed_at,
        "deadline_at": alert.deadline_at,
        "escalated_at": alert.escalated_at,
        "escalated_to_org_id": alert.escalated_to_org_id,
        "resolved_at": alert.resolved_at,
        "resolved_by": alert.resolved_by,
        "resolution_type": alert.resolution_type,
        "resolution_note": alert.resolution_note,
        "measure": alert.measure,
        "response_seconds": alert.response_seconds,
        "handling_seconds": alert.handling_seconds,
        "created_at": alert.created_at,
        "overdue": overdue,
    }
    if include_actions:
        actions = db.execute(
            select(AlertAction)
            .where(AlertAction.alert_id == alert.id)
            .order_by(AlertAction.action_at.asc(), AlertAction.id.asc())
        ).scalars().all()
        data["actions"] = [serialize_action(action) for action in actions]
    return data


def serialize_action(action: AlertAction) -> dict[str, Any]:
    return {
        "id": action.id,
        "alert_id": action.alert_id,
        "action_type": str(action.action_type),
        "channel": action.channel,
        "target": action.target,
        "operator_id": action.operator_id,
        "note": action.note,
        "action_at": action.action_at,
        "created_at": action.created_at,
    }


# ---------------------------------------------------------------- 数据范围
def _scoped_stmt(db: Session, user: User, stmt):
    """先按通用数据范围过滤，再收敛到可见老人（Alert 表无组织字段）。"""
    stmt = apply_scope(stmt, Alert, user, db)
    elder_ids = visible_elder_ids(db, user)
    if elder_ids is not None:
        stmt = stmt.where(Alert.elder_id.in_(elder_ids))
    return stmt


def get_alert(db: Session, user: User, alert_id: int, *, include_actions: bool = True) -> dict[str, Any]:
    stmt = _scoped_stmt(db, user, select(Alert).where(Alert.id == alert_id))
    alert = db.execute(stmt).scalar_one_or_none()
    if alert is None:
        raise NotFound("预警不存在或无权访问")
    return serialize_alert(db, alert, include_actions=include_actions)


# ---------------------------------------------------------------- 通知 / 短信留痕
def _grid_user_ids(db: Session, elder: Elder | None) -> list[int]:
    if elder is None or not elder.grid_org_id:
        return []
    stmt = (
        select(User.id)
        .join(Role, User.role_id == Role.id)
        .where(
            Role.code == RoleCode.GRID.value,
            User.org_id == elder.grid_org_id,
            User.is_active.is_(True),
        )
        .order_by(User.id.asc())
    )
    return [int(uid) for uid in db.execute(stmt).scalars().all()]


def _street_user_ids(db: Session, street_id: int | None) -> list[int]:
    stmt = (
        select(User.id)
        .join(Role, User.role_id == Role.id)
        .where(Role.code == RoleCode.STREET.value, User.is_active.is_(True))
        .order_by(User.id.asc())
    )
    if street_id:
        stmt = stmt.where(User.org_id == street_id)
    return [int(uid) for uid in db.execute(stmt).scalars().all()]


def _notify_alert(db: Session, elder: Elder, alert: Alert, now: datetime) -> int:
    """推送站内信：同网格网格员 + 家属 + 签约医生（短信通道）。"""
    title = f"【{level_name(alert.level)}】{alert.summary or '健康异常'}"[:128]
    content = (
        f"{now:%Y-%m-%d %H:%M} {elder.name}（{elder.code}）触发"
        f"{level_name(alert.level)}：{alert.summary or ''}，请及时处置。"
    )
    targets = _grid_user_ids(db, elder)
    if elder.user_id:
        targets.append(int(elder.user_id))
    sent = 0
    for uid in sorted({uid for uid in targets if uid}):
        db.add(
            Notification(
                user_id=uid,
                elder_id=elder.id,
                channel="inapp",
                title=title,
                content=content,
                status="sent",
                related_type="alert",
                related_id=alert.id,
            )
        )
        sent += 1

    doctor_title = f"【签约医生】{level_name(alert.level)}：{elder.name} {alert.metric_code or ''}".strip()
    db.add(
        Notification(
            user_id=None,
            elder_id=elder.id,
            channel="sms",
            title=doctor_title[:128],
            content=content,
            status="sent",
            related_type="alert",
            related_id=alert.id,
        )
    )
    return sent + 1


def _write_sms_log(alert: Alert, elder: Elder, now: datetime) -> None:
    """红色预警写模拟短信网关日志，失败不影响主流程。"""
    try:
        path = sms_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (
            f"{now:%Y-%m-%d %H:%M:%S} | SMS | alert_id={alert.id} | level={str(alert.level)}"
            f" | elder={elder.code}/{elder.name} | {alert.summary or ''}\n"
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:  # pragma: no cover - 磁盘异常不影响预警
        logger.warning("短信模拟日志写入失败 alert_id=%s", getattr(alert, "id", None), exc_info=True)


# ---------------------------------------------------------------- 生成预警
def _in_cooldown(db: Session, elder_id: int, rule: AlertRule, now: datetime) -> bool:
    minutes = rule.cooldown_minutes if rule.cooldown_minutes is not None else settings.alert_cooldown_minutes
    if minutes is None or int(minutes) <= 0:
        return False
    since = now - timedelta(minutes=int(minutes))
    exists = db.execute(
        select(Alert.id)
        .where(Alert.elder_id == elder_id, Alert.rule_id == rule.id, Alert.created_at >= since)
        .limit(1)
    ).scalar_one_or_none()
    return exists is not None


def _create_alert(
    db: Session,
    *,
    elder: Elder,
    rule: AlertRule | None,
    level: str,
    summary: str,
    now: datetime,
    metric: HealthMetric | None = None,
    metric_value: float | None = None,
    detail: dict[str, Any] | None = None,
    operator_id: int | None = None,
) -> Alert:
    escalate = rule.escalate_minutes if rule is not None and rule.escalate_minutes is not None else settings.alert_escalate_minutes
    level_value = str(level)
    value = metric_value
    if value is None and metric is not None and metric.value is not None:
        value = float(metric.value)
    alert = Alert(
        elder_id=elder.id,
        rule_id=rule.id if rule is not None else None,
        level=level_value,
        status=AlertStatus.PENDING.value,
        metric_code=(metric.metric_code if metric is not None else (rule.metric_code if rule else None)),
        metric_value=value,
        summary=(summary or "")[:255],
        detail=detail,
        created_from_value=value,
        pushed_at=now,
        deadline_at=now + timedelta(minutes=int(escalate)),
    )
    db.add(alert)
    db.flush()

    db.add(
        AlertAction(
            alert_id=alert.id,
            action_type="push",
            channel="inapp",
            target=f"grid:{elder.grid_org_id or '-'}",
            operator_id=operator_id,
            note=f"{level_name(level_value)}推送（网格员/家属/签约医生）",
            action_at=now,
        )
    )
    _notify_alert(db, elder, alert, now)
    if level_value == AlertLevel.RED.value:
        _write_sms_log(alert, elder, now)
    db.flush()
    return alert


def _metric_history(db: Session, metric: HealthMetric, limit: int) -> list[float]:
    """取当前指标之前的历史读数（时间正序，不含当前记录）。"""
    stmt = select(HealthMetric.value).where(
        HealthMetric.elder_id == metric.elder_id,
        HealthMetric.metric_code == metric.metric_code,
    )
    if metric.id is not None:
        stmt = stmt.where(HealthMetric.id != metric.id)
    if metric.measured_at is not None:
        stmt = stmt.where(HealthMetric.measured_at <= metric.measured_at)
    rows = db.execute(
        stmt.order_by(HealthMetric.measured_at.desc(), HealthMetric.id.desc()).limit(max(1, limit))
    ).scalars().all()
    return [float(value) for value in reversed(rows)]


def evaluate_metric(
    db: Session, metric: HealthMetric, operator_id: int | None = None
) -> list[Alert]:
    """评估一条健康指标，返回本次新生成的预警（同老人同规则冷却期内不重复）。"""
    if metric is None:
        raise BadRequest("健康指标不能为空")
    now = utcnow()
    ensure_default_rules(db)
    elder = db.get(Elder, metric.elder_id)
    if elder is None or elder.is_deleted:
        raise NotFound("老人档案不存在")

    rules = db.execute(
        select(AlertRule)
        .where(
            AlertRule.enabled.is_(True),
            AlertRule.metric_code == metric.metric_code,
            AlertRule.rule_type.in_([AlertRuleType.THRESHOLD.value, AlertRuleType.CONSECUTIVE.value]),
        )
        .order_by(AlertRule.id.asc())
    ).scalars().all()
    if not rules:
        return []

    max_window = max(
        [int(rule.window_size or 1) for rule in rules if str(rule.rule_type) == AlertRuleType.CONSECUTIVE.value]
        or [1]
    )
    history = _metric_history(db, metric, max_window)
    value = float(metric.value) if metric.value is not None else None

    alerts: list[Alert] = []
    for rule in rules:
        if str(rule.rule_type) == AlertRuleType.CONSECUTIVE.value:
            need = max(1, int(rule.window_size or 1))
            recent = history[-need + 1:] if need > 1 else []
        else:
            recent = []
        if not rule_matches(rule, value, recent, None, now):
            continue
        if _in_cooldown(db, elder.id, rule, now):
            continue
        summary = f"{elder.name} {_window_brief(rule, value)}"
        detail = {
            "rule_code": rule.code,
            "rule_name": rule.name,
            "rule_type": str(rule.rule_type),
            "metric_code": metric.metric_code,
            "metric_id": metric.id,
            "operator": rule.operator,
            "threshold": rule.threshold,
            "threshold2": rule.threshold2,
            "window_size": rule.window_size,
            "measured_at": metric.measured_at.isoformat() if metric.measured_at else None,
            "recent_values": [float(v) for v in recent],
        }
        alerts.append(
            _create_alert(
                db,
                elder=elder,
                rule=rule,
                level=str(rule.level),
                summary=summary,
                now=now,
                metric=metric,
                detail=detail,
                operator_id=operator_id,
            )
        )
    if alerts:
        db.commit()
    return alerts


# ---------------------------------------------------------------- 静默检测 / 超时升级
def _has_pending_silence(db: Session, elder_id: int) -> bool:
    row = db.execute(
        select(Alert.id)
        .join(AlertRule, Alert.rule_id == AlertRule.id)
        .where(
            Alert.elder_id == elder_id,
            Alert.status == AlertStatus.PENDING.value,
            AlertRule.code == SILENCE_CODE,
        )
        .limit(1)
    ).scalar_one_or_none()
    return row is not None


def _street_org_id(db: Session, elder: Elder | None) -> int | None:
    org = db.get(Org, elder.community_org_id) if elder is not None and elder.community_org_id else None
    hops = 0
    while org is not None and org.parent_id is not None and hops < 6:
        org = db.get(Org, org.parent_id)
        hops += 1
    if org is not None and str(org.level) == "street":
        return org.id
    return db.execute(
        select(Org.id).where(Org.level == "street").order_by(Org.id.asc()).limit(1)
    ).scalar_one_or_none()


def _notify_escalation(
    db: Session, elder: Elder | None, alert: Alert, now: datetime, street_id: int | None
) -> int:
    elder_name = elder.name if elder is not None else f"老人#{alert.elder_id}"
    title = f"【预警升级】{elder_name} {alert.summary or ''}"[:128]
    content = (
        f"{now:%Y-%m-%d %H:%M} 预警（{level_name(alert.level)}）超过处置时限仍未闭环，"
        f"已升级至街道监管：{alert.summary or ''}"
    )
    targets = _street_user_ids(db, street_id)
    if not targets:
        targets = _grid_user_ids(db, elder)
    sent = 0
    for uid in sorted({uid for uid in targets if uid}):
        db.add(
            Notification(
                user_id=uid,
                elder_id=alert.elder_id,
                channel="inapp",
                title=title,
                content=content,
                status="sent",
                related_type="alert",
                related_id=alert.id,
            )
        )
        sent += 1
    if sent == 0:
        db.add(
            Notification(
                user_id=None,
                elder_id=alert.elder_id,
                channel="sms",
                title=title,
                content=content,
                status="sent",
                related_type="alert",
                related_id=alert.id,
            )
        )
        sent = 1
    return sent


def _escalate_overdue(db: Session, now: datetime) -> int:
    overdue_alerts = db.execute(
        select(Alert)
        .where(
            Alert.status == AlertStatus.PENDING.value,
            Alert.deadline_at.is_not(None),
            Alert.deadline_at < now,
            Alert.escalated_at.is_(None),
        )
        .order_by(Alert.id.asc())
    ).scalars().all()
    escalated = 0
    for alert in overdue_alerts:
        elder = db.get(Elder, alert.elder_id) if alert.elder_id else None
        street_id = _street_org_id(db, elder)
        alert.escalated_at = now
        alert.escalated_to_org_id = street_id
        db.add(
            AlertAction(
                alert_id=alert.id,
                action_type="escalate",
                channel="inapp",
                target=f"street:{street_id or '-'}",
                operator_id=None,
                note="超过处置时限未闭环，自动升级至街道",
                action_at=now,
            )
        )
        _notify_escalation(db, elder, alert, now, street_id)
        escalated += 1
    return escalated


def scan_alerts(db: Session, now: datetime | None = None) -> dict[str, int]:
    """静默检测 + 超时升级；返回统计信息。"""
    now = now or utcnow()
    ensure_default_rules(db)
    silence_rule = db.execute(
        select(AlertRule).where(AlertRule.code == SILENCE_CODE).limit(1)
    ).scalar_one_or_none()
    hours = (
        silence_rule.silence_hours
        if silence_rule is not None and silence_rule.silence_hours
        else settings.alert_silence_hours
    )

    elders = db.execute(select(Elder).where(Elder.is_deleted == 0)).scalars().all()
    silence_created = 0
    for elder in elders:
        last_at = db.execute(
            select(func.max(HealthMetric.measured_at)).where(HealthMetric.elder_id == elder.id)
        ).scalar_one_or_none()
        if not is_silent(last_at, now, hours):
            continue
        if _has_pending_silence(db, elder.id):
            continue
        last_text = last_at.strftime("%Y-%m-%d %H:%M") if last_at is not None else "从未上报"
        summary = f"{elder.name} 连续{int(hours)}小时未上报健康数据（最近：{last_text}）"
        _create_alert(
            db,
            elder=elder,
            rule=silence_rule,
            level=str(silence_rule.level) if silence_rule is not None else AlertLevel.BLUE.value,
            summary=summary,
            now=now,
            detail={
                "rule_code": SILENCE_CODE,
                "silence_hours": int(hours),
                "last_metric_at": last_at.isoformat() if last_at is not None else None,
            },
        )
        silence_created += 1

    escalated = _escalate_overdue(db, now)
    db.commit()
    return {
        "elders_scanned": len(elders),
        "silence_created": silence_created,
        # 兼容契约命名：silenced 与 silence_created 同义
        "silenced": silence_created,
        "escalated": escalated,
    }


# ---------------------------------------------------------------- 列表 / 详情 / 处置
def list_alerts(
    db: Session,
    user: User,
    *,
    level: str | None = None,
    status: str | None = None,
    elder_id: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    conditions = []
    if level:
        level_value = str(level)
        if level_value not in ALERT_LEVELS:
            raise RuleViolation("level 只能是 red / yellow / blue")
        conditions.append(Alert.level == level_value)
    if status:
        status_value = str(status)
        if status_value not in ALERT_STATUSES:
            raise RuleViolation("status 只能是 pending / resolved / false_alarm / referred")
        conditions.append(Alert.status == status_value)
    if elder_id:
        conditions.append(Alert.elder_id == int(elder_id))

    offset, limit = _page_bounds(page, size)
    count_stmt = _scoped_stmt(db, user, select(func.count(Alert.id)))
    stmt = _scoped_stmt(db, user, select(Alert))
    if conditions:
        count_stmt = count_stmt.where(*conditions)
        stmt = stmt.where(*conditions)
    total = int(db.execute(count_stmt).scalar_one() or 0)
    level_rank = case(
        (Alert.level == AlertLevel.RED.value, 3),
        (Alert.level == AlertLevel.YELLOW.value, 2),
        else_=1,
    )
    rows = db.execute(
        stmt.order_by(level_rank.desc(), Alert.created_at.desc(), Alert.id.desc())
        .offset(offset)
        .limit(limit)
    ).scalars().all()
    return [serialize_alert(db, alert) for alert in rows], total


def ack_alert(
    db: Session, user: User, alert_id: int, request: Request | None = None
) -> dict[str, Any]:
    stmt = _scoped_stmt(db, user, select(Alert).where(Alert.id == alert_id))
    alert = db.execute(stmt).scalar_one_or_none()
    if alert is None:
        raise NotFound("预警不存在或无权访问")
    if str(alert.status) != AlertStatus.PENDING.value:
        raise Conflict("预警已闭环，无法确认响应")
    now = utcnow()
    start = alert.pushed_at or alert.created_at or now
    if alert.response_seconds is None:
        alert.response_seconds = max(0, int((now - start).total_seconds()))
    db.add(
        AlertAction(
            alert_id=alert.id,
            action_type="ack",
            channel="inapp",
            target="grid",
            operator_id=user.id,
            note="已确认收到预警",
            action_at=now,
        )
    )
    write_audit(
        db,
        user=user,
        action="ack",
        entity_type="alert",
        entity_id=alert.id,
        after={"response_seconds": alert.response_seconds},
        detail="确认响应预警",
        request=request,
    )
    db.commit()
    db.refresh(alert)
    return serialize_alert(db, alert, include_actions=True, now=now)


def handle_alert(
    db: Session,
    user: User,
    alert_id: int,
    *,
    resolution_type: str,
    measure: str | None = None,
    note: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    resolution = str(resolution_type or "").strip()
    action = RESOLUTION_ACTIONS.get(resolution)
    if action is None:
        raise RuleViolation("resolution_type 只能是 resolved / false_alarm / referred")

    stmt = _scoped_stmt(db, user, select(Alert).where(Alert.id == alert_id))
    alert = db.execute(stmt).scalar_one_or_none()
    if alert is None:
        raise NotFound("预警不存在或无权访问")

    now = utcnow()
    before = {"status": str(alert.status), "resolution_type": alert.resolution_type}
    target = ALERT.next(str(alert.status), action)
    alert.status = target
    alert.resolution_type = resolution
    alert.measure = (measure or "").strip() or None
    alert.resolution_note = note
    alert.resolved_at = now
    alert.resolved_by = user.id
    if alert.created_at is not None:
        alert.handling_seconds = max(0, int((now - alert.created_at).total_seconds()))
    db.add(
        AlertAction(
            alert_id=alert.id,
            action_type="resolve",
            channel="inapp",
            target="网格",
            operator_id=user.id,
            note=(f"[{resolution}] {(measure or '').strip()} {note or ''}".strip())[:255],
            action_at=now,
        )
    )
    write_audit(
        db,
        user=user,
        action="handle",
        entity_type="alert",
        entity_id=alert.id,
        before=before,
        after={
            "status": str(alert.status),
            "resolution_type": resolution,
            "measure": alert.measure,
            "handling_seconds": alert.handling_seconds,
        },
        detail="预警闭环处置",
        request=request,
    )
    db.commit()
    db.refresh(alert)
    return serialize_alert(db, alert, include_actions=True, now=now)


# ---------------------------------------------------------------- 统计
def _median(values: list[int | float]) -> float | None:
    numbers = [float(v) for v in values if v is not None]
    if not numbers:
        return None
    return round(float(statistics.median(numbers)), 1)


def alert_stats(db: Session, user: User) -> dict[str, Any]:
    stmt = _scoped_stmt(
        db,
        user,
        select(
            Alert.level,
            Alert.status,
            Alert.response_seconds,
            Alert.handling_seconds,
            Alert.escalated_at,
        ),
    )
    rows = db.execute(stmt).all()
    total = len(rows)
    red_total = sum(1 for row in rows if str(row.level) == AlertLevel.RED.value)
    pending = sum(1 for row in rows if str(row.status) == AlertStatus.PENDING.value)
    closed = total - pending
    false_alarm = sum(1 for row in rows if str(row.status) == AlertStatus.FALSE_ALARM.value)
    escalated = sum(1 for row in rows if row.escalated_at is not None)
    by_level = {level: 0 for level in ALERT_LEVELS}
    by_status = {status: 0 for status in ALERT_STATUSES}
    for row in rows:
        by_level[str(row.level)] = by_level.get(str(row.level), 0) + 1
        by_status[str(row.status)] = by_status.get(str(row.status), 0) + 1
    return {
        "total": total,
        "red_total": red_total,
        "pending": pending,
        "closed": closed,
        "escalated": escalated,
        "response_median_seconds": _median([row.response_seconds for row in rows]),
        "handling_median_seconds": _median([row.handling_seconds for row in rows]),
        "false_alarm_rate": round(false_alarm / closed, 4) if closed else 0.0,
        "by_level": by_level,
        "by_status": by_status,
    }
