"""费用结算与补贴 API（E1）。

冻结路由（docs/CONTRACTS.md §billing）：
    POST /billing/generate             生成月度账单 {period}
    GET  /billing/bills                账单列表（period/status/分页 + 数据范围）
    GET  /billing/bills/{id}           账单详情（含明细 / 差异 / 缴费）
    POST /billing/bills/{id}/submit    提交复核（bill:generate）
    POST /billing/bills/{id}/confirm   复核通过（bill:review，差异率 >1% 必须带复核说明）
    POST /billing/bills/{id}/pay       登记缴费（三方分账，金额必须一致）
    POST /billing/bills/{id}/reverse   红冲（生成负数新单，原单金额不变）
    GET  /billing/diffs                对账差异清单
    POST /billing/diffs/{id}/handle    处理差异 {status, explanation}
    GET  /billing/bills/{id}/export    导出 Excel
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import require
from app.core.response import ok, paged
from app.db.session import get_db
from app.models import User
from app.services import billing_service

router = APIRouter(prefix="/billing", tags=["费用结算与补贴"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class GenerateRequest(BaseModel):
    period: str = Field(..., description="账期，格式 YYYY-MM", examples=["2026-10"])


class ConfirmRequest(BaseModel):
    review_note: str | None = Field(None, description="复核说明（差异率超阈值时必填）")


class PayRequest(BaseModel):
    government: Decimal | None = Field(None, description="政府补贴实缴金额")
    insurance: Decimal | None = Field(None, description="长护险实缴金额")
    self_pay: Decimal | None = Field(None, description="个人自付实缴金额")
    payer_type: str | None = Field(None, description="单付款方模式：government/insurance/self")
    amount: Decimal | None = Field(None, description="单付款方模式金额")
    remark: str | None = Field(None, max_length=255, description="备注")


class ReverseRequest(BaseModel):
    reason: str = Field("", max_length=200, description="红冲原因")


class DiffHandleRequest(BaseModel):
    status: str = Field("handled", description="处理结果：explained / handled")
    explanation: str = Field("", description="处理说明（必填）")


class DiffCreateRequest(BaseModel):
    bill_id: int | None = Field(None, description="关联账单 id")
    work_order_id: int | None = Field(None, description="关联工单 id")
    elder_id: int | None = Field(None, description="关联老人 id")
    rule_code: str = Field("manual", description="差异类型：duplicate/subsidy_limit/unfinished/duration/manual")
    description: str = Field(..., min_length=1, description="差异说明")
    amount: Decimal = Field(Decimal("0"), description="差异金额")


# ---------------------------------------------------------------- 生成 / 列表
@router.post("/generate", summary="生成月度账单（按工单价格快照 + 补贴分账 + 四类差异）")
def generate_bills(
    payload: GenerateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("bill:generate")),
) -> dict:
    data = billing_service.generate_bills(db, period=payload.period, user=user, request=request)
    return ok(data, f"账期 {payload.period} 账单生成完成")


@router.get("/bills", summary="账单列表")
def list_bills(
    period: str | None = Query(None, description="账期 YYYY-MM"),
    status: str | None = Query(None, description="账单状态"),
    community_org_id: int | None = Query(None, description="社区机构 id"),
    keyword: str | None = Query(None, description="账单号模糊搜索"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require("bill:read")),
) -> dict:
    rows, total = billing_service.list_bills(
        db,
        user,
        period=period,
        status=status,
        community_org_id=community_org_id,
        keyword=keyword,
        page=page,
        size=size,
    )
    return paged(rows, total, page, size)


@router.get("/bills/{bill_id}", summary="账单详情（明细 / 差异 / 缴费）")
def get_bill(
    bill_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require("bill:read")),
) -> dict:
    return ok(billing_service.get_bill_detail(db, bill_id, user))


# ---------------------------------------------------------------- 流转
@router.post("/bills/{bill_id}/submit", summary="提交复核")
def submit_bill(
    bill_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("bill:generate")),
) -> dict:
    data = billing_service.submit_bill(db, bill_id, user=user, request=request)
    return ok(data, "已提交复核")


@router.post("/bills/{bill_id}/confirm", summary="复核通过（差异率 >1% 必须填写复核说明）")
def confirm_bill(
    bill_id: int,
    request: Request,
    payload: ConfirmRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require("bill:review")),
) -> dict:
    data = billing_service.confirm_bill(
        db,
        bill_id,
        review_note=payload.review_note if payload is not None else None,
        user=user,
        request=request,
    )
    return ok(data, "账单已复核通过")


@router.post("/bills/{bill_id}/pay", summary="登记缴费（三方分账金额必须与账单一致）")
def pay_bill(
    bill_id: int,
    payload: PayRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("bill:pay")),
) -> dict:
    data = billing_service.pay_bill(
        db,
        bill_id,
        government=payload.government,
        insurance=payload.insurance,
        self_pay=payload.self_pay,
        payer_type=payload.payer_type,
        amount=payload.amount,
        remark=payload.remark,
        user=user,
        request=request,
    )
    return ok(data, "缴费已登记")


@router.post("/bills/{bill_id}/reverse", summary="红冲（生成负数新单，原单金额不变）")
def reverse_bill(
    bill_id: int,
    request: Request,
    payload: ReverseRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require("bill:refund")),
) -> dict:
    data = billing_service.reverse_bill(
        db,
        bill_id,
        reason=payload.reason if payload is not None else "",
        user=user,
        request=request,
    )
    return ok(data, "已生成红冲账单")


# ---------------------------------------------------------------- 差异
@router.get("/diffs", summary="对账差异清单")
def list_diffs(
    status: str | None = Query(None, description="open/explained/handled"),
    rule_code: str | None = Query(None, description="duplicate/subsidy_limit/unfinished/duration"),
    period: str | None = Query(None, description="账期 YYYY-MM"),
    bill_id: int | None = Query(None, description="账单 id"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(require("bill:read")),
) -> dict:
    rows, total = billing_service.list_diffs(
        db,
        user,
        status=status,
        rule_code=rule_code,
        period=period,
        bill_id=bill_id,
        page=page,
        size=size,
    )
    return paged(rows, total, page, size)


@router.post("/diffs", summary="手工登记对账差异（监管抽查 / 人工补录）")
def create_diff(
    payload: DiffCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("bill:review")),
) -> dict:
    data = billing_service.create_diff(
        db,
        rule_code=payload.rule_code,
        description=payload.description,
        amount=payload.amount,
        bill_id=payload.bill_id,
        work_order_id=payload.work_order_id,
        elder_id=payload.elder_id,
        user=user,
        request=request,
    )
    return ok(data, "差异已登记")


@router.post("/diffs/{diff_id}/handle", summary="处理对账差异")
def handle_diff(
    diff_id: int,
    payload: DiffHandleRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("bill:review")),
) -> dict:
    data = billing_service.handle_diff(
        db,
        diff_id,
        status=payload.status,
        explanation=payload.explanation,
        user=user,
        request=request,
    )
    return ok(data, "差异已处理")


# ---------------------------------------------------------------- 导出
@router.get("/bills/{bill_id}/export", summary="导出账单 Excel")
def export_bill(
    bill_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require("bill:read")),
):
    content, filename = billing_service.export_bill_xlsx(db, bill_id, user=user, request=request)
    return StreamingResponse(
        iter([content]),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
