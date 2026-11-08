"""应用入口：uvicorn app.main:app --reload"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import BASE_DIR, settings
from app.core.exceptions import BizError
from app.db.session import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("eldercare")


def _bootstrap_dirs() -> None:
    settings.upload_path.mkdir(parents=True, exist_ok=True)
    settings.backup_path.mkdir(parents=True, exist_ok=True)
    settings.abs_path(settings.data_dir).mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap_dirs()
    if getattr(app.state, "startup_db", True):
        init_db()
        logger.info("数据库初始化完成：%s", settings.db_url)
    if getattr(app.state, "enable_scheduler", False):
        try:
            from app.tasks.scheduler import start_scheduler

            start_scheduler()
            app.state.scheduler_started = True
            logger.info("定时任务调度器已启动")
        except Exception:  # noqa: BLE001 - 调度器异常不阻断服务
            logger.exception("调度器启动失败（不影响主服务）")
    yield
    if getattr(app.state, "scheduler_started", False):
        from app.tasks.scheduler import shutdown_scheduler

        shutdown_scheduler()


def create_app(
    *,
    startup_db: bool = True,
    enable_scheduler: bool | None = None,
    mount_static: bool = True,
) -> FastAPI:
    application = FastAPI(
        title=settings.app_name,
        version=settings.version,
        lifespan=lifespan,
        description="社区智慧养老服务与健康监测平台 · 决赛作品",
    )
    application.state.startup_db = startup_db
    application.state.enable_scheduler = (
        settings.enable_scheduler if enable_scheduler is None else enable_scheduler
    )

    if mount_static:
        static_dir = BASE_DIR / "app" / "static"
        static_dir.mkdir(parents=True, exist_ok=True)
        application.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @application.exception_handler(BizError)
    async def _biz_error(_: Request, exc: BizError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": exc.code, "message": exc.message, "data": exc.data},
        )

    @application.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "参数校验失败",
                "data": jsonable_encoder(exc.errors()),
            },
        )

    @application.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"code": "INTERNAL_ERROR", "message": "服务内部错误", "data": None},
        )

    # 延迟导入，避免循环依赖
    from app.api.v1.router import api_router

    application.include_router(api_router, prefix="/api/v1")

    @application.get("/health", tags=["系统"], summary="健康检查")
    def health() -> dict:
        return {
            "code": 0,
            "message": "ok",
            "data": {"status": "healthy", "app": settings.app_name, "version": settings.version},
        }

    from app.web.routes import router as web_router

    application.include_router(web_router)

    return application


app = create_app()
