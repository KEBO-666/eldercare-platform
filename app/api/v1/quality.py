"""评价、回访、投诉与绩效 API（C 模块）。

路由（统一前缀 /api/v1）：
- POST /quality/evaluations                    提交评价（4 项 1-5 星）
- GET  /quality/evaluations                    评价列表
- GET  /quality/callbacks                      回访待办（含 overdue 标记）
- POST /quality/callbacks/{id}/finish          完成回访（可转投诉）
- POST /quality/complaints                     投诉登记
- GET  /quality/complaints                     投诉列表
- POST /quality/complaints/{id}/assign         指派处置人
- POST /quality/complaints/{id}/conclude       复核结论（成立可退费/换人/扣分）
- GET  /quality/performance                    月度绩效榜 ?period=YYYY-MM
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import require
from app.core.response import ok, paged
from app.db.session import get_db
from app.models import User
from app.services import quality_service

router = APIRouter(tags=["评价与投诉"])


class EvaluationIn(BaseModel):
    work_order_id: int
    punctuality: int
    attitude: int
    professionalism: int
    effect: int
    comment: str | None = None


class CallbackFinishIn(BaseModel):
    result: str = Field(description="improve（已改善）/ to_complaint（转投诉）")
    note: str | None = None


class ComplaintIn(BaseModel):
    elder_id: int
    work_order_id: int | None = None
    nurse_id: int | None = None
    content: str


class ComplaintAssignIn(BaseModel):
    handler_id: int


class ComplaintConcludeIn(BaseModel):
    conclusion: str
    is_established: bool = False
    resolution: str = Field(default="none", description="refund/change_nurse/deduct/none")


# ---------------------------------------------------------------- 评价
@router.post("/quality/evaluations", summary="提交评价（4 项 1-5 星 + 自动回访）")
def create_evaluation(
    payload: EvaluationIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("quality:evaluate")),
):
    data = quality_service.create_evaluation(
        db,
        user,
        work_order_id=payload.work_order_id,
        punctuality=payload.punctuality,
        attitude=payload.attitude,
        professionalism=payload.professionalism,
        effect=payload.effect,
        comment=payload.comment,
        request=request,
    )
    return ok(data)


@router.get("/quality/evaluations", summary="评价列表")
def list_evaluations(
    nurse_id: int | None = Query(default=None),
    work_order_id: int | None = Query(default=None),
    elder_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require("quality:read")),
):
    items, total = quality_service.list_evaluations(
        db, user, nurse_id=nurse_id, work_order_id=work_order_id, elder_id=elder_id, page=page, size=size
    )
    return paged(items, total, page, size)


# ---------------------------------------------------------------- 回访
@router.get("/quality/callbacks", summary="回访待办（含超时标记）")
def list_callbacks(
    status: str | None = Query(default="pending", description="pending/done/all"),
    elder_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require("quality:read")),
):
    items, total, overdue_count = quality_service.list_callbacks(
        db, user, status=status, elder_id=elder_id, page=page, size=size
    )
    return ok(
        {
            "items": items,
            "total": total,
            "page": page,
            "size": size,
            "pages": (total + size - 1) // size if size else 0,
            "overdue_count": overdue_count,
        }
    )


@router.post("/quality/callbacks/{callback_id}/finish", summary="完成回访（可转投诉）")
def finish_callback(
    callback_id: int,
    payload: CallbackFinishIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("quality:handle")),
):
    data = quality_service.finish_callback(
        db, user, callback_id, result=payload.result, note=payload.note, request=request
    )
    return ok(data)


# ---------------------------------------------------------------- 投诉
@router.post("/quality/complaints", summary="投诉登记")
def create_complaint(
    payload: ComplaintIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("quality:read")),
):
    data = quality_service.create_complaint(
        db,
        user,
        elder_id=payload.elder_id,
        content=payload.content,
        work_order_id=payload.work_order_id,
        nurse_id=payload.nurse_id,
        request=request,
    )
    return ok(data)


@router.get("/quality/complaints", summary="投诉列表")
def list_complaints(
    status: str | None = Query(default=None),
    nurse_id: int | None = Query(default=None),
    elder_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require("quality:read")),
):
    items, total = quality_service.list_complaints(
        db, user, status=status, nurse_id=nurse_id, elder_id=elder_id, page=page, size=size
    )
    return paged(items, total, page, size)


@router.post("/quality/complaints/{complaint_id}/assign", summary="指派投诉处置人")
def assign_complaint(
    complaint_id: int,
    payload: ComplaintAssignIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("quality:handle")),
):
    data = quality_service.assign_complaint(
        db, user, complaint_id, handler_id=payload.handler_id, request=request
    )
    return ok(data)


@router.post("/quality/complaints/{complaint_id}/conclude", summary="投诉结论（成立可退费/换人/扣分）")
def conclude_complaint(
    complaint_id: int,
    payload: ComplaintConcludeIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("quality:handle")),
):
    data = quality_service.conclude_complaint(
        db,
        user,
        complaint_id,
        conclusion=payload.conclusion,
        is_established=payload.is_established,
        resolution=payload.resolution,
        request=request,
    )
    return ok(data)


# ---------------------------------------------------------------- 绩效
@router.get("/quality/performance", summary="月度绩效榜（平均分/准时率/异常率/完成量）")
def performance(
    period: str | None = Query(default=None, description="YYYY-MM，默认当月"),
    db: Session = Depends(get_db),
    user: User = Depends(require("quality:read")),
):
    return ok(quality_service.performance_board(db, user, period))
