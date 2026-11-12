"""智能派单与调度 API（B 模块）。

冻结路由（docs/CONTRACTS.md §5 dispatch）：
    GET  /dispatch/{appointment_id}/candidates  前 N 候选 + 四项因子明细
    POST /dispatch/{appointment_id}/assign      派单 {nurse_id} → 生成工单（价格快照）
    POST /dispatch/orders/{order_id}/reassign   改派 {nurse_id, reason}
    POST /dispatch/scan-timeout                 手动触发 30 分钟未接单顺延
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.deps import require
from app.core.response import ok
from app.db.session import get_db
from app.models import User
from app.services import dispatch_service

router = APIRouter(prefix="/dispatch", tags=["智能派单与调度"])


class AssignRequest(BaseModel):
    nurse_id: int = Field(..., description="护理员（staff.id）")


class ReassignRequest(BaseModel):
    nurse_id: int = Field(..., description="新护理员（staff.id）")
    reason: str = Field("", description="改派原因（必填）")


class CoordinateRequest(BaseModel):
    reason: str = Field(..., max_length=255, description="人工协调原因")

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("协调原因必填")
        return value.strip()


@router.post("/scan-timeout", summary="手动触发接单超时顺延扫描")
def scan_accept_timeout(
    db: Session = Depends(get_db),
    user: User = Depends(require("order:dispatch")),
) -> dict:
    data = dispatch_service.scan_accept_timeout(db, operator_id=user.id, user=user)
    return ok(data, "超时扫描完成")


@router.post("/orders/{order_id}/reassign", summary="改派工单（原因必填）")
def reassign_order(
    order_id: int,
    payload: ReassignRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("order:reassign")),
) -> dict:
    data = dispatch_service.reassign_order(
        db, order_id, payload.nurse_id, reason=payload.reason, user=user, request=request
    )
    return ok(data, "改派成功")


@router.get("/{appointment_id}/candidates", summary="候选护理员（四项因子明细）")
def list_candidates(
    appointment_id: int,
    limit: int = Query(3, ge=1, le=20, description="返回前 N 名，默认 3"),
    db: Session = Depends(get_db),
    user: User = Depends(require("order:dispatch")),
) -> dict:
    return ok(dispatch_service.get_candidates(db, appointment_id, user, limit=limit))


@router.post("/{appointment_id}/assign", summary="派单生成工单（价格快照）")
def assign_order(
    appointment_id: int,
    payload: AssignRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("order:dispatch")),
) -> dict:
    data = dispatch_service.assign_order(
        db, appointment_id, payload.nurse_id, user=user, request=request
    )
    return ok(data, "派单成功")


@router.get("/{appointment_id}/recommendation", summary="只读候选排序、权重和筛选解释")
def recommendation(
    appointment_id: int,
    limit: int | None = Query(None, ge=1, le=20),
    db: Session = Depends(get_db),
    user: User = Depends(require("order:dispatch")),
) -> dict:
    return ok(dispatch_service.get_candidate_report(db, appointment_id, user, limit=limit))


@router.post("/{appointment_id}/coordinate", summary="无人可派时记录人工协调")
def coordinate(
    appointment_id: int, payload: CoordinateRequest, request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("order:dispatch")),
) -> dict:
    return ok(dispatch_service.coordinate_dispatch(
        db, appointment_id, reason=payload.reason, user=user, request=request,
    ), "已记录人工协调，预约状态保持不变")
