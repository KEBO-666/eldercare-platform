"""预警超时扫描定时任务：独立会话执行静默检测与超时升级，异常只记日志不抛出。"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("eldercare.tasks.alert")


def run_alert_timeout_scan() -> dict[str, Any]:
    """扫描静默老人与超时未处置预警，返回统计信息。

    - 使用 app.db.session.SessionLocal 独立会话，避免与请求会话互相影响；
    - 任何异常都回滚并返回带 error 的统计，保证调度器不中断。
    """
    db = None
    try:
        from app.db.session import SessionLocal  # 延迟导入，便于测试替换
        from app.services.alert_service import scan_alerts

        db = SessionLocal()
        result = scan_alerts(db)
        db.commit()
        logger.info("预警超时扫描完成: %s", result)
        return result
    except Exception as exc:  # noqa: BLE001 - 定时任务不能抛出
        logger.exception("预警超时扫描失败")
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                logger.debug("预警扫描回滚失败", exc_info=True)
        return {"elders_scanned": 0, "silence_created": 0, "escalated": 0, "error": str(exc)}
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                logger.debug("预警扫描会话关闭失败", exc_info=True)
