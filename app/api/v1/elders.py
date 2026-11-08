"""老人档案与能力评估 API（A2）。

路由层只做参数解析、权限/数据范围依赖注入与统一响应封装，
全部业务规则在 ``app.services.archive_service`` 中实现。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.deps import require
from app.core.response import ok, paged
from app.db.session import get_db
from app.models import User
from app.schemas.elder import (
    AssessmentCreate,
    DiseaseCreate,
    ElderCreate,
    ElderUpdate,
    GrantCreate,
    MedicationCreate,
    TagCreate,
)
from app.services import archive_service

router = APIRouter(prefix="/elders", tags=["老人档案与能力评估"])


# ---------------- 列表 / 新增 ----------------
@router.get("", summary="老人档案列表（脱敏，支持数据范围）")
def list_elders(
    keyword: str | None = Query(None, description="姓名/编码/身份证摘要/手机摘要"),
    ability_level: str | None = Query(None, description="intact/mild/moderate/severe"),
    tag: str | None = Query(None, description="标签"),
    grid_id: int | None = Query(None, description="网格组织 id"),
    community_id: int | None = Query(None, description="社区组织 id"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:read")),
):
    items, total = archive_service.list_elders(
        db,
        user,
        keyword=keyword,
        ability_level=ability_level,
        tag=tag,
        grid_id=grid_id,
        community_id=community_id,
        page=page,
        size=size,
    )
    return paged(items, total, page, size)


@router.post("", summary="新增老人档案（敏感字段加密 + 变更日志 + 审计）")
def create_elder(
    payload: ElderCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:write")),
):
    elder = archive_service.create_elder(db, user, payload, request=request)
    return ok(archive_service.detail_view(db, user, elder.id))


# ---------------- 批量导入 / 导出（必须声明在 /{elder_id} 之前） ----------------
@router.post("/import", summary="批量导入老人档案（逐条校验，返回失败明细）")
def import_elders(
    request: Request,
    payload: list[dict[str, Any]] = Body(..., description="老人档案 JSON 数组"),
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:write")),
):
    result = archive_service.import_elders(db, user, payload, request=request)
    return ok(result)


@router.get("/export", summary="导出老人档案 CSV（UTF-8 BOM、字段脱敏）")
def export_elders(
    request: Request,
    keyword: str | None = Query(None),
    ability_level: str | None = Query(None),
    tag: str | None = Query(None),
    grid_id: int | None = Query(None),
    community_id: int | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:export")),
):
    content, filename = archive_service.export_csv(
        db,
        user,
        keyword=keyword,
        ability_level=ability_level,
        tag=tag,
        grid_id=grid_id,
        community_id=community_id,
        request=request,
    )
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------- 详情 / 编辑 / 删除 ----------------
@router.get("/{elder_id}", summary="老人档案详情（脱敏）")
def get_elder(
    elder_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:read")),
):
    return ok(archive_service.detail_view(db, user, elder_id))


@router.put("/{elder_id}", summary="编辑老人档案（逐字段 diff + 变更日志）")
def update_elder(
    elder_id: int,
    payload: ElderUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:write")),
):
    elder = archive_service.update_elder(db, user, elder_id, payload, request=request)
    return ok(archive_service.detail_view(db, user, elder.id))


@router.delete("/{elder_id}", summary="逻辑删除老人档案")
def delete_elder(
    elder_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:delete")),
):
    elder = archive_service.delete_elder(db, user, elder_id, request=request)
    deleted_at = elder.deleted_at.isoformat() if elder.deleted_at else None
    return ok({"id": elder.id, "deleted": True, "deleted_at": deleted_at})


# ---------------- 变更日志 ----------------
@router.get("/{elder_id}/changes", summary="档案变更日志（时间倒序）")
def list_changes(
    elder_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:read")),
):
    return ok(archive_service.list_changes(db, user, elder_id))


# ---------------- 二次授权与敏感字段 ----------------
@router.post("/{elder_id}/grants", summary="申请二次授权（10 分钟有效）")
def create_grant(
    elder_id: int,
    payload: GrantCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:grant")),
):
    return ok(archive_service.create_grant(db, user, elder_id, payload, request=request))


@router.get("/{elder_id}/sensitive", summary="凭二次授权查看敏感字段明文")
def read_sensitive(
    elder_id: int,
    request: Request,
    grant_id: int | None = Query(None, description="二次授权 id"),
    fields: str | None = Query(None, description="逗号分隔的字段名，缺省返回全部已授权字段"),
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:grant")),
):
    wanted = [item.strip() for item in fields.split(",") if item.strip()] if fields else None
    return ok(
        archive_service.read_sensitive(
            db, user, elder_id, grant_id, fields=wanted, request=request
        )
    )


# ---------------- 标签 ----------------
@router.get("/{elder_id}/tags", summary="标签列表")
def list_tags(
    elder_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:read")),
):
    return ok(archive_service.list_tags(db, user, elder_id))


@router.post("/{elder_id}/tags", summary="新增标签（自动去重）")
def add_tags(
    elder_id: int,
    payload: TagCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:write")),
):
    return ok(archive_service.add_tags(db, user, elder_id, payload.normalized(), request=request))


# ---------------- 慢病 ----------------
@router.get("/{elder_id}/diseases", summary="慢病列表")
def list_diseases(
    elder_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:read")),
):
    return ok(archive_service.list_diseases(db, user, elder_id))


@router.post("/{elder_id}/diseases", summary="新增慢病")
def add_disease(
    elder_id: int,
    payload: DiseaseCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:write")),
):
    return ok(archive_service.add_disease(db, user, elder_id, payload))


# ---------------- 用药 ----------------
@router.get("/{elder_id}/medications", summary="用药列表")
def list_medications(
    elder_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:read")),
):
    return ok(archive_service.list_medications(db, user, elder_id))


@router.post("/{elder_id}/medications", summary="新增用药")
def add_medication(
    elder_id: int,
    payload: MedicationCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:write")),
):
    return ok(archive_service.add_medication(db, user, elder_id, payload))


# ---------------- 能力评估 ----------------
@router.get("/{elder_id}/assessments", summary="能力评估记录")
def list_assessments(
    elder_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:read")),
):
    return ok(archive_service.list_assessments(db, user, elder_id))


@router.post("/{elder_id}/assessments", summary="新增能力评估并同步老人能力等级")
def add_assessment(
    elder_id: int,
    payload: AssessmentCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("elder:write")),
):
    return ok(archive_service.add_assessment(db, user, elder_id, payload, request=request))
