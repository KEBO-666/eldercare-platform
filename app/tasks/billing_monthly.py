"""月度结算定时任务：每月 1 日 00:00 生成上月账单（也可手动触发）。

任务函数与触发器解耦：`run_monthly_billing()` 是普通函数，APScheduler 调用、
`POST /api/v1/billing/generate` 手动触发走的是同一套服务逻辑。
本函数使用独立 SessionLocal 会话，任何异常都只返回 `{ok: false}`，不影响调度器。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

logger = logging.getLogger("eldercare.tasks.billing")


def last_month_period(today: date | None = None) -> str:
    """返回相对 `today` 的上一个自然月，格式 YYYY-MM。"""
    base = today or date.today()
    first_day = base.replace(day=1)
    previous = first_day - timedelta(days=1)
    return f"{previous.year:04d}-{previous.month:02d}"


def run_monthly_billing(period: str | None = None) -> dict:
    """生成指定账期（默认上月）的账单，返回统计信息。"""
    target = (period or last_month_period()).strip()
    db = None
    try:
        from app.db.session import SessionLocal
        from app.services import billing_service

        db = SessionLocal()
        result = billing_service.generate_bills(db, period=target)
        bills = result.get("bills") or []
        logger.info("月度结算完成：账期 %s，账单 %s 张，明细 %s 条", target, len(bills), result.get("details_count"))
        return {
            "ok": True,
            "period": target,
            "bills": len(bills),
            "bill_ids": [bill.get("id") for bill in bills],
            "details": int(result.get("details_count") or 0),
            "diffs": int(result.get("diffs_count") or 0),
        }
    except Exception as exc:  # noqa: BLE001 - 定时任务不允许抛异常
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001 - 回滚失败不掩盖原始错误
                logger.exception("月度结算回滚失败")
        logger.exception("月度结算失败：账期 %s，原因 %s", target, exc)
        return {"ok": False, "period": target, "error": str(exc)}
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响结果
                logger.exception("月度结算会话关闭失败")


__all__ = ["last_month_period", "run_monthly_billing"]
