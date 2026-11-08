"""模型统一出口：保证 Base.metadata 注册全部表。"""
from app.db.base import Base
from app.models.billing import (
    Bill,
    BillDetail,
    PaymentRecord,
    ReconciliationDiff,
    SubsidyPolicy,
)
from app.models.catalog import ServiceItem, ServicePriceVersion
from app.models.elder import (
    AbilityAssessment,
    AccessGrant,
    ArchiveChangeLog,
    Elder,
    ElderDisease,
    ElderMedication,
    ElderTag,
)
from app.models.health import Alert, AlertAction, AlertRule, Device, HealthMetric
from app.models.order import (
    AnomalyReport,
    Appointment,
    AppointmentLog,
    WorkOrder,
    WorkOrderItem,
    WorkOrderPhoto,
    WorkOrderTrack,
)
from app.models.org import Org, Permission, Role, Staff, User, role_permission
from app.models.quality import (
    CallbackTask,
    Complaint,
    Evaluation,
    Notification,
    StaffPerformanceMonthly,
)
from app.models.system import AuditLog, BackupRecord, ConfigItem

__all__ = [
    "Base",
    "Org", "Role", "Permission", "User", "Staff", "role_permission",
    "Elder", "ElderTag", "ElderDisease", "ElderMedication", "AbilityAssessment",
    "ArchiveChangeLog", "AccessGrant",
    "ServiceItem", "ServicePriceVersion",
    "Appointment", "AppointmentLog", "WorkOrder", "WorkOrderItem",
    "WorkOrderPhoto", "WorkOrderTrack", "AnomalyReport",
    "HealthMetric", "AlertRule", "Alert", "AlertAction", "Device",
    "Evaluation", "CallbackTask", "Complaint", "StaffPerformanceMonthly", "Notification",
    "SubsidyPolicy", "Bill", "BillDetail", "ReconciliationDiff", "PaymentRecord",
    "AuditLog", "BackupRecord", "ConfigItem",
]
