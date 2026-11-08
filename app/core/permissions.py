"""RBAC 权限点定义与默认角色授权矩阵（全系统唯一事实来源）。

约定：
- 权限点格式 `模块:动作`，接口用 `deps.require("elder:read")` 校验；
- 超级管理员在 deps 中直接放行（无需在表中配置）；
- 种子脚本与测试夹具均基于本文件生成数据。
"""
from __future__ import annotations

from app.core.enums import DataScope, RoleCode

# (code, 名称, 模块)
PERMISSIONS: list[tuple[str, str, str]] = [
    ("elder:read", "查看老人档案", "elder"),
    ("elder:write", "新增/编辑老人档案", "elder"),
    ("elder:delete", "删除老人档案", "elder"),
    ("elder:export", "导出老人档案", "elder"),
    ("elder:grant", "二次授权查看明文", "elder"),
    ("catalog:read", "查看服务目录", "catalog"),
    ("catalog:write", "维护服务项目与价格", "catalog"),
    ("appointment:read", "查看预约单", "appointment"),
    ("appointment:create", "提交预约", "appointment"),
    ("appointment:accept", "受理预约", "appointment"),
    ("appointment:cancel", "取消预约", "appointment"),
    ("order:read", "查看工单", "order"),
    ("order:execute", "签到签退与填报", "order"),
    ("order:dispatch", "派单", "order"),
    ("order:reassign", "改派", "order"),
    ("health:read", "查看健康数据", "health"),
    ("health:write", "录入健康数据", "health"),
    ("alert:read", "查看预警", "alert"),
    ("alert:handle", "处置预警", "alert"),
    ("alert:rule", "维护预警规则", "alert"),
    ("quality:read", "查看评价与投诉", "quality"),
    ("quality:evaluate", "提交评价", "quality"),
    ("quality:handle", "回访与投诉处理", "quality"),
    ("bill:read", "查看账单", "bill"),
    ("bill:generate", "生成账单", "bill"),
    ("bill:review", "复核账单", "bill"),
    ("bill:pay", "登记缴费", "bill"),
    ("bill:refund", "退费与红冲", "bill"),
    ("stats:read", "查看统计看板", "stats"),
    ("stats:export", "导出统计报表", "stats"),
    ("admin:user", "用户管理", "admin"),
    ("admin:org", "组织管理", "admin"),
    ("admin:backup", "备份与恢复", "admin"),
    ("admin:audit", "审计日志", "admin"),
    ("admin:config", "系统配置", "admin"),
]

ALL_PERMISSION_CODES = [p[0] for p in PERMISSIONS]

# 默认角色 → 权限点
ROLE_PERMISSIONS: dict[str, list[str]] = {
    RoleCode.ELDER: [
        "elder:read",
        "appointment:read",
        "appointment:create",
        "appointment:cancel",
        "order:read",
        "alert:read",
        "quality:evaluate",
        "bill:read",
    ],
    RoleCode.NURSE: [
        "elder:read",
        "order:read",
        "order:execute",
        "health:read",
        "health:write",
        "alert:read",
    ],
    RoleCode.GRID: [
        "elder:read",
        "elder:write",
        "catalog:read",
        "appointment:read",
        "appointment:accept",
        "appointment:cancel",
        "order:read",
        "order:dispatch",
        "order:reassign",
        "health:read",
        "alert:read",
        "alert:handle",
        "quality:read",
        "quality:handle",
        "bill:read",
        "stats:read",
    ],
    RoleCode.ORG_ADMIN: [
        "elder:read",
        "elder:write",
        "elder:export",
        "catalog:read",
        "catalog:write",
        "appointment:read",
        "order:read",
        "order:reassign",
        "health:read",
        "alert:read",
        "quality:read",
        "quality:handle",
        "bill:read",
        "bill:review",
        "stats:read",
        "stats:export",
        "admin:user",
    ],
    RoleCode.STREET: [
        "elder:read",
        "elder:export",
        "catalog:read",
        "appointment:read",
        "order:read",
        "order:reassign",
        "health:read",
        "alert:read",
        "alert:handle",
        "quality:read",
        "bill:read",
        "bill:review",
        "stats:read",
        "stats:export",
        "admin:audit",
        "admin:backup",
    ],
    RoleCode.SUPER: ALL_PERMISSION_CODES,
}

# 角色默认数据范围
ROLE_DATA_SCOPE: dict[str, str] = {
    RoleCode.ELDER: DataScope.SELF,
    RoleCode.NURSE: DataScope.SELF,
    RoleCode.GRID: DataScope.GRID,
    RoleCode.ORG_ADMIN: DataScope.ORG,
    RoleCode.STREET: DataScope.STREET,
    RoleCode.SUPER: DataScope.STREET,
}
