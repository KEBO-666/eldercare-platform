"""上门服务执行与留痕 API（C 模块）。

路由（统一前缀 /api/v1）：
- GET  /orders                          工单列表（状态/护理员/老人/日期/分页 + 数据范围）
- GET  /orders/{id}                     工单详情（含项目勾选/照片/轨迹/异常/评价）
- POST /orders/{id}/accept              接单
- POST /orders/{id}/reject              拒单（回退预约到待派单）
- POST /orders/{id}/checkin             签到（200 米距离校验）
- POST /orders/{id}/items               逐项勾选
- POST /orders/{id}/photos              上传照片（multipart，≤3 张，Pillow 压缩）
- POST /orders/{id}/checkout            签退（时长异常判定 + 健康指标联动）
- POST /orders/{id}/anomaly             异常上报
- GET  /orders/anomalies                异常处置队列（含等待分钟数）
- POST /orders/anomalies/{id}/handle    异常处置
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import require
from app.core.exceptions import RuleViolation
from app.core.response import ok, paged
from app.db.session import get_db
from app.models import User
from app.services import order_service

router = APIRouter(tags=["上门服务执行与留痕"])


class RejectIn(BaseModel):
    reason: str = Field(default="", description="拒单原因（必填）")


class CheckinIn(BaseModel):
    lat: float
    lng: float
    note: str | None = Field(default=None, description="距离超限说明")


class ItemIn(BaseModel):
    name: str
    completed: bool = False


class ItemsIn(BaseModel):
    items: list[ItemIn]


class MetricIn(BaseModel):
    metric_code: str
    value: float


class CheckoutIn(BaseModel):
    summary: str = ""
    metrics: list[MetricIn] | None = None


class AnomalyIn(BaseModel):
    anomaly_type: str
    description: str = ""


class AnomalyHandleIn(BaseModel):
    handle_note: str = ""


# ---------------------------------------------------------------- 列表与队列
@router.get("/orders", summary="工单列表（含数据范围）")
def list_orders(
    status: str | None = Query(default=None),
    nurse_id: int | None = Query(default=None),
    elder_id: int | None = Query(default=None),
    date: str | None = Query(default=None, description="服务日期 YYYY-MM-DD"),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    date_from: str | None = Query(default=None, description="起始日期（start_date 别名）"),
    date_to: str | None = Query(default=None, description="截止日期（end_date 别名）"),
    community_org_id: int | None = Query(default=None),
    community_id: int | None = Query(default=None, description="community_org_id 别名"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require("order:read")),
):
    items, total = order_service.list_orders(
        db,
        user,
        status=status,
        nurse_id=nurse_id,
        elder_id=elder_id,
        day=date,
        start_date=start_date or date_from,
        end_date=end_date or date_to,
        community_org_id=community_org_id or community_id,
        page=page,
        size=size,
    )
    return paged(items, total, page, size)


@router.get("/orders/anomalies", summary="异常处置队列（含等待分钟数）")
def list_anomalies(
    status: str | None = Query(default="pending", description="pending/handled/all"),
    work_order_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require("order:read")),
):
    items, total = order_service.list_anomalies(
        db, user, status=status, work_order_id=work_order_id, page=page, size=size
    )
    return paged(items, total, page, size)


@router.post("/orders/anomalies/{anomaly_id}/handle", summary="处置异常（记录处置时长）")
def handle_anomaly(
    anomaly_id: int,
    payload: AnomalyHandleIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("order:read", "order:dispatch", "order:reassign", any_of=True)),
):
    data = order_service.handle_anomaly(db, user, anomaly_id, handle_note=payload.handle_note, request=request)
    return ok(data)


@router.get("/orders/{order_id}", summary="工单详情（含勾选/照片/轨迹/异常）")
def order_detail(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("order:read")),
):
    return ok(order_service.get_order_detail(db, user, order_id))


# ---------------------------------------------------------------- 接单 / 拒单
@router.post("/orders/{order_id}/accept", summary="接单")
def accept_order(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("order:execute")),
):
    return ok(order_service.accept_order(db, user, order_id, request=request))


@router.post("/orders/{order_id}/reject", summary="拒单（回退预约到待派单并通知网格员）")
def reject_order(
    order_id: int,
    payload: RejectIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("order:execute")),
):
    return ok(order_service.reject_order(db, user, order_id, payload.reason, request=request))


# ---------------------------------------------------------------- 签到 / 勾选 / 照片 / 签退
@router.post("/orders/{order_id}/checkin", summary="签到（距离服务地址 200 米内）")
def checkin_order(
    order_id: int,
    payload: CheckinIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("order:execute")),
):
    data = order_service.checkin_order(
        db, user, order_id, lat=payload.lat, lng=payload.lng, note=payload.note, request=request
    )
    return ok(data)


@router.post("/orders/{order_id}/items", summary="逐项勾选服务项目")
def update_items(
    order_id: int,
    payload: ItemsIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("order:execute")),
):
    items = [{"name": item.name, "completed": item.completed} for item in payload.items]
    return ok(order_service.update_items(db, user, order_id, items, request=request))


@router.post("/orders/{order_id}/photos", summary="上传服务照片（multipart，≤3 张，自动压缩）")
async def upload_photo(
    order_id: int,
    request: Request,
    file: UploadFile = File(..., description="服务照片文件，字段名 file"),
    db: Session = Depends(get_db),
    user: User = Depends(require("order:execute")),
):
    raw = await file.read()
    if not raw:
        raise RuleViolation("上传文件为空")
    data = order_service.save_photo(
        db, user, order_id, raw, filename=file.filename, request=request
    )
    return ok(data)


@router.post("/orders/{order_id}/checkout", summary="签退（时长异常判定 + 健康指标联动）")
def checkout_order(
    order_id: int,
    payload: CheckoutIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("order:execute")),
):
    metrics = (
        [{"metric_code": m.metric_code, "value": m.value} for m in payload.metrics]
        if payload.metrics
        else []
    )
    data = order_service.checkout_order(
        db, user, order_id, summary=payload.summary, metrics=metrics, request=request
    )
    return ok(data)


@router.post("/orders/{order_id}/anomaly", summary="异常上报")
def report_anomaly(
    order_id: int,
    payload: AnomalyIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("order:execute")),
):
    data = order_service.report_anomaly(
        db, user, order_id, anomaly_type=payload.anomaly_type, description=payload.description, request=request
    )
    return ok(data)
