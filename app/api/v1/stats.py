"""统计分析与看板 API（E2 模块）。

路由（统一前缀 /api/v1）：
- GET /stats/community/overview                    社区看板总览
- GET /stats/org/overview                          机构看板总览
- GET /stats/street/overview                       街道看板总览
- GET /stats/trend?metric=&from=&to=&granularity=  趋势（同比/环比）
- GET /stats/ranking?type=&period=                 排行榜（前 10）
- GET /stats/distribution?type=                    分布占比
- GET /stats/export?report=&from=&to=              CSV 导出（UTF-8 BOM）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.deps import require
from app.core.response import ok
from app.db.session import get_db
from app.models import User
from app.services import stats_service

router = APIRouter(tags=["统计分析与看板"])


@router.get("/stats/community/overview", summary="社区看板总览")
def community_overview(
    db: Session = Depends(get_db),
    user: User = Depends(require("stats:read")),
):
    """社区看板总览。

    统计口径：
    - service_count：本月（自然月）status=completed 且 completed_at 落在本月的工单数；
    - coverage_rate：本月有 completed 工单的老人数 / 辖区未删除老人总数；
    - pending_orders：待接单 + 已接单 + 服务中的工单数；pending_alerts：待处置预警数；
    - avg/median_alert_response_minutes：预警响应时长（秒折算分钟），无数据返回 null；
    - satisfaction_avg：评价 avg_score 均值（保留 2 位），无数据返回 null。
    """
    return ok(stats_service.community_overview(db, user))


@router.get("/stats/org/overview", summary="机构看板总览")
def org_overview(
    db: Session = Depends(get_db),
    user: User = Depends(require("stats:read")),
):
    """机构看板总览。

    统计口径：
    - nurse_count：状态为 active 的护理员数；
    - avg_orders_per_nurse：本月 completed 工单数 / 护理员数；
    - avg_rating：评价 avg_score 均值（无评价时回退护理员 rating_cached 均值）；
    - income：账单 subsidy / ltc / self_pay 金额与占比（排除 reversed 账单，ratio 合计为 1）。
    """
    return ok(stats_service.org_overview(db, user))


@router.get("/stats/street/overview", summary="街道看板总览")
def street_overview(
    db: Session = Depends(get_db),
    user: User = Depends(require("stats:read")),
):
    """街道看板总览。

    统计口径：
    - communities[]：各社区 completed 工单数、账单补贴金额（排除 reversed）、评价均分；
    - satisfaction_distribution：全街道评价分档占比（ratio 合计为 1）；
    - alert_escalated：已升级预警数；red_alerts：待处置红色预警数；total_subsidy：补贴合计。
    """
    return ok(stats_service.street_overview(db, user))


@router.get("/stats/trend", summary="趋势统计（完单/预警/满意度）")
def trend(
    metric: str = Query("orders", description="orders / alerts / satisfaction"),
    from_: str | None = Query(None, alias="from", description="开始日期 YYYY-MM-DD"),
    to: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    granularity: str = Query("day", description="day / week / month"),
    db: Session = Depends(get_db),
    user: User = Depends(require("stats:read")),
):
    """趋势统计（同比 / 环比）。

    统计口径：orders 按 completed_at 计数、alerts 按 created_at 计数、satisfaction 取 avg_score 均值；
    按 day / ISO 周 / 自然月分桶，缺失桶补 0（满意度空桶为 0.0）；
    last_year 为去年同桶值（无数据 null），mom 为环比上一桶（无基数 null）。
    """
    return ok(stats_service.trend(db, user, metric, from_, to, granularity))


@router.get("/stats/ranking", summary="排行榜（前 10）")
def ranking(
    type: str = Query("nurse", description="nurse / community"),
    period: str | None = Query(None, description="账期 YYYY-MM，缺省当月"),
    db: Session = Depends(get_db),
    user: User = Depends(require("stats:read")),
):
    """排行榜（前 10）。

    统计口径：type=nurse 按本月 completed 工单数降序并附带平均评分；type=community 按完成量降序；
    period 缺省当月。
    """
    return ok(stats_service.ranking(db, user, type, period))


@router.get("/stats/distribution", summary="分布占比")
def distribution(
    type: str = Query("ability", description="ability / satisfaction / service_category"),
    db: Session = Depends(get_db),
    user: User = Depends(require("stats:read")),
):
    """分布占比。

    统计口径：ability=未删除老人按失能等级；satisfaction=评价 avg_score 分档；
    service_category=completed 工单按服务项目类别；ratio 保留 4 位且合计严格为 1（无数据全 0）。
    """
    return ok(stats_service.distribution(db, user, type))


@router.get("/stats/export", summary="导出统计报表（CSV，UTF-8 BOM）")
def export_stats(
    request: Request,
    report: str = Query("orders", description="orders / billing"),
    from_: str | None = Query(None, alias="from", description="开始日期 YYYY-MM-DD"),
    to: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    db: Session = Depends(get_db),
    user: User = Depends(require("stats:export")),
):
    """导出统计报表（CSV，UTF-8 BOM）。

    统计口径：report=orders 导出区间内工单明细；report=billing 导出账单明细；
    from/to 为自然日区间（缺省近 30 天）；导出动作写审计（EXPORT）。
    """
    filename, content = stats_service.export_report(
        db, user, report=report, from_=from_, to=to, request=request
    )
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
