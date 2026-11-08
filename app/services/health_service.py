"""健康指标与健康设备接入服务。

约定：
- 所有时间字段使用 naive datetime（与库表一致）；
- 预警评估由 D2 的 ``app.services.alert_service.evaluate_metric`` 提供，
  该模块尚未就绪或评估异常时**不影响指标入库**（指标优先落库）；
- 业务异常统一抛 ``app.core.exceptions`` 子类，由 main.py 的 handler 统一转 JSON。
"""
from __future__ import annotations

import inspect
import logging
import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import MetricCode, MetricSource
from app.core.exceptions import BizError, Conflict, NotFound, RuleViolation
from app.models import Device, Elder, HealthMetric

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


# ---------------------------------------------------------------- 基础工具
def naive_now() -> datetime:
    """当前本地时间（naive）。"""
    return datetime.now()


def to_naive(value: datetime | None) -> datetime | None:
    """把可能带时区的时间统一转成 naive datetime。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


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
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuleViolation(f"指标值必须是数字：{value}", http_status=422) from exc
    if not math.isfinite(number):
        raise RuleViolation("指标值不合法（NaN/Infinity）", http_status=422)
    low, high = METRIC_RANGES[metric_code]
    if number < low or number > high:
        raise RuleViolation(f"指标值超出合理区间 [{low:g}, {high:g}]：{number:g}", http_status=422)
    return number


def get_elder_or_404(db: Session, elder_id: int) -> Elder:
    elder = db.get(Elder, int(elder_id))
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


def evaluate_metric_safe(db: Session, metric: HealthMetric, operator_id: int | None = None) -> list[dict]:
    """调用预警评估；模块缺失或评估异常时降级为空列表（不影响指标入库）。"""
    try:
        from app.services.alert_service import evaluate_metric  # D2 提供的函数
    except Exception:  # noqa: BLE001 - 模块尚未就绪（D2 并行开发中）
        logger.debug("alert_service 尚未就绪，跳过预警评估 metric_id=%s", metric.id)
        return []

    try:
        alerts = _call_evaluate_metric(db, metric, evaluate_metric, operator_id) or []
        db.commit()
    except Exception:  # noqa: BLE001 - 预警失败不能回滚已入库的指标
        logger.exception("预警评估失败（指标已入库）metric_id=%s", metric.id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("预警评估失败后的 rollback 异常", exc_info=True)
        return []

    briefs: list[dict] = []
    for alert in alerts:
        try:
            briefs.append(_alert_brief(alert))
        except Exception:  # noqa: BLE001 - 单条序列化失败不影响整体
            logger.debug("预警对象序列化失败", exc_info=True)
    return briefs


# ---------------------------------------------------------------- 指标写入
def create_metric(
    db: Session,
    *,
    elder_id: int,
    metric_code: str | MetricCode,
    value: float,
    measured_at: datetime | None = None,
    source: str | MetricSource | None = None,
    unit: str | None = None,
    device_id: int | None = None,
    work_order_id: int | None = None,
    recorded_by: int | None = None,
    raw: dict | None = None,
) -> tuple[HealthMetric, list[dict]]:
    """写入单条健康指标并触发预警评估，返回 (指标, 预警简要列表)。"""
    get_elder_or_404(db, elder_id)
    code = normalize_metric_code(metric_code)
    source_value = normalize_source(source)
    number = validate_value(code, value)

    metric = HealthMetric(
        elder_id=int(elder_id),
        metric_code=code,
        value=number,
        unit=unit if unit is not None else METRIC_UNITS.get(code),
        source=source_value,
        device_id=device_id,
        work_order_id=work_order_id,
        measured_at=to_naive(measured_at) or naive_now(),
        recorded_by=recorded_by,
        raw=raw,
    )
    db.add(metric)
    db.commit()
    db.refresh(metric)
    metric_id = metric.id

    alerts = evaluate_metric_safe(db, metric, operator_id=recorded_by)

    try:  # 预警评估可能 commit 导致对象过期，尽量刷新，失败也不影响返回主键
        db.refresh(metric)
    except Exception:  # noqa: BLE001
        logger.debug("健康指标刷新失败 metric_id=%s", metric_id, exc_info=True)
    return metric, alerts


def create_metrics_batch(
    db: Session,
    items: list[dict],
    *,
    operator_id: int | None = None,
    user: Any | None = None,
) -> dict[str, Any]:
    """批量写入：逐条独立成败，返回 {success, failed:[{index, reason}], alerts_count}。"""
    success = 0
    alerts_count = 0
    failed: list[dict[str, Any]] = []

    for index, item in enumerate(items):
        try:
            elder_id = int(item.get("elder_id"))
            if user is not None:
                from app.core.deps import ensure_elder_visible  # 延迟导入，避免循环依赖

                ensure_elder_visible(db, user, elder_id)
            _, alerts = create_metric(
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
            )
            success += 1
            alerts_count += len(alerts)
        except BizError as exc:
            db.rollback()
            failed.append({"index": index, "reason": exc.message})
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断整批
            db.rollback()
            logger.exception("批量写入健康指标失败 index=%s", index)
            failed.append({"index": index, "reason": str(exc) or exc.__class__.__name__})

    return {
        "success": success,
        "failed": failed,
        "alerts_count": alerts_count,
        # 兼容契约命名：created_alerts 与 alerts_count 同义
        "created_alerts": alerts_count,
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
