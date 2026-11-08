"""认证与登录 API：登录（JWT + HttpOnly Cookie）、登出、当前用户。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.response import ok
from app.db.session import get_db
from app.models import User
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["认证与登录"])

COOKIE_NAME = "access_token"


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login", summary="登录")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = auth_service.login(db, payload.username, payload.password, request)
    # 供页面（Jinja2）使用的 HttpOnly Cookie；接口仍以 Bearer Token 为主
    response.set_cookie(
        key=COOKIE_NAME,
        value=result["token"],
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
        httponly=True,
        samesite="lax",
    )
    return ok(result)


@router.post("/logout", summary="退出登录")
def logout(response: Response, _user: User = Depends(get_current_user)) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return ok({"message": "已退出登录"})


@router.get("/me", summary="当前登录用户")
def me(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return ok(auth_service.build_user_payload(user))
