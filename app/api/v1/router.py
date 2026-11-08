"""API v1 路由汇总（模块由各负责人实现，本文件为冻结的注册中心）。"""
from fastapi import APIRouter

from app.api.v1 import (
    admin,
    alerts,
    appointments,
    auth,
    billing,
    catalog,
    dispatch,
    elders,
    health,
    orders,
    quality,
    stats,
)

api_router = APIRouter()

for _module in (
    auth,
    elders,
    catalog,
    appointments,
    dispatch,
    orders,
    health,
    alerts,
    quality,
    billing,
    stats,
    admin,
):
    api_router.include_router(_module.router)
