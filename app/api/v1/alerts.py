"""分级预警与处置 API（D2 模块）。

路由（统一前缀 /api/v1）：
- GET  /alerts                     预警列表（level/status/elder_id，红>黄>蓝再按时间倒序）
- GET  /alerts/stats               响应/处置中位数、误报率、升级数
- GET  /alerts/rules               规则列表（含默认规则）
- POST /alerts/rules               新建规则
- PUT  /alerts/rules/{rule_id}     编辑规则
- POST /alerts/scan                手动触发静默检测 + 超时升级
- GET  /alerts/{alert_id}          详情 + 处置流水
- POST /alerts/{alert_id}/ack      确认响应（记录 response_seconds）
- POST /alerts/{alert_id}/handle   闭环处置（resolved / false_alarm / referred）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import require
from app.core.response import ok, paged
from app.db.session import get_db
from app.models import User
from app.services import alert_service

router = APIRouter(tags=["分级预警与处置"])


class HandleIn(BaseModel):
    resolution_type: str = Field(description="resolved / false_alarm / referred")
    measure: str | None = Field(default=None, description="处置措施")
    note: str | None = Field(default=None, description="处置备注")


class RuleIn(BaseModel):
    code: str = Field(description="规则编码，全局唯一，如 SBP_HIGH")
    name: str
    rule_type: str = Field(default="threshold", description="threshold / consecutive / silence")
    metric_code: str | None = None
    operator: str | None = Field(default=None, description=">= / <= / > / <")
    threshold: float | None = None
    threshold2: float | None = None
    window_size: int | None = None
    silence_hours: int | None = None
    level: str = "yellow"
    cooldown_minutes: int = 30
    escalate_minutes: int = 30
    push_minutes: int = 5
    enabled: bool = True
    description: str | None = None


class RuleUpdateIn(BaseModel):
    code: str | None = None
    name: str | None = None
    rule_type: str | None = None
    metric_code: str | None = None
    operator: str | None = None
    threshold: float | None = None
    threshold2: float | None = None
    window_size: int | None = None
    silence_hours: int | None = None
    level: str | None = None
    cooldown_minutes: int | None = None
    escalate_minutes: int | None = None
    push_minutes: int | None = None
    enabled: bool | None = None
    description: str | None = None


# ---------------------------------------------------------------- 预警
@router.get("/alerts", summary="预警列表（红>黄>蓝，再按时间倒序）")
def list_alerts(
    level: str | None = Query(default=None, description="red / yellow / blue"),
    status: str | None = Query(default=None, description="pending / resolved / false_alarm / referred"),
    elder_id: int | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require("alert:read")),
):
    items, total = alert_service.list_alerts(
        db, user, level=level, status=status, elder_id=elder_id, page=page, size=size
    )
    return paged(items, total, page, size)


@router.get("/alerts/stats", summary="预警统计（响应/处置中位数、误报率、升级数）")
def alert_stats(
    db: Session = Depends(get_db),
    user: User = Depends(require("alert:read")),
):
    return ok(alert_service.alert_stats(db, user))


# ---------------------------------------------------------------- 规则（静态路由需先于 /alerts/{id} 声明）
@router.get("/alerts/rules", summary="预警规则列表")
def list_rules(
    db: Session = Depends(get_db),
    user: User = Depends(require("alert:read")),
):
    return ok(alert_service.list_rules(db))


@router.post("/alerts/rules", summary="新建预警规则")
def create_rule(
    payload: RuleIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("alert:rule")),
):
    return ok(alert_service.create_rule(db, user, payload.model_dump(), request=request))


@router.put("/alerts/rules/{rule_id}", summary="编辑预警规则")
def update_rule(
    rule_id: int,
    payload: RuleUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("alert:rule")),
):
    return ok(
        alert_service.update_rule(
            db, user, rule_id, payload.model_dump(exclude_unset=True), request=request
        )
    )


@router.post("/alerts/scan", summary="手动触发静默检测与超时升级")
def scan_alerts(
    db: Session = Depends(get_db),
    user: User = Depends(require("alert:rule")),
):
    return ok(alert_service.scan_alerts(db))


@router.get("/alerts/{alert_id}", summary="预警详情（含处置流水）")
def get_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("alert:read")),
):
    return ok(alert_service.get_alert(db, user, alert_id, include_actions=True))


@router.post("/alerts/{alert_id}/ack", summary="确认响应（记录响应时长）")
def ack_alert(
    alert_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("alert:handle")),
):
    return ok(alert_service.ack_alert(db, user, alert_id, request=request))


@router.post("/alerts/{alert_id}/handle", summary="预警闭环处置")
def handle_alert(
    alert_id: int,
    payload: HandleIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("alert:handle")),
):
    return ok(
        alert_service.handle_alert(
            db,
            user,
            alert_id,
            resolution_type=payload.resolution_type,
            measure=payload.measure,
            note=payload.note,
            request=request,
        )
    )
