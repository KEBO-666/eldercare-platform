"""健康指标与健康设备接入服务。

约定：
- 所有时间字段使用北京时间的 naive datetime（与库表一致，部署主机使用北京时间）；
- 指标优先独立落库，预警评估异常时保留指标并明确返回失败状态；
- 业务异常统一抛 ``app.core.exceptions`` 子类，由 main.py 的 handler 统一转 JSON。
"""
from __future__ import annotations

import inspect
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.enums import MetricCode, MetricSource
from app.core.exceptions import BizError, Conflict, NotFound, RuleViolation
from app.models import Device, Elder, HealthMetric, User, WorkOrder

logger = logging.getLogger(__name__)

# 指标默认单位（录入时若未显式提供则按指标补齐）
METRIC_UNITS: dict[str, str | None] = {
    MetricCode.SBP.value: "mmHg",
    MetricCode.DBP.value: "mmHg",
    MetricCode.HR.value: "次/分",
    MetricCode.SPO2.value: "%",
    MetricCode.STEPS.value: "步",
    MetricCode.MOTION.value: None,
}

# 指标的合理值区间（含边界），用于挡住明显异常的脏数据
METRIC_RANGES: dict[str, tuple[float, float]] = {
    MetricCode.SBP.value: (0.0, 300.0),
    MetricCode.DBP.value: (0.0, 200.0),
    MetricCode.HR.value: (0.0, 300.0),
    MetricCode.SPO2.value: (0.0, 100.0),
    MetricCode.STEPS.value: (0.0, 200000.0),
    MetricCode.MOTION.value: (0.0, 100.0),
}

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200
PROJECT_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


# ---------------------------------------------------------------- 基础工具
def naive_now() -> datetime:
    """当前服务器本地时间（部署约定为北京时间，与其他业务时钟一致）。"""
    return datetime.now()


def to_naive(value: datetime | str | None) -> datetime | None:
    """以北京时间入库；无时区输入沿用北京时间，带时区输入保留同一时刻。"""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if not isinstance(value, datetime):
            raise ValueError("not a datetime")
        if value.tzinfo is not None:
            return value.astimezone(PROJECT_TIMEZONE).replace(tzinfo=None)
        return value
    except (ValueError, TypeError, OverflowError) as exc:
        raise RuleViolation("测量时间必须是有效的 ISO 日期时间", http_status=422) from exc


def normalize_metric_code(metric_code: str | MetricCode | None) -> str:
    """校验并归一化指标编码，不合法抛 RULE_VIOLATION(422)。"""
    raw = metric_code.value if isinstance(metric_code, MetricCode) else str(metric_code or "").strip()
    try:
        return MetricCode(raw).value
    except ValueError as exc:
        valid = "、".join(item.value for item in MetricCode)
        raise RuleViolation(f"不支持的指标编码：{raw or '(空)'}；可选值：{valid}", http_status=422) from exc


def normalize_source(source: str | MetricSource | None) -> str:
    if source is None or source == "":
        return MetricSource.VISIT.value
    raw = source.value if isinstance(source, MetricSource) else str(source).strip()
    try:
        return MetricSource(raw).value
    except ValueError as exc:
        valid = "、".join(item.value for item in MetricSource)
        raise RuleViolation(f"不支持的数据来源：{raw}；可选值：{valid}", http_status=422) from exc


def validate_value(metric_code: str, value: float) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a metric")
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuleViolation("指标值必须是有限数字", http_status=422) from exc
    if not math.isfinite(number):
        raise RuleViolation("指标值不合法（NaN/Infinity）", http_status=422)
    low, high = METRIC_RANGES[metric_code]
    if number < low or number > high:
        raise RuleViolation(f"指标值超出合理区间 [{low:g}, {high:g}]：{number:g}", http_status=422)
    return number


def _positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise RuleViolation(f"{label}必须是正整数", http_status=422)
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuleViolation(f"{label}必须是正整数", http_status=422) from exc
    if number <= 0:
        raise RuleViolation(f"{label}必须是正整数", http_status=422)
    return number


def get_elder_or_404(db: Session, elder_id: int) -> Elder:
    elder = db.get(Elder, _positive_id(elder_id, "老人 id"))
    if elder is None or elder.is_deleted:
        raise NotFound("老人档案不存在")
    return elder


