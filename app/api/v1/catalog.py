"""服务项目与价格版本 API（B 模块）。

冻结路由（docs/CONTRACTS.md §5 catalog）：
    GET    /catalog/items              项目列表（category/status/keyword/分页）
    POST   /catalog/items              新建项目（自动创建 v1 价格版本）
    PUT    /catalog/items/{id}         编辑（不含价格）
    POST   /catalog/items/{id}/status  上下架 {status: on/off}
    GET    /catalog/items/{id}/prices  价格版本列表
    POST   /catalog/items/{id}/prices  新增价格版本 {unit_price, effective_from}
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import require
from app.core.response import ok, paged
from app.db.session import get_db
from app.models import User
from app.services import catalog_service

router = APIRouter(prefix="/catalog", tags=["服务目录"])


class CatalogItemCreate(BaseModel):
    code: str = Field(..., max_length=32, description="项目编码")
    name: str = Field(..., max_length=64, description="项目名称")
    category: str = Field("other", max_length=32, description="分类")
    pricing_mode: str = Field("per_time", description="计价方式 per_time / per_hour")
    standard_duration_min: int = Field(60, ge=1, description="标准时长（分钟）")
    required_skills: list[str] = Field(default_factory=list, description="所需技能")
    applicable_levels: list[str] = Field(default_factory=list, description="适用能力等级，空表示全部")
    is_subsidized: bool = True
    subsidy_ratio: float = Field(0.0, ge=0, le=1)
    status: str = Field("on", description="on / off")
    description: str | None = None
    unit_price: Decimal = Field(Decimal("0.00"), ge=0, description="初始单价（自动创建 v1 价格版本）")
    effective_from: datetime | None = Field(None, description="初始价格生效时间，默认当前时间")
    remark: str | None = None


class CatalogItemUpdate(BaseModel):
    name: str | None = Field(None, max_length=64)
    category: str | None = Field(None, max_length=32)
    pricing_mode: str | None = None
    standard_duration_min: int | None = Field(None, ge=1)
    required_skills: list[str] | None = None
    applicable_levels: list[str] | None = None
    is_subsidized: bool | None = None
    subsidy_ratio: float | None = Field(None, ge=0, le=1)
    description: str | None = None


class ItemStatusUpdate(BaseModel):
    status: str = Field(..., description="on 上架 / off 下架")


class PriceVersionCreate(BaseModel):
    unit_price: Decimal = Field(..., ge=0, description="新单价")
    effective_from: datetime | None = Field(None, description="生效时间，默认当前时间")
    remark: str | None = None


@router.get("/items", summary="服务项目列表")
def list_items(
    category: str | None = Query(None, description="分类过滤"),
    status: str | None = Query(None, description="状态过滤 on/off"),
    keyword: str | None = Query(None, description="名称/编码模糊搜索"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require("catalog:read")),
) -> dict:
    items = catalog_service.list_items(db, category=category, status=status, keyword=keyword)
    total = len(items)
    offset = (page - 1) * size
    rows = [catalog_service.item_to_dict(db, item) for item in items[offset: offset + size]]
    return paged(rows, total, page, size)


@router.post("/items", summary="新建服务项目（自动创建 v1 价格版本）")
def create_item(
    payload: CatalogItemCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("catalog:write")),
) -> dict:
    data = catalog_service.create_item(db, payload.model_dump(), user=user, request=request)
    return ok(data, "创建成功")


@router.put("/items/{item_id}", summary="编辑服务项目（价格请走价格版本接口）")
def update_item(
    item_id: int,
    payload: CatalogItemUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("catalog:write")),
) -> dict:
    data = catalog_service.update_item(
        db, item_id, payload.model_dump(exclude_unset=True), user=user, request=request
    )
    return ok(data, "更新成功")


@router.post("/items/{item_id}/status", summary="服务项目上下架")
def update_item_status(
    item_id: int,
    payload: ItemStatusUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("catalog:write")),
) -> dict:
    data = catalog_service.set_item_status(db, item_id, payload.status, user=user, request=request)
    return ok(data, "状态已更新")


@router.get("/items/{item_id}/prices", summary="价格版本历史")
def list_prices(
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("catalog:read")),
) -> dict:
    return ok(catalog_service.list_price_versions(db, item_id))


@router.post("/items/{item_id}/prices", summary="新增价格版本（关闭上一版本生效区间）")
def add_price_version(
    item_id: int,
    payload: PriceVersionCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("catalog:write")),
) -> dict:
    data = catalog_service.add_price_version(
        db,
        item_id,
        unit_price=payload.unit_price,
        effective_from=payload.effective_from,
        remark=payload.remark,
        user=user,
        request=request,
    )
    return ok(data, "价格版本已更新")
