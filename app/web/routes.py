"""(占位) 页面路由。由前端负责人实现 Jinja2 页面。"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
def index() -> str:
    return "<h1>社区智慧养老服务与健康监测平台</h1><p>页面建设中… 接口文档见 /docs</p>"
