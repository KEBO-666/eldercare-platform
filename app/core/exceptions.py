"""统一业务异常与错误码。

约定：
- 业务异常统一抛 BizError 子类，由 main.py 注册的 handler 转成
  {"code": "<错误码>", "message": "<提示>", "data": null} + 对应 HTTP 状态码。
- 错误码使用字符串，便于前端与测试断言。
"""
from __future__ import annotations

from typing import Any


class BizError(Exception):
    code: str = "BIZ_ERROR"
    http_status: int = 400

    def __init__(self, message: str = "", *, code: str | None = None,
                 http_status: int | None = None, data: Any = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status
        self.data = data


class BadRequest(BizError):
    code = "BAD_REQUEST"
    http_status = 400


class Unauthorized(BizError):
    code = "UNAUTHORIZED"
    http_status = 401


class Forbidden(BizError):
    code = "FORBIDDEN"
    http_status = 403


class NotFound(BizError):
    code = "NOT_FOUND"
    http_status = 404


class Conflict(BizError):
    code = "CONFLICT"
    http_status = 409


class RuleViolation(BadRequest):
    """业务规则不满足（如重复预约、距离超限、补贴超限）。"""
    code = "RULE_VIOLATION"


class NeedsManual(BizError):
    """校验不通过需转人工受理。"""
    code = "NEEDS_MANUAL"
    http_status = 422
