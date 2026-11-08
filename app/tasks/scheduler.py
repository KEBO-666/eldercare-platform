"""定时任务调度：预警超时扫描、月度结算、每日备份。"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings

logger = logging.getLogger("eldercare.tasks")

_scheduler: BackgroundScheduler | None = None


def _alert_timeout_job() -> None:
    from app.tasks.alert_timeout import run_alert_timeout_scan

    logger.info("执行预警超时扫描: %s", run_alert_timeout_scan())


def _billing_job() -> None:
    from app.tasks.billing_monthly import run_monthly_billing

    logger.info("执行月度结算: %s", run_monthly_billing())


def _backup_job() -> None:
    from app.tasks.backup_daily import run_daily_backup

    logger.info("执行每日备份: %s", run_daily_backup())


def _dispatch_timeout_job() -> None:
    """30 分钟无人接单的工单自动顺延（回退预约到待派单）。"""
    from app.db.session import SessionLocal
    from app.services.dispatch_service import scan_accept_timeout

    db = SessionLocal()
    try:
        logger.info("派单超时扫描: %s", scan_accept_timeout(db))
    except Exception:  # noqa: BLE001
        logger.exception("派单超时扫描失败")
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    sched = BackgroundScheduler(timezone=settings.scheduler_timezone)
    sched.add_job(_alert_timeout_job, CronTrigger(minute="*/5"), id="alert_timeout", replace_existing=True)
    sched.add_job(_dispatch_timeout_job, CronTrigger(minute="1/5"), id="dispatch_timeout", replace_existing=True)
    sched.add_job(_billing_job, CronTrigger(day=1, hour=0, minute=0), id="billing_monthly", replace_existing=True)
    sched.add_job(_backup_job, CronTrigger(hour=2, minute=0), id="backup_daily", replace_existing=True)
    sched.start()
    _scheduler = sched
    return sched


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler
