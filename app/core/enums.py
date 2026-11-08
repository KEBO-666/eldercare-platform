"""领域枚举常量（数据库以字符串存储，避免 SQLite 枚举迁移问题）。"""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - 友好输出
        return self.value


# ---------- 角色与权限 ----------
class RoleCode(StrEnum):
    ELDER = "elder"            # 老人 / 家属
    NURSE = "nurse"            # 护理员
    GRID = "grid"              # 社区网格员
    ORG_ADMIN = "org_admin"    # 服务机构管理员
    STREET = "street"          # 街道监管员
    SUPER = "super"            # 超级管理员


ROLE_NAMES = {
    RoleCode.ELDER: "老人/家属",
    RoleCode.NURSE: "护理员",
    RoleCode.GRID: "社区网格员",
    RoleCode.ORG_ADMIN: "服务机构管理员",
    RoleCode.STREET: "街道监管员",
    RoleCode.SUPER: "系统超级管理员",
}


class DataScope(StrEnum):
    SELF = "self"      # 本人
    GRID = "grid"      # 本网格
    ORG = "org"        # 本机构/本社区
    STREET = "street"  # 全街道


class OrgLevel(StrEnum):
    STREET = "street"
    COMMUNITY = "community"
    GRID = "grid"


# ---------- 老人档案 ----------
class AbilityLevel(StrEnum):
    INTACT = "intact"        # 能力完好
    MILD = "mild"            # 轻度失能
    MODERATE = "moderate"    # 中度失能
    SEVERE = "severe"        # 重度失能


ABILITY_NAMES = {
    AbilityLevel.INTACT: "能力完好",
    AbilityLevel.MILD: "轻度失能",
    AbilityLevel.MODERATE: "中度失能",
    AbilityLevel.SEVERE: "重度失能",
}


class ElderTagCode(StrEnum):
    LIVING_ALONE = "living_alone"   # 独居
    EMPTY_NEST = "empty_nest"       # 空巢
    DEMENTIA = "dementia"           # 失智
    BEDRIDDEN = "bedridden"         # 卧床
    LOW_INCOME = "low_income"       # 低保


ELDER_TAG_NAMES = {
    ElderTagCode.LIVING_ALONE: "独居",
    ElderTagCode.EMPTY_NEST: "空巢",
    ElderTagCode.DEMENTIA: "失智",
    ElderTagCode.BEDRIDDEN: "卧床",
    ElderTagCode.LOW_INCOME: "低保",
}


class DifficultyType(StrEnum):
    NORMAL = "normal"   # 普通
    LOW_INCOME = "low_income"  # 低保
    EXTREME = "extreme"        # 特困


DIFFICULTY_NAMES = {
    DifficultyType.NORMAL: "普通",
    DifficultyType.LOW_INCOME: "低保",
    DifficultyType.EXTREME: "特困",
}


# ---------- 服务目录 ----------
class PricingMode(StrEnum):
    PER_TIME = "per_time"   # 按次
    PER_HOUR = "per_hour"   # 按小时


class ItemStatus(StrEnum):
    ON = "on"
    OFF = "off"


# ---------- 预约单 ----------
class AppointmentStatus(StrEnum):
    PENDING_ACCEPT = "pending_accept"      # 待受理
    ACCEPTED = "accepted"                  # 已受理
    PENDING_DISPATCH = "pending_dispatch"  # 待派单
    DISPATCHED = "dispatched"              # 已派单
    IN_SERVICE = "in_service"              # 服务中
    COMPLETED = "completed"                # 已完成
    CANCELLED = "cancelled"                # 已取消
    NO_SHOW = "no_show"                    # 已爽约


# ---------- 工单 ----------
class OrderStatus(StrEnum):
    PENDING_ACCEPT = "pending_accept"          # 待接单
    ACCEPTED = "accepted"                      # 已接单
    IN_SERVICE = "in_service"                  # 已签到/服务中
    PENDING_EVALUATION = "pending_evaluation"  # 已签退/待评价
    COMPLETED = "completed"                    # 已完成
    ANOMALY = "anomaly"                        # 异常
    CANCELLED = "cancelled"                    # 已取消


class AnomalyType(StrEnum):
    NOT_HOME = "not_home"              # 老人不在家
    REFUSED = "refused"                # 拒绝服务
    SUDDEN_ILLNESS = "sudden_illness"  # 突发不适
    OTHER = "other"


# ---------- 健康与预警 ----------
class MetricCode(StrEnum):
    SBP = "sbp"          # 收缩压 mmHg
    DBP = "dbp"          # 舒张压 mmHg
    HR = "hr"            # 心率 次/分
    SPO2 = "spo2"        # 血氧 %
    STEPS = "steps"      # 步数
    MOTION = "motion"    # 体动指数


METRIC_NAMES = {
    MetricCode.SBP: "收缩压",
    MetricCode.DBP: "舒张压",
    MetricCode.HR: "心率",
    MetricCode.SPO2: "血氧",
    MetricCode.STEPS: "步数",
    MetricCode.MOTION: "体动",
}


class MetricSource(StrEnum):
    VISIT = "visit"        # 上门录入
    WEARABLE = "wearable"  # 可穿戴设备


class AlertLevel(StrEnum):
    BLUE = "blue"
    YELLOW = "yellow"
    RED = "red"


ALERT_LEVEL_NAMES = {AlertLevel.BLUE: "蓝色预警", AlertLevel.YELLOW: "黄色预警", AlertLevel.RED: "红色预警"}
ALERT_LEVEL_RANK = {AlertLevel.BLUE: 1, AlertLevel.YELLOW: 2, AlertLevel.RED: 3}


class AlertRuleType(StrEnum):
    THRESHOLD = "threshold"      # 单项越界
    CONSECUTIVE = "consecutive"  # 连续异常
    SILENCE = "silence"          # 长时间无数据


class AlertStatus(StrEnum):
    PENDING = "pending"          # 待处置
    RESOLVED = "resolved"        # 已处置
    FALSE_ALARM = "false_alarm"  # 误报
    REFERRED = "referred"        # 已转诊


# ---------- 评价与投诉 ----------
class ComplaintStatus(StrEnum):
    NEW = "new"
    ASSIGNED = "assigned"
    REVIEWING = "reviewing"
    CONCLUDED = "concluded"
    ARCHIVED = "archived"


class CallbackStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"


# ---------- 结算 ----------
class BillStatus(StrEnum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    CONFIRMED = "confirmed"
    PAID = "paid"
    REVERSED = "reversed"


class DiffStatus(StrEnum):
    OPEN = "open"
    EXPLAINED = "explained"
    HANDLED = "handled"


class PayerType(StrEnum):
    GOVERNMENT = "government"
    INSURANCE = "insurance"
    SELF = "self"


# ---------- 系统 ----------
class AuditAction(StrEnum):
    LOGIN = "login"
    LOGIN_FAIL = "login_fail"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    EXPORT = "export"
    DISPATCH = "dispatch"
    REASSIGN = "reassign"
    REFUND = "refund"
    AUTHORIZE = "authorize"
    BACKUP = "backup"
    RESTORE = "restore"


class NotificationChannel(StrEnum):
    INAPP = "inapp"
    SMS = "sms"
    WECHAT = "wechat"
    PHONE = "phone"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    READ = "read"
    FAILED = "failed"
