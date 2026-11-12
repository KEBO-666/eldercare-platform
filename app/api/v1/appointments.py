"""预约受理 API（B 模块）。

冻结路由（docs/CONTRACTS.md §5 appointments）：
    POST /appointments                  提交预约（三重校验）
    GET  /appointments                  列表（status/elder_id/日期范围/分页 + 数据范围）
    GET  /appointments/{id}             详情（含状态日志）
    POST /appointments/{id}/accept      受理
    POST /appointments/{id}/to-dispatch 转待派单
    POST /appointments/{id}/cancel      取消 {reason}（2 小时计费规则）
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import require
from app.core.response import ok, paged
from app.db.session import get_db
from app.models import User
from app.services import appointment_service

router = APIRouter(prefix="/appointments", tags=["预约与受理"])


class AppointmentCreate(BaseModel):
    service_item_id: int = Field(..., description="服务项目 id")
    desired_start: datetime = Field(..., description="期望上门时间")
    elder_id: int | None = Field(None, description="老人 id（老人/家属账号可省略，默认本人绑定档案）")
    address: str | None = Field(None, max_length=255, description="服务地址（可省略，默认复用老人地址）")
    remark: str | None = None
    cycle_rule: str | None = Field(None, max_length=32, description="weekly：一次提交连续 4 周的同一时段预约")


class CancelRequest(BaseModel):
    reason: str = Field("", description="取消原因（必填）")


@router.post("", summary="提交预约（三重校验）")
def create_appointment(
    payload: AppointmentCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("appointment:create")),
) -> dict:
    data = appointment_service.create_appointment(
        db,
        service_item_id=payload.service_item_id,
        desired_start=payload.desired_start,
        elder_id=payload.elder_id,
        address=payload.address,
        remark=payload.remark,
        cycle_rule=payload.cycle_rule,
        user=user,
        request=request,
    )
    return ok(data, "预约提交成功")


@router.get("", summary="预约列表")
def list_appointments(
    status: str | None = Query(None, description="预约状态过滤"),
    elder_id: int | None = Query(None, description="老人 id 过滤"),
    date_from: datetime | None = Query(None, description="期望上门时间起"),
    date_to: datetime | None = Query(None, description="期望上门时间止"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require("appointment:read")),
) -> dict:
    rows, total = appointment_service.list_appointments(
        db,
        user,
        status=status,
        elder_id=elder_id,
        date_from=date_from,
        date_to=date_to,
        page=page,
        size=size,
    )
    return paged(rows, total, page, size)


@router.get("/{appointment_id}", summary="预约详情（含状态日志）")
def get_appointment(
    appointment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("appointment:read")),
) -> dict:
    return ok(appointment_service.get_appointment_detail(db, appointment_id, user))


@router.post("/{appointment_id}/accept", summary="受理预约")
def accept_appointment(
    appointment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("appointment:accept")),
) -> dict:
    data = appointment_service.accept_appointment(db, appointment_id, user=user, request=request)
    return ok(data, "已受理")


@router.post("/{appointment_id}/to-dispatch", summary="转待派单")
def to_dispatch_appointment(
    appointment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("appointment:accept")),
) -> dict:
    data = appointment_service.to_dispatch_appointment(db, appointment_id, user=user, request=request)
    return ok(data, "已转待派单")


@router.post("/{appointment_id}/cancel", summary="取消预约（距服务开始 <2 小时计费）")
def cancel_appointment(
    appointment_id: int,
    payload: CancelRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("appointment:cancel")),
) -> dict:
    data = appointment_service.cancel_appointment(
        db, appointment_id, reason=payload.reason, user=user, request=request
    )
    return ok(data, "已取消")


@router.post("/{appointment_id}/no-show", summary="登记爽约并释放护理员时段")
def no_show_appointment(
    appointment_id: int,
    payload: CancelRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("appointment:accept")),
) -> dict:
    return ok(appointment_service.no_show_appointment(
        db, appointment_id, user=user, reason=payload.reason, request=request,
    ), "已登记爽约")
