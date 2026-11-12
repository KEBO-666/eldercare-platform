"""健康数据接入 API：指标录入（单条/批量）、指标查询、趋势聚合、设备绑定。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.deps import ensure_elder_visible, require
from app.core.exceptions import RuleViolation
from app.core.response import ok, paged
from app.db.session import get_db
from app.models import User
from app.services import health_service as health_svc

router = APIRouter(tags=["健康数据接入"])


# ---------------------------------------------------------------- 入参模型
class MetricCreateIn(BaseModel):
    elder_id: int = Field(..., description="老人 id")
    metric_code: str = Field(..., description="指标编码：sbp/dbp/hr/spo2/steps/motion")
    value: float = Field(..., description="指标值")
    measured_at: datetime | None = Field(None, description="测量时间，缺省为当前时间")
    source: str | None = Field(None, description="数据来源：visit（上门录入，默认）/ wearable（可穿戴）")
    unit: str | None = Field(None, description="单位，缺省按指标自动补齐")
    device_id: int | None = Field(None, description="来源设备 id（可穿戴场景）")
    work_order_id: int | None = Field(None, description="关联工单 id（上门录入场景）")

    @field_validator("value", mode="before")
    @classmethod
    def reject_boolean_value(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise RuleViolation("指标值必须是有限数字", http_status=422)
        return value

    @field_validator("measured_at", mode="before")
    @classmethod
    def normalize_measured_time(cls, value: Any) -> datetime | None:
        return health_svc.to_naive(value)


class MetricBatchIn(BaseModel):
    # 保留原始条目，避免单条字段校验错误在逐条处理前拒绝整批。
    items: list[Any] = Field(default_factory=list, description="待录入指标列表，每条独立校验")


class DeviceBindIn(BaseModel):
    elder_id: int = Field(..., description="老人 id")
    device_code: str = Field(..., description="设备编号（全局唯一）")
    device_type: str = Field("band", description="设备类型：band/watch/bp")
    status: str = Field("online", description="设备状态")


# ---------------------------------------------------------------- 指标录入
@router.post("/health/metrics", summary="录入单条健康指标")
def create_metric(
    payload: MetricCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("health:write")),
) -> dict:
    """校验指标编码 → 写入 HealthMetric（recorded_by=当前用户）→ 触发预警评估。

    预警评估异常不影响指标入库，并通过 alert_evaluation 明确返回处理状态。
    """
    ensure_elder_visible(db, user, payload.elder_id)
    result = health_svc.create_metric_report(
        db,
        elder_id=payload.elder_id,
        metric_code=payload.metric_code,
        value=payload.value,
        measured_at=payload.measured_at,
        source=payload.source,
        unit=payload.unit,
        device_id=payload.device_id,
        work_order_id=payload.work_order_id,
        recorded_by=user.id,
    )
    return ok(result, message="录入成功")


@router.post("/health/metrics/batch", summary="批量录入健康指标")
def create_metrics_batch(
    payload: MetricBatchIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("health:write")),
) -> dict:
    """逐条独立处理：成功的入库、失败的返回 index/reason，不因单条失败回滚整批。"""
    if not payload.items:
        raise RuleViolation("items 不能为空", http_status=422)
    result = health_svc.create_metrics_batch(
        db,
        payload.items,
        operator_id=user.id,
        user=user,
    )
    return ok(result, message=f"批量录入完成：成功 {result['success']} 条")


# ---------------------------------------------------------------- 指标查询
@router.get("/health/elders/{elder_id}/metrics", summary="分页查询老人健康指标")
def list_metrics(
    elder_id: int,
    metric_code: str | None = Query(None, description="指标编码过滤"),
    from_time: datetime | None = Query(None, description="起始测量时间"),
    to_time: datetime | None = Query(None, description="结束测量时间"),
    from_: datetime | None = Query(None, alias="from", description="起始测量时间（等价 from_time）"),
    to_: datetime | None = Query(None, alias="to", description="结束测量时间（等价 to_time）"),
    page: int = Query(1, ge=1, description="页码"),
    size: int = Query(20, ge=1, le=200, description="每页条数"),
    db: Session = Depends(get_db),
    user: User = Depends(require("health:read")),
) -> dict:
    """按测量时间倒序分页返回，受当前用户数据范围约束。"""
    ensure_elder_visible(db, user, elder_id)
    items, total = health_svc.list_metrics(
        db,
        elder_id=elder_id,
        metric_code=metric_code,
        from_time=from_time or from_,
        to_time=to_time or to_,
        page=page,
        size=size,
    )
    return paged([health_svc.metric_to_dict(item) for item in items], total, page, size)


@router.get("/health/elders/{elder_id}/series", summary="健康指标按日聚合趋势")
def metric_series(
    elder_id: int,
    metric_code: str = Query("sbp", description="指标编码，默认 sbp"),
    days: int = Query(30, ge=1, le=3650, description="统计最近天数"),
    db: Session = Depends(get_db),
    user: User = Depends(require("health:read")),
) -> dict:
    """按日聚合，返回 [{date, avg, min, max, count}]（按日期升序）。"""
    ensure_elder_visible(db, user, elder_id)
    series = health_svc.metric_series(db, elder_id=elder_id, metric_code=metric_code, days=days)
    return ok(series)


# ---------------------------------------------------------------- 设备
@router.get("/health/devices", summary="健康设备列表")
def list_devices(
    elder_id: int | None = Query(None, description="老人 id 过滤"),
    device_type: str | None = Query(None, description="设备类型过滤"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require("health:read")),
) -> dict:
    """支持按老人过滤，并自动按当前用户数据范围裁剪。"""
    if elder_id is not None:
        ensure_elder_visible(db, user, elder_id)
    items, total = health_svc.list_devices(
        db,
        user=user,
        elder_id=elder_id,
        device_type=device_type,
        page=page,
        size=size,
    )
    return paged([health_svc.device_to_dict(item) for item in items], total, page, size)


@router.post("/health/devices", summary="绑定健康设备")
def bind_device(
    payload: DeviceBindIn,
    db: Session = Depends(get_db),
    user: User = Depends(require("health:write")),
) -> dict:
    """device_code 全局唯一，重复绑定返回 409 CONFLICT。"""
    ensure_elder_visible(db, user, payload.elder_id)
    device = health_svc.bind_device(
        db,
        elder_id=payload.elder_id,
        device_code=payload.device_code,
        device_type=payload.device_type,
        status=payload.status,
        operator_id=user.id,
    )
    return ok(health_svc.device_to_dict(device), message="绑定成功")