# ---------------------------------------------------------------- 预警评估（D2）
def _alert_brief(alert: Any) -> dict[str, Any]:
    """把预警对象（D2 返回的 Alert）转成接口可序列化的简要字典。"""
    level = getattr(alert, "level", None)
    status = getattr(alert, "status", None)
    return {
        "id": getattr(alert, "id", None),
        "level": getattr(level, "value", level),
        "status": getattr(status, "value", status),
        "summary": getattr(alert, "summary", None),
        "created_at": (
            getattr(alert, "created_at", None).isoformat()
            if getattr(alert, "created_at", None) is not None
            else None
        ),
    }


def _call_evaluate_metric(db: Session, metric: HealthMetric, func: Any, operator_id: int | None) -> Any:
    """按 D2 函数的实际签名传参（operator_id 兼容命名差异）。"""
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - 内建/无法解析签名
        return func(db, metric, operator_id=operator_id)
    accepts_keyword = "operator_id" in params or any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
    )
    if accepts_keyword:
        return func(db, metric, operator_id=operator_id)
    return func(db, metric)


def evaluate_metric_report_safe(
    db: Session, metric: HealthMetric, operator_id: int | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """指标先独立保存；预警副作用独立提交，失败时返回可辨识的评估状态。"""
    metric_id = metric.id
    try:
        from app.services.alert_service import evaluate_metric_report

        with db.begin_nested():
            report = _call_evaluate_metric(db, metric, evaluate_metric_report, operator_id)
            briefs = [_alert_brief(alert) for alert in report["alerts"]]
            evaluation = {key: value for key, value in report.items() if key != "alerts"}
            write_audit(db, user=db.get(User, operator_id) if operator_id else None,
                        action="evaluate", entity_type="health_metric", entity_id=metric_id,
                        after={**evaluation, "alert_ids": [a["id"] for a in briefs]},
                        detail="健康指标预警评估", request=request)
        db.commit()
    except Exception:  # noqa: BLE001 - 预警失败不能回滚已独立提交的指标
        logger.exception("预警评估失败（指标已入库）metric_id=%s", metric_id)
        db.rollback()
        write_audit(db, user=db.get(User, operator_id) if operator_id else None,
                    action="evaluate", entity_type="health_metric", entity_id=metric_id,
                    after={"status": "error", "alert_ids": []},
                    detail="指标已保存，预警评估失败，需人工核实", request=request)
        db.commit()
        return {
            "alerts": [],
            "alert_evaluation": {
                "status": "error",
                "evaluated_rule_count": None,
                "matched_rule_count": None,
                "suppressed": [],
                "message": "指标已保存，但预警评估失败，请联系管理员处理；请勿重复录入该指标。",
            },
        }

    messages = {
        "created": "指标已保存，已生成预警。",
        "no_match": "指标已保存，本次未命中已启用的预警规则。",
        "cooldown": "指标已保存，已命中规则，冷却期内不重复生成预警。",
        "no_rules": "指标已保存，此指标暂无已启用的预警规则。",
    }
    evaluation["message"] = messages[evaluation["status"]]
    return {"alerts": briefs, "alert_evaluation": evaluation}


def evaluate_metric_safe(db: Session, metric: HealthMetric, operator_id: int | None = None) -> list[dict]:
    """兼容旧调用方的预警简表；新接口应读取完整评估状态。"""
    return evaluate_metric_report_safe(db, metric, operator_id)["alerts"]


# ---------------------------------------------------------------- 指标写入
def create_metric_report(
    db: Session,
    *,
    elder_id: int,
    metric_code: str | MetricCode,
    value: float,
    measured_at: datetime | str | None = None,
    source: str | MetricSource | None = None,
    unit: str | None = None,
    device_id: int | None = None,
    work_order_id: int | None = None,
    recorded_by: int | None = None,
    raw: dict | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """写入单条指标并返回保存结果与可辨识的预警评估状态。"""
    elder = get_elder_or_404(db, elder_id)
    code = normalize_metric_code(metric_code)
    source_value = normalize_source(source)
    number = validate_value(code, value)
    measured_time = to_naive(measured_at) or naive_now()
    if unit is not None and (not isinstance(unit, str) or len(unit) > 16):
        raise RuleViolation("单位必须是最多 16 字的字符串", http_status=422)
    if raw is not None and not isinstance(raw, dict):
        raise RuleViolation("原始数据必须是对象", http_status=422)
    for label, model, reference_id in (
        ("设备", Device, device_id), ("工单", WorkOrder, work_order_id),
    ):
        if reference_id is not None:
            reference_id = _positive_id(reference_id, f"{label} id")
            reference = db.get(model, reference_id)
            if reference is None or reference.elder_id != elder.id:
                raise RuleViolation(f"关联{label}不存在或不属于当前老人", http_status=422)
            if model is Device:
                device_id = reference_id
            else:
                work_order_id = reference_id

    metric = HealthMetric(
        elder_id=elder.id,
        metric_code=code,
        value=number,
        unit=unit if unit is not None else METRIC_UNITS.get(code),
        source=source_value,
        device_id=device_id,
        work_order_id=work_order_id,
        measured_at=measured_time,
        recorded_by=recorded_by,
        raw=raw,
    )
    with db.begin_nested():
        db.add(metric)
        db.flush()
        write_audit(db, user=db.get(User, recorded_by) if recorded_by else None,
                    action="create", entity_type="health_metric", entity_id=metric.id,
                    after=metric_to_dict(metric), detail="录入健康指标", request=request)
    db.commit()
    db.refresh(metric)
    result = {"metric_id": metric.id, "metric": metric_to_dict(metric)}
    result.update(evaluate_metric_report_safe(db, metric, operator_id=recorded_by, request=request))
    return result


def create_metric(db: Session, **kwargs: Any) -> tuple[HealthMetric, list[dict]]:
    """兼容既有服务调用的 (指标, 新预警列表) 返回约定。"""
    result = create_metric_report(db, **kwargs)
    return db.get(HealthMetric, result["metric_id"]), result["alerts"]


def create_metrics_batch(
    db: Session,
    items: list[Any],
    *,
    operator_id: int | None = None,
    user: Any | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    """批量写入：逐条独立成败，返回 {success, failed:[{index, reason}], alerts_count}。"""
    success = 0
    alerts_count = 0
    failed: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for index, item in enumerate(items):
        try:
            if not isinstance(item, dict):
                raise RuleViolation("每条指标必须是对象", http_status=422)
            elder_id = _positive_id(item.get("elder_id"), "老人 id")
            if user is not None:
                from app.core.deps import ensure_elder_visible  # 延迟导入，避免循环依赖

                ensure_elder_visible(db, user, elder_id)
            result = create_metric_report(
                db,
                elder_id=elder_id,
                metric_code=item.get("metric_code"),
                value=item.get("value"),
                measured_at=item.get("measured_at"),
                source=item.get("source"),
                unit=item.get("unit"),
                device_id=item.get("device_id"),
                work_order_id=item.get("work_order_id"),
                recorded_by=operator_id,
                raw=item.get("raw"),
                request=request,
            )
            success += 1
            alerts_count += len(result["alerts"])
            results.append({"index": index, **result})
        except BizError as exc:
            db.rollback()
            failed.append({"index": index, "reason": exc.message, "code": exc.code})
        except Exception:  # noqa: BLE001 - 单条失败不阻断整批
            db.rollback()
            logger.exception("批量写入健康指标失败 index=%s", index)
            failed.append({"index": index, "reason": "指标保存失败，请联系管理员", "code": "SAVE_FAILED"})

    return {
        "success": success,
        "failed": failed,
        "alerts_count": alerts_count,
        # 兼容契约命名：created_alerts 与 alerts_count 同义
        "created_alerts": alerts_count,
        "results": results,
    }


# ---------------------------------------------------------------- 指标查询
def list_metrics(
    db: Session,
    *,
    elder_id: int,
    metric_code: str | MetricCode | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    page: int = 1,
    size: int = DEFAULT_PAGE_SIZE,
) -> tuple[list[HealthMetric], int]:
    """分页查询老人指标，时间倒序。"""
    page = max(1, int(page or 1))
    size = min(MAX_PAGE_SIZE, max(1, int(size or DEFAULT_PAGE_SIZE)))

    conds = [HealthMetric.elder_id == int(elder_id)]
    if metric_code:
        conds.append(HealthMetric.metric_code == normalize_metric_code(metric_code))
    start = to_naive(from_time)
    end = to_naive(to_time)
    if start is not None:
        conds.append(HealthMetric.measured_at >= start)
    if end is not None:
        conds.append(HealthMetric.measured_at <= end)

    total = int(db.execute(select(func.count(HealthMetric.id)).where(*conds)).scalar_one() or 0)
    stmt = (
        select(HealthMetric)
        .where(*conds)
        .order_by(HealthMetric.measured_at.desc(), HealthMetric.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    items = list(db.execute(stmt).scalars().all())
    return items, total


def metric_series(
    db: Session,
    *,
    elder_id: int,
    metric_code: str | MetricCode = MetricCode.SBP,
    days: int = 30,
) -> list[dict[str, Any]]:
    """按日聚合指标趋势：[{date, avg, min, max, count}]。"""
    code = normalize_metric_code(metric_code)
    days = min(3650, max(1, int(days or 30)))
    start = naive_now() - timedelta(days=days)

    day_col = func.date(HealthMetric.measured_at)
    stmt = (
        select(
            day_col.label("day"),
            func.avg(HealthMetric.value).label("avg_value"),
            func.min(HealthMetric.value).label("min_value"),
            func.max(HealthMetric.value).label("max_value"),
            func.count(HealthMetric.id).label("cnt"),
        )
        .where(
            HealthMetric.elder_id == int(elder_id),
            HealthMetric.metric_code == code,
            HealthMetric.measured_at >= start,
        )
        .group_by(day_col)
        .order_by(day_col)
    )

    series: list[dict[str, Any]] = []
    for row in db.execute(stmt).all():
        series.append(
            {
                "date": str(row.day),
                "avg": round(float(row.avg_value), 2) if row.avg_value is not None else None,
                "min": round(float(row.min_value), 2) if row.min_value is not None else None,
                "max": round(float(row.max_value), 2) if row.max_value is not None else None,
                "count": int(row.cnt or 0),
            }
        )
    return series


# ---------------------------------------------------------------- 设备
def list_devices(
    db: Session,
    *,
    user: Any | None = None,
    elder_id: int | None = None,
    device_type: str | None = None,
    page: int = 1,
    size: int = DEFAULT_PAGE_SIZE,
) -> tuple[list[Device], int]:
    """设备列表：支持 elder_id 过滤，并按当前用户数据范围裁剪。"""
    from app.core.deps import ensure_elder_visible, visible_elder_ids

    page = max(1, int(page or 1))
    size = min(MAX_PAGE_SIZE, max(1, int(size or DEFAULT_PAGE_SIZE)))

    conds = []
    if elder_id is not None:
        if user is not None:
            ensure_elder_visible(db, user, int(elder_id))
        conds.append(Device.elder_id == int(elder_id))
    elif user is not None:
        allowed = visible_elder_ids(db, user)
        if allowed is not None:
            if not allowed:
                return [], 0
            conds.append(Device.elder_id.in_(allowed))
    if device_type:
        conds.append(Device.device_type == str(device_type))

    total = int(db.execute(select(func.count(Device.id)).where(*conds)).scalar_one() or 0)
    stmt = (
        select(Device)
        .where(*conds)
        .order_by(Device.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    return list(db.execute(stmt).scalars().all()), total


def bind_device(
    db: Session,
    *,
    elder_id: int,
    device_code: str,
    device_type: str = "band",
    status: str = "online",
    operator_id: int | None = None,
) -> Device:
    """绑定设备；device_code 全局唯一，重复绑定抛 CONFLICT。"""
    get_elder_or_404(db, elder_id)
    code = str(device_code or "").strip()
    if not code:
        raise RuleViolation("设备编号不能为空", http_status=422)
    exists = db.execute(select(Device.id).where(Device.device_code == code)).scalar_one_or_none()
    if exists is not None:
        raise Conflict(f"设备编号已存在：{code}")

    device = Device(
        elder_id=int(elder_id),
        device_type=str(device_type or "band").strip() or "band",
        device_code=code,
        status=str(status or "online").strip() or "online",
        last_seen_at=naive_now(),
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    logger.info("设备绑定成功 elder_id=%s device_code=%s operator=%s", elder_id, code, operator_id)
    return device


# ---------------------------------------------------------------- 序列化
def metric_to_dict(metric: HealthMetric) -> dict[str, Any]:
    return {
        "id": metric.id,
        "elder_id": metric.elder_id,
        "metric_code": metric.metric_code,
        "value": metric.value,
        "unit": metric.unit,
        "source": metric.source,
        "device_id": metric.device_id,
        "work_order_id": metric.work_order_id,
        "recorded_by": metric.recorded_by,
        "measured_at": metric.measured_at.isoformat() if metric.measured_at else None,
        "created_at": metric.created_at.isoformat() if metric.created_at else None,
    }


def device_to_dict(device: Device) -> dict[str, Any]:
    return {
        "id": device.id,
        "elder_id": device.elder_id,
        "device_type": device.device_type,
        "device_code": device.device_code,
        "status": device.status,
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        "created_at": device.created_at.isoformat() if device.created_at else None,
    }
