"""电子表格导出中的文本转义；金额等数值保持数值类型。"""
from __future__ import annotations

from typing import Any


def safe_cell(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value
