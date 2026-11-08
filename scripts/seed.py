"""演示数据种子脚本（幂等）。

用法：
    python -m scripts.seed            # 首次初始化；已初始化则跳过交易数据（主轴数据始终幂等补齐）
    python -m scripts.seed --force    # 清空交易类演示数据后重新生成
    python -m scripts.seed --reset    # 删库重建（危险：会清空全部表）

特性：
- 账号密码统一 Admin@123（见 README）
- 复用 app.core.permissions 的角色权限矩阵与 app.core.security 的加密/摘要/哈希
- 固定随机种子，数据可复现
- 组织：1 街道 + 3 社区 + 6 网格；20 位老人；8 名护理员；10 个服务项目
- 近 30 天 120 张工单（含评价）、200 条健康指标（含 3 类异常序列）、预警、投诉、上月账单（含差异）
"""
from __future__ import annotations

import argparse
import random
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import (
    ROLE_NAMES,
    AbilityLevel,
    AlertLevel,
    AlertRuleType,
    AlertStatus,
    AnomalyType,
    AppointmentStatus,
    BillStatus,
    CallbackStatus,
    ComplaintStatus,
    DifficultyType,
    ItemStatus,
    MetricCode,
    MetricSource,
    OrderStatus,
    PricingMode,
    RoleCode,
)
from app.core.permissions import PERMISSIONS, ROLE_DATA_SCOPE, ROLE_PERMISSIONS
from app.core.security import encrypt_field, field_digest, hash_password
from app.db.base import Base
from app.db.session import SessionLocal, engine, init_db
from app.models import (
    AbilityAssessment,
    Alert,
    AlertAction,
    AlertRule,
    AnomalyReport,
    Appointment,
    AppointmentLog,
    ArchiveChangeLog,
    AuditLog,
    Bill,
    BillDetail,
    CallbackTask,
    Complaint,
    ConfigItem,
    Device,
    Elder,
    ElderDisease,
    ElderMedication,
    ElderTag,
    Evaluation,
    HealthMetric,
    Notification,
    Org,
    PaymentRecord,
    Permission,
    ReconciliationDiff,
    Role,
    ServiceItem,
    ServicePriceVersion,
    Staff,
    StaffPerformanceMonthly,
    SubsidyPolicy,
    User,
    WorkOrder,
    WorkOrderItem,
    WorkOrderPhoto,
    WorkOrderTrack,
)

PASSWORD = "Admin@123"
RNG = random.Random(20261108)
TOTAL_ORDERS = 120
TOTAL_METRICS = 200
SEED_KEY = "seed.version"
SEED_VERSION = "1"

NOW = datetime.now().replace(microsecond=0)
TODAY = NOW.date()


# ============================================================
# 基础数据定义
# ============================================================
STREET = {"code": "ST01", "name": "幸福街道", "level": "street"}

COMMUNITIES = [
    {"code": "CM01", "name": "幸福社区", "level": "community", "center": (31.2304, 121.4737)},
    {"code": "CM02", "name": "安康社区", "level": "community", "center": (31.2385, 121.4820)},
    {"code": "CM03", "name": "乐龄社区", "level": "community", "center": (31.2245, 121.4630)},
]

GRIDS = [
    {"code": "GD01", "name": "幸福社区一网格", "community": "CM01", "center": (31.2310, 121.4742)},
    {"code": "GD02", "name": "幸福社区二网格", "community": "CM01", "center": (31.2290, 121.4720)},
    {"code": "GD03", "name": "安康社区一网格", "community": "CM02", "center": (31.2390, 121.4828)},
    {"code": "GD04", "name": "安康社区二网格", "community": "CM02", "center": (31.2372, 121.4805)},
    {"code": "GD05", "name": "乐龄社区一网格", "community": "CM03", "center": (31.2250, 121.4638)},
    {"code": "GD06", "name": "乐龄社区二网格", "community": "CM03", "center": (31.2232, 121.4615)},
]

ELDER_NAMES = [
    "张建国", "王秀兰", "李长福", "赵桂芳", "陈志强", "刘凤英", "杨振华", "周玉梅",
    "吴德明", "徐淑珍", "孙宝山", "马春芳", "朱国平", "胡兰英", "郭永祥", "何秀英",
    "高长海", "林素琴", "罗金山", "梁桂香",
]

# 20 位老人：能力等级 / 困难类型 / 标签 / 社区 / 网格
ELDER_PLAN = [
    # (ability, difficulty, tags, community_idx, grid_idx)
    (AbilityLevel.SEVERE, DifficultyType.LOW_INCOME, ["bedridden", "living_alone", "low_income"], 0, 0),
    (AbilityLevel.MODERATE, DifficultyType.NORMAL, ["living_alone"], 0, 0),
    (AbilityLevel.MILD, DifficultyType.NORMAL, ["empty_nest"], 0, 1),
    (AbilityLevel.SEVERE, DifficultyType.EXTREME, ["bedridden", "dementia", "low_income"], 0, 1),
    (AbilityLevel.INTACT, DifficultyType.NORMAL, ["empty_nest"], 0, 0),
    (AbilityLevel.MODERATE, DifficultyType.LOW_INCOME, ["living_alone", "low_income"], 0, 1),
    (AbilityLevel.MILD, DifficultyType.NORMAL, [], 1, 2),
    (AbilityLevel.SEVERE, DifficultyType.NORMAL, ["bedridden"], 1, 2),
    (AbilityLevel.MODERATE, DifficultyType.NORMAL, ["dementia"], 1, 3),
    (AbilityLevel.INTACT, DifficultyType.NORMAL, [], 1, 3),
    (AbilityLevel.MILD, DifficultyType.LOW_INCOME, ["low_income"], 1, 2),
    (AbilityLevel.MODERATE, DifficultyType.NORMAL, ["empty_nest"], 1, 3),
    (AbilityLevel.SEVERE, DifficultyType.EXTREME, ["bedridden", "living_alone", "low_income"], 2, 4),
    (AbilityLevel.MILD, DifficultyType.NORMAL, [], 2, 4),
    (AbilityLevel.MODERATE, DifficultyType.LOW_INCOME, ["living_alone", "low_income"], 2, 4),
    (AbilityLevel.INTACT, DifficultyType.NORMAL, ["empty_nest"], 2, 5),
    (AbilityLevel.MILD, DifficultyType.NORMAL, ["dementia"], 2, 5),
    (AbilityLevel.MODERATE, DifficultyType.NORMAL, [], 2, 5),
    (AbilityLevel.SEVERE, DifficultyType.LOW_INCOME, ["bedridden", "low_income"], 2, 5),
    (AbilityLevel.INTACT, DifficultyType.NORMAL, [], 2, 4),
]

DISEASES = [
    ("高血压", "2015-03-12", "长期服用降压药"),
    ("2 型糖尿病", "2018-07-01", "控制饮食，定期监测血糖"),
    ("冠心病", "2016-11-20", "避免剧烈活动"),
    ("脑卒中后遗症", "2019-05-08", "右侧肢体活动受限"),
    ("阿尔茨海默病", "2020-09-15", "需家属陪同"),
    ("骨关节炎", "2014-02-18", "行动不便"),
]
MEDS = [
    ("苯磺酸氨氯地平片", "5mg", "每日一次"),
    ("二甲双胍缓释片", "0.5g", "每日两次"),
    ("阿司匹林肠溶片", "100mg", "每晚一次"),
    ("阿托伐他汀钙片", "20mg", "每晚一次"),
    ("多奈哌齐片", "5mg", "每晚一次"),
]

NURSES = [
    {"username": "nurse1", "name": "李春梅", "skills": ["shower", "cleaning"], "lat": 31.2308, "lng": 121.4740},
    {"username": "nurse2", "name": "王大力", "skills": ["cooking", "errand"], "lat": 31.2315, "lng": 121.4751},
    {"username": "nurse3", "name": "张红英", "skills": ["nursing", "escort"], "lat": 31.2388, "lng": 121.4825},
    {"username": "nurse4", "name": "陈国庆", "skills": ["rehab", "nursing"], "lat": 31.2375, "lng": 121.4812},
    {"username": "nurse5", "name": "赵小燕", "skills": ["chat", "escort"], "lat": 31.2248, "lng": 121.4635},
    {"username": "nurse6", "name": "孙丽娟", "skills": ["shower", "cleaning", "cooking"], "lat": 31.2296, "lng": 121.4726},
    {"username": "nurse7", "name": "周建平", "skills": ["nursing", "rehab", "escort"], "lat": 31.2320, "lng": 121.4700},
    {"username": "nurse8", "name": "吴桂兰", "skills": ["cleaning", "errand", "chat"], "lat": 31.2401, "lng": 121.4790},
]

# 10 个服务项目：(code, name, category, pricing_mode, duration, skills, levels, price, subsidized, ratio)
SERVICE_ITEMS = [
    ("S001", "助浴服务", "bath", PricingMode.PER_TIME, 60, ["shower"],
     [AbilityLevel.MODERATE, AbilityLevel.SEVERE], Decimal("120.00"), True, 0.5),
    ("S002", "助餐服务", "meal", PricingMode.PER_TIME, 30, ["cooking"],
     [], Decimal("45.00"), True, 0.4),
    ("S003", "助洁服务", "cleaning", PricingMode.PER_TIME, 60, ["cleaning"],
     [], Decimal("90.00"), True, 0.4),
    ("S004", "助行服务", "walking", PricingMode.PER_TIME, 45, ["escort"],
     [AbilityLevel.MILD, AbilityLevel.MODERATE], Decimal("60.00"), True, 0.4),
    ("S005", "助医服务", "medical", PricingMode.PER_TIME, 45, ["nursing"],
     [], Decimal("150.00"), True, 0.5),
    ("S006", "康复训练", "rehab", PricingMode.PER_TIME, 60, ["rehab"],
     [AbilityLevel.MODERATE, AbilityLevel.SEVERE], Decimal("180.00"), True, 0.3),
    ("S007", "精神慰藉", "spiritual", PricingMode.PER_TIME, 45, ["chat"],
     [AbilityLevel.INTACT, AbilityLevel.MILD, AbilityLevel.MODERATE], Decimal("80.00"), False, 0.0),
    ("S008", "助急服务", "emergency", PricingMode.PER_TIME, 60, ["nursing", "escort"],
     [], Decimal("200.00"), True, 0.2),
    ("S009", "代购代办", "errand", PricingMode.PER_TIME, 30, ["errand"],
     [], Decimal("50.00"), False, 0.0),
    ("S010", "健康监测", "monitor", PricingMode.PER_TIME, 30, ["nursing"],
     [], Decimal("80.00"), True, 0.5),
]

CHECKLISTS = {
    "S001": ["测量生命体征", "协助沐浴", "更衣整理", "清洁浴室"],
    "S002": ["核对饮食禁忌", "协助进餐", "餐后清洁", "记录进食量"],
    "S003": ["清扫居室", "更换床品", "清洗衣物", "垃圾分类"],
    "S004": ["评估行动能力", "陪同户外活动", "协助上下楼", "记录活动时长"],
    "S005": ["核对用药", "陪同就医", "取药登记", "医嘱告知家属"],
    "S006": ["评估康复状态", "肢体功能训练", "器械辅助训练", "康复记录"],
    "S007": ["情绪评估", "陪伴交流", "心理疏导", "家属沟通"],
    "S008": ["紧急情况评估", "现场应急处理", "联系家属/医院", "事件记录"],
    "S009": ["确认采购清单", "代购物品", "费用结算", "物品交接"],
    "S010": ["测量血压", "测量心率", "测量血氧", "数据上传"],
}

ITEM_SKILL_USERS = {n["username"]: n["skills"] for n in NURSES}


# ============================================================
# 工具函数
# ============================================================
def get_or_create(db: Session, model, defaults: dict | None = None, **lookup):
    obj = db.execute(select(model).filter_by(**lookup)).scalars().first()
    if obj is not None:
        return obj, False
    obj = model(**lookup, **(defaults or {}))
    db.add(obj)
    db.flush()
    return obj, True


def _id_card(idx: int) -> str:
    birth = date(1936 + (idx % 22), 1 + (idx % 12), 1 + (idx % 27))
    check = "0123456789X"[idx % 11]
    return f"310101{birth.strftime('%Y%m%d')}{idx:03d}{check}"


def _phone(idx: int) -> str:
    return f"138{idx:04d}{(idx * 137) % 10000:04d}"


def _addr(community_name: str, idx: int) -> str:
    return f"上海市{community_name}幸福路 {100 + idx} 弄 {1 + idx % 12} 号 {101 + idx % 6} 室"


def _price(db: Session, item: ServiceItem) -> ServicePriceVersion | None:
    if not item.current_price_version_id:
        return None
    return db.get(ServicePriceVersion, item.current_price_version_id)


def _round_money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


# ============================================================
# 1. 组织
# ============================================================
def seed_orgs(db: Session) -> dict[str, Org]:
    orgs: dict[str, Org] = {}
    street, _ = get_or_create(db, Org, {"name": STREET["name"], "level": STREET["level"], "sort": 0}, code=STREET["code"])
    orgs[street.code] = street
    for idx, data in enumerate(COMMUNITIES):
        org, _ = get_or_create(
            db,
            Org,
            {"name": data["name"], "level": data["level"], "parent_id": street.id, "sort": idx + 1},
            code=data["code"],
        )
        orgs[org.code] = org
    for idx, data in enumerate(GRIDS):
        parent = orgs[data["community"]]
        org, _ = get_or_create(
            db,
            Org,
            {"name": data["name"], "level": "grid", "parent_id": parent.id, "sort": idx + 1},
            code=data["code"],
        )
        orgs[org.code] = org
    db.commit()
    return orgs


# ============================================================
# 2. 角色与权限
# ============================================================
def seed_rbac(db: Session) -> dict[str, Role]:
    perms: dict[str, Permission] = {}
    for code, name, module in PERMISSIONS:
        perm, _ = get_or_create(db, Permission, {"name": name, "module": module}, code=code)
        perms[code] = perm
    db.flush()

    roles: dict[str, Role] = {}
    for role_code in RoleCode:
        role, _ = get_or_create(
            db,
            Role,
            {"name": ROLE_NAMES[role_code], "data_scope": ROLE_DATA_SCOPE[role_code]},
            code=role_code.value,
        )
        role.permissions = [perms[c] for c in ROLE_PERMISSIONS[role_code.value] if c in perms]
        roles[role_code.value] = role
    db.commit()
    return roles


# ============================================================
# 3. 账号
# ============================================================
def seed_accounts(db: Session, orgs: dict[str, Org], roles: dict[str, Role]) -> dict[str, User]:
    pwd_hash = hash_password(PASSWORD)
    users: dict[str, User] = {}

    def make(username: str, real_name: str, role_code: str, org_id: int | None, scope: str) -> User:
        user, created = get_or_create(
            db,
            User,
            {
                "password_hash": pwd_hash,
                "real_name": real_name,
                "role_id": roles[role_code].id,
                "org_id": org_id,
                "data_scope": scope,
                "is_active": True,
            },
            username=username,
        )
        if not created:
            user.password_hash = pwd_hash
        users[username] = user
        return user

    make("super", "系统管理员", "super", orgs["ST01"].id, ROLE_DATA_SCOPE[RoleCode.SUPER])
    make("street", "街道监管员·王主任", "street", orgs["ST01"].id, ROLE_DATA_SCOPE[RoleCode.STREET])
    make("orgadmin", "机构管理员·刘经理", "org_admin", orgs["CM01"].id, ROLE_DATA_SCOPE[RoleCode.ORG_ADMIN])
    make("grid1", "网格员·陈网格", "grid", orgs["GD01"].id, ROLE_DATA_SCOPE[RoleCode.GRID])
    make("grid2", "网格员·孙网格", "grid", orgs["GD03"].id, ROLE_DATA_SCOPE[RoleCode.GRID])

    nurse_orgs = ["CM01", "CM01", "CM02", "CM02", "CM03", "CM01", "CM02", "CM03"]
    for idx, nurse in enumerate(NURSES):
        user = make(nurse["username"], nurse["name"], "nurse", orgs[nurse_orgs[idx]].id, ROLE_DATA_SCOPE[RoleCode.NURSE])
        user.phone_enc = encrypt_field(_phone(100 + idx))

    for idx in range(1, 21):
        make(f"elder{idx}", ELDER_NAMES[idx - 1], "elder", orgs[COMMUNITIES[(idx - 1) % 3]["code"]].id,
             ROLE_DATA_SCOPE[RoleCode.ELDER])

    db.commit()
    return users


def seed_staff(db: Session, orgs: dict[str, Org], users: dict[str, User]) -> dict[str, Staff]:
    staffs: dict[str, Staff] = {}
    nurse_orgs = ["CM01", "CM01", "CM02", "CM02", "CM03", "CM01", "CM02", "CM03"]
    for idx, nurse in enumerate(NURSES):
        user = users[nurse["username"]]
        staff, created = get_or_create(
            db,
            Staff,
            {
                "user_id": user.id,
                "name": nurse["name"],
                "phone_enc": encrypt_field(_phone(100 + idx)),
                "skills": nurse["skills"],
                "service_radius_km": 3.0,
                "base_lat": nurse["lat"],
                "base_lng": nurse["lng"],
                "status": "active",
                "rating_cached": 4.6,
                "order_count_cached": 0,
                "org_id": orgs[nurse_orgs[idx]].id,
                "is_focus": idx == 5,
            },
            employee_no=f"N{idx + 1:04d}",
        )
        staffs[nurse["username"]] = staff
    db.commit()
    return staffs


# ============================================================
# 4. 服务目录与价格版本
# ============================================================
def seed_catalog(db: Session) -> dict[str, ServiceItem]:
    items: dict[str, ServiceItem] = {}
    for idx, (code, name, category, pricing, duration, skills, levels, price, subsidized, ratio) in enumerate(SERVICE_ITEMS):
        item, created = get_or_create(
            db,
            ServiceItem,
            {
                "name": name,
                "category": category,
                "pricing_mode": pricing,
                "standard_duration_min": duration,
                "required_skills": skills,
                "applicable_levels": levels,
                "is_subsidized": subsidized,
                "subsidy_ratio": ratio,
                "status": ItemStatus.ON.value,
                "description": f"{name} · 标准时长 {duration} 分钟",
            },
            code=code,
        )
        # 价格版本 v1（幂等）
        v1 = db.execute(
            select(ServicePriceVersion).where(
                ServicePriceVersion.service_item_id == item.id, ServicePriceVersion.version == 1
            )
        ).scalars().first()
        if v1 is None:
            v1 = ServicePriceVersion(
                service_item_id=item.id,
                version=1,
                unit_price=price,
                effective_from=NOW - timedelta(days=180),
                remark="初始价格",
            )
            db.add(v1)
            db.flush()
        # 前 3 个项目演示价格版本化：新增 v2 并关闭 v1 有效期
        if idx < 3:
            v2 = db.execute(
                select(ServicePriceVersion).where(
                    ServicePriceVersion.service_item_id == item.id, ServicePriceVersion.version == 2
                )
            ).scalars().first()
            if v2 is None:
                v2 = ServicePriceVersion(
                    service_item_id=item.id,
                    version=2,
                    unit_price=_round_money(price + Decimal("10.00")),
                    effective_from=NOW - timedelta(days=15),
                    remark="调价：人工成本上涨",
                )
                db.add(v2)
                db.flush()
                v1.effective_to = v2.effective_from
                item.current_price_version_id = v2.id
            else:
                item.current_price_version_id = v2.id
        else:
            item.current_price_version_id = item.current_price_version_id or v1.id
        items[code] = item
    db.commit()
    return items


# ============================================================
# 5. 补贴政策矩阵
# ============================================================
def seed_policies(db: Session) -> int:
    ratio_matrix = {
        DifficultyType.NORMAL: {AbilityLevel.INTACT: 0.0, AbilityLevel.MILD: 0.10, AbilityLevel.MODERATE: 0.20,
                                AbilityLevel.SEVERE: 0.30},
        DifficultyType.LOW_INCOME: {AbilityLevel.INTACT: 0.20, AbilityLevel.MILD: 0.30, AbilityLevel.MODERATE: 0.40,
                                    AbilityLevel.SEVERE: 0.50},
        DifficultyType.EXTREME: {AbilityLevel.INTACT: 0.30, AbilityLevel.MILD: 0.40, AbilityLevel.MODERATE: 0.50,
                                 AbilityLevel.SEVERE: 0.60},
    }
    limit_matrix = {AbilityLevel.INTACT: 200, AbilityLevel.MILD: 300, AbilityLevel.MODERATE: 400,
                    AbilityLevel.SEVERE: 500}
    ltc_matrix = {AbilityLevel.INTACT: 0.10, AbilityLevel.MILD: 0.20, AbilityLevel.MODERATE: 0.30,
                  AbilityLevel.SEVERE: 0.40}
    count = 0
    for difficulty, levels in ratio_matrix.items():
        for ability, ratio in levels.items():
            _, created = get_or_create(
                db,
                SubsidyPolicy,
                {
                    "subsidy_ratio": ratio,
                    "monthly_limit": Decimal(str(limit_matrix[ability])),
                    "ltc_ratio": ltc_matrix[ability],
                    "effective_from": NOW - timedelta(days=365),
                    "enabled": True,
                    "note": "基础补贴政策矩阵",
                },
                ability_level=ability.value,
                difficulty_type=difficulty.value,
            )
            count += int(created)
    db.commit()
    return count


# ============================================================
# 6. 预警规则
# ============================================================
def seed_alert_rules(db: Session) -> int:
    try:  # 优先复用 D 模块的幂等初始化
        from app.services.alert_service import ensure_default_rules  # type: ignore

        return int(ensure_default_rules(db) or 0)
    except Exception:  # noqa: BLE001 - 模块未就绪时使用内置默认规则
        pass

    rules = [
        {
            "code": "SBP_HIGH",
            "name": "收缩压危急值",
            "rule_type": AlertRuleType.THRESHOLD.value,
            "metric_code": MetricCode.SBP.value,
            "operator": ">=",
            "threshold": 180.0,
            "threshold2": 90.0,
            "level": AlertLevel.RED.value,
            "cooldown_minutes": 30,
            "escalate_minutes": 30,
            "push_minutes": 5,
            "description": "收缩压 ≥180 或 ≤90 mmHg 时红色预警",
        },
        {
            "code": "SPO2_LOW",
            "name": "血氧过低",
            "rule_type": AlertRuleType.THRESHOLD.value,
            "metric_code": MetricCode.SPO2.value,
            "operator": "<=",
            "threshold": 90.0,
            "level": AlertLevel.RED.value,
            "cooldown_minutes": 30,
            "escalate_minutes": 30,
            "push_minutes": 5,
            "description": "血氧 <90% 时红色预警",
        },
        {
            "code": "HR_CONSEC",
            "name": "心率连续异常",
            "rule_type": AlertRuleType.CONSECUTIVE.value,
            "metric_code": MetricCode.HR.value,
            "operator": ">",
            "threshold": 100.0,
            "window_size": 3,
            "level": AlertLevel.YELLOW.value,
            "cooldown_minutes": 30,
            "escalate_minutes": 30,
            "description": "连续 3 次心率 >100 次/分时黄色预警",
        },
        {
            "code": "SILENCE",
            "name": "48 小时无数据",
            "rule_type": AlertRuleType.SILENCE.value,
            "silence_hours": 48,
            "level": AlertLevel.BLUE.value,
            "cooldown_minutes": 120,
            "escalate_minutes": 30,
            "description": "老人 48 小时内无健康数据上报",
        },
    ]
    count = 0
    for data in rules:
        rule, created = get_or_create(
            db,
            AlertRule,
            {
                "name": data["name"],
                "rule_type": data["rule_type"],
                "metric_code": data.get("metric_code"),
                "operator": data.get("operator"),
                "threshold": data.get("threshold"),
                "threshold2": data.get("threshold2"),
                "window_size": data.get("window_size"),
                "silence_hours": data.get("silence_hours"),
                "level": data["level"],
                "cooldown_minutes": data.get("cooldown_minutes", 30),
                "escalate_minutes": data.get("escalate_minutes", 30),
                "push_minutes": data.get("push_minutes", 5),
                "enabled": True,
                "description": data.get("description"),
            },
            code=data["code"],
        )
        count += int(created)
    db.commit()
    return count


# ============================================================
# 7. 老人档案
# ============================================================
def seed_elders(db: Session, orgs: dict[str, Org], users: dict[str, User]) -> list[Elder]:
    elders: list[Elder] = []
    grid_center = {g["code"]: g["center"] for g in GRIDS}
    for idx, plan in enumerate(ELDER_PLAN, start=1):
        ability, difficulty, tags, community_idx, grid_idx = plan
        community = COMMUNITIES[community_idx]
        grid = GRIDS[grid_idx]
        lat0, lng0 = grid_center[grid["code"]]
        lat = round(lat0 + RNG.uniform(-0.0022, 0.0022), 6)
        lng = round(lng0 + RNG.uniform(-0.0022, 0.0022), 6)
        id_card = _id_card(idx)
        phone = _phone(idx)
        address = _addr(community["name"], idx)
        elder, created = get_or_create(
            db,
            Elder,
            {
                "name": ELDER_NAMES[idx - 1],
                "gender": "male" if idx % 2 == 1 else "female",
                "birth_date": date(1936 + (idx % 22), 1 + (idx % 12), 1 + (idx % 27)),
                "id_card_enc": encrypt_field(id_card),
                "id_card_hash": field_digest(id_card),
                "phone_enc": encrypt_field(phone),
                "phone_hash": field_digest(phone),
                "address_enc": encrypt_field(address),
                "address_masked": address[:6] + "****",
                "lat": lat,
                "lng": lng,
                "community_org_id": orgs[community["code"]].id,
                "grid_org_id": orgs[grid["code"]].id,
                "ability_level": ability.value,
                "difficulty_type": difficulty.value,
                "insurance_type": "城镇职工医保" if idx % 2 == 0 else "城乡居民医保",
                "ltc_type": "长护险三级" if idx % 3 == 0 else ("长护险二级" if idx % 3 == 1 else None),
                "emergency_name": f"{ELDER_NAMES[idx - 1][0]}家属",
                "emergency_relation": "子女" if idx % 2 == 0 else "配偶",
                "emergency_phone_enc": encrypt_field(_phone(300 + idx)),
                "allergy": RNG.choice(["无", "青霉素过敏", "磺胺类过敏", "无"]),
                "notes": "重点关注对象，建议每周回访" if tags else None,
                "user_id": users[f"elder{idx}"].id,
                "is_deleted": 0,
            },
            code=f"E{idx:04d}",
        )
        elders.append(elder)

        for tag in tags:
            get_or_create(db, ElderTag, {}, elder_id=elder.id, tag=tag)

        # 慢病与用药
        for disease_name, diagnosed, note in RNG.sample(DISEASES, k=min(2, len(DISEASES))):
            get_or_create(
                db,
                ElderDisease,
                {"diagnosed_at": date.fromisoformat(diagnosed), "note": note},
                elder_id=elder.id,
                name=disease_name,
            )
        for med_name, dosage, freq in RNG.sample(MEDS, k=2):
            get_or_create(
                db, ElderMedication, {"dosage": dosage, "frequency": freq}, elder_id=elder.id, name=med_name
            )

        # 能力评估记录（2 条，最新一条与当前等级一致；按 老人+等级+依据 幂等）
        old_level = RNG.choice([AbilityLevel.INTACT, AbilityLevel.MILD])
        get_or_create(
            db,
            AbilityAssessment,
            {
                "score": RNG.randint(60, 85),
                "assessed_at": NOW - timedelta(days=180),
                "assessor_id": users["grid1"].id,
            },
            elder_id=elder.id,
            level=old_level.value,
            basis="年度常规评估",
        )
        get_or_create(
            db,
            AbilityAssessment,
            {
                "score": RNG.randint(20, 90),
                "assessor_id": users["grid1"].id,
            },
            elder_id=elder.id,
            level=ability.value,
            basis="复评：结合体检报告与生活自理能力",
            assessed_at=(NOW - timedelta(days=30)).replace(hour=9, minute=0, second=0, microsecond=0),
        )
    db.commit()
    return elders


# ============================================================
# 8. 健康指标（200 条，含 3 类异常序列）
# ============================================================
def seed_metrics(db: Session, elders: list[Elder]) -> list[HealthMetric]:
    created: list[HealthMetric] = []

    def add(elder: Elder, code: str, value: float, when: datetime, source: str = MetricSource.WEARABLE.value,
            unit: str | None = None) -> HealthMetric:
        if len(created) >= TOTAL_METRICS:
            return None  # type: ignore[return-value]
        metric = HealthMetric(
            elder_id=elder.id,
            metric_code=code,
            value=float(value),
            unit=unit,
            source=source,
            measured_at=when,
            recorded_by=None,
        )
        db.add(metric)
        created.append(metric)
        return metric

    def event(elder: Elder, when: datetime, sbp: float, dbp: float, hr: float, spo2: float) -> None:
        if len(created) + 4 > TOTAL_METRICS:
            return
        add(elder, MetricCode.SBP.value, sbp, when, unit="mmHg")
        add(elder, MetricCode.DBP.value, dbp, when, unit="mmHg")
        add(elder, MetricCode.HR.value, hr, when, unit="次/分")
        add(elder, MetricCode.SPO2.value, spo2, when, unit="%")

    # ---- ① 老人 1：收缩压 ≥180（红色预警） ----
    elder1 = elders[0]
    base = NOW - timedelta(days=13)
    for day in range(0, 14):
        when = (base + timedelta(days=day)).replace(hour=9, minute=0, second=0)
        if day == 13:
            event(elder1, when, 186, 104, 88, 96)      # 危急值
        else:
            event(elder1, when, RNG.uniform(138, 158), RNG.uniform(82, 94), RNG.uniform(70, 88), RNG.uniform(95, 98))

    # ---- ② 老人 2：连续 3 次心率 >100（黄色预警） ----
    elder2 = elders[1]
    for day in range(10):
        when = (NOW - timedelta(days=9 - day)).replace(hour=8, minute=30, second=0)
        hr = 106 + day if day >= 7 else RNG.uniform(72, 92)
        event(elder2, when, RNG.uniform(126, 142), RNG.uniform(76, 88), hr, RNG.uniform(95, 98))

    # ---- ③ 老人 3：48 小时无数据（蓝色预警） ----
    elder3 = elders[2]
    for day in range(4):
        when = (NOW - timedelta(days=4 + day)).replace(hour=10, minute=0, second=0)
        event(elder3, when, RNG.uniform(130, 145), RNG.uniform(78, 88), RNG.uniform(70, 85), RNG.uniform(95, 98))

    # ---- 其余老人：分散指标（凑满 200 条） ----
    codes = [MetricCode.SBP, MetricCode.DBP, MetricCode.HR, MetricCode.SPO2, MetricCode.STEPS, MetricCode.MOTION]
    units = {
        MetricCode.SBP: "mmHg", MetricCode.DBP: "mmHg", MetricCode.HR: "次/分",
        MetricCode.SPO2: "%", MetricCode.STEPS: "步", MetricCode.MOTION: "次",
    }
    idx = 3
    for elder in elders[3:]:
        slots = 7 + (idx % 2)          # 7~8 条
        for k in range(slots):
            if len(created) >= TOTAL_METRICS:
                break
            code = codes[(idx + k) % len(codes)]
            when = (NOW - timedelta(days=RNG.randint(1, 28))).replace(
                hour=RNG.randint(7, 20), minute=RNG.choice([0, 15, 30, 45]), second=0
            )
            if code == MetricCode.SBP:
                value = RNG.uniform(120, 168)
            elif code == MetricCode.DBP:
                value = RNG.uniform(70, 98)
            elif code == MetricCode.HR:
                value = RNG.uniform(62, 96)
            elif code == MetricCode.SPO2:
                value = RNG.uniform(93, 99)
            elif code == MetricCode.STEPS:
                value = RNG.randint(800, 6200)
            else:
                value = RNG.randint(20, 90)
            add(elder, code.value, round(value, 1), when, unit=units[code])
        idx += 1

    # 补足到 200 条（老人 1 的数据补步数）
    while len(created) < TOTAL_METRICS:
        when = (NOW - timedelta(days=RNG.randint(1, 13))).replace(hour=18, minute=0, second=0)
        add(elder1, MetricCode.STEPS.value, RNG.randint(500, 3500), when, unit="步")

    db.commit()
    return created


# ============================================================
# 9. 预警与推送
# ============================================================
def seed_alerts(db: Session, elders: list[Elder], users: dict[str, User]) -> list[Alert]:
    rules = {r.code: r for r in db.execute(select(AlertRule)).scalars().all()}
    alerts: list[Alert] = []

    def make(elder: Elder, rule_code: str, level: str, summary: str, metric_code: str, metric_value: float,
             created_at: datetime, status: str = AlertStatus.PENDING.value) -> Alert:
        rule = rules.get(rule_code)
        alert = Alert(
            elder_id=elder.id,
            rule_id=rule.id if rule else None,
            level=level,
            status=status,
            metric_code=metric_code,
            metric_value=metric_value,
            summary=summary,
            created_from_value=metric_value,
            created_at=created_at,
            pushed_at=created_at + timedelta(seconds=20),
            deadline_at=created_at + timedelta(minutes=30),
            response_seconds=RNG.randint(60, 900) if status != AlertStatus.PENDING.value else None,
        )
        if status != AlertStatus.PENDING.value:
            alert.resolved_at = created_at + timedelta(minutes=RNG.randint(5, 25))
            alert.resolved_by = users["grid1"].id
            alert.resolution_type = status
            alert.resolution_note = "现场/电话核处，已通知家属"
            alert.measure = "联系家属并指导复测血压"
            alert.handling_seconds = RNG.randint(300, 1500)
        db.add(alert)
        db.flush()
        db.add(
            AlertAction(
                alert_id=alert.id,
                action_type="push",
                channel="sms",
                target=_phone(1)[:3] + "****" + _phone(1)[-4:],
                note="红色预警 5 分钟内推送（模拟短信 + 站内消息）",
                action_at=alert.pushed_at,
            )
        )
        if status != AlertStatus.PENDING.value:
            db.add(
                AlertAction(
                    alert_id=alert.id,
                    action_type="resolve",
                    operator_id=users["grid1"].id,
                    note=alert.resolution_note,
                    action_at=alert.resolved_at,
                )
            )
        db.add(
            Notification(
                elder_id=elder.id,
                channel="inapp",
                title=f"【{ALERT_LEVEL_NAMES_CN(level)}】{elder.name} 健康预警",
                content=summary,
                status="sent",
                related_type="alert",
                related_id=alert.id,
                created_at=created_at + timedelta(seconds=15),
            )
        )
        alerts.append(alert)
        return alert

    make(elders[0], "SBP_HIGH", AlertLevel.RED.value,
         "收缩压 186 mmHg（≥180），立即处置", MetricCode.SBP.value, 186.0, NOW - timedelta(minutes=8))
    make(elders[1], "HR_CONSEC", AlertLevel.YELLOW.value,
         "连续 3 次心率 >100 次/分（106/107/108）", MetricCode.HR.value, 108.0, NOW - timedelta(hours=2))
    make(elders[2], "SILENCE", AlertLevel.BLUE.value,
         "已 52 小时无健康数据上报（设备可能离线）", "", 0.0, NOW - timedelta(hours=6))

    make(elders[3], "SPO2_LOW", AlertLevel.RED.value, "血氧 88%（<90%），已电话联系家属转诊",
         MetricCode.SPO2.value, 88.0, NOW - timedelta(days=2, hours=3), status=AlertStatus.REFERRED.value)
    make(elders[5], "SBP_HIGH", AlertLevel.RED.value, "收缩压 182 mmHg，服药后复测正常",
         MetricCode.SBP.value, 182.0, NOW - timedelta(days=4, hours=5), status=AlertStatus.RESOLVED.value)
    make(elders[8], "HR_CONSEC", AlertLevel.YELLOW.value, "连续 3 次心率 >100，设备佩戴松动误报",
         MetricCode.HR.value, 112.0, NOW - timedelta(days=5, hours=1), status=AlertStatus.FALSE_ALARM.value)
    db.commit()
    return alerts


def ALERT_LEVEL_NAMES_CN(level: str) -> str:
    return {"blue": "蓝色预警", "yellow": "黄色预警", "red": "红色预警"}.get(level, level)


# ============================================================
# 10. 预约与工单
# ============================================================
def seed_appointments(db: Session, elders: list[Elder], items: dict[str, ServiceItem], users: dict[str, User]) -> list[Appointment]:
    plan = (
        [(AppointmentStatus.PENDING_ACCEPT.value, 6)]
        + [(AppointmentStatus.ACCEPTED.value, 6)]
        + [(AppointmentStatus.PENDING_DISPATCH.value, 8)]
        + [(AppointmentStatus.COMPLETED.value, 5)]
        + [(AppointmentStatus.CANCELLED.value, 3)]
        + [(AppointmentStatus.NO_SHOW.value, 2)]
    )
    statuses: list[str] = [status for status, count in plan for _ in range(count)]
    RNG.shuffle(statuses)
    appts: list[Appointment] = []
    for idx, status in enumerate(statuses, start=1):
        elder = elders[(idx * 3) % len(elders)]
        item = items[SERVICE_ITEMS[idx % len(SERVICE_ITEMS)][0]]
        if status == AppointmentStatus.PENDING_ACCEPT.value:
            desired = (NOW + timedelta(hours=RNG.randint(2, 30))).replace(minute=0, second=0)
        elif status == AppointmentStatus.ACCEPTED.value:
            desired = (NOW + timedelta(days=1, hours=RNG.randint(0, 6))).replace(minute=0, second=0)
        elif status == AppointmentStatus.PENDING_DISPATCH.value:
            desired = (NOW + timedelta(hours=RNG.randint(1, 20))).replace(minute=0, second=0)
        else:
            desired = (NOW - timedelta(days=RNG.randint(1, 28))).replace(hour=RNG.randint(8, 16), minute=0, second=0)
        appt = Appointment(
            code=f"A{idx:04d}",
            elder_id=elder.id,
            service_item_id=item.id,
            desired_start=desired,
            address_enc=encrypt_field(_addr(COMMUNITIES[(idx - 1) % 3]["name"], idx)),
            remark=RNG.choice(["", "老人行动不便，请提前联系", "需要测量血压", "家属会在场"]),
            status=status,
            community_org_id=elder.community_org_id,
            created_by=users[f"elder{(idx % 20) + 1}"].id,
            accepted_by=users["grid1"].id if status not in (AppointmentStatus.PENDING_ACCEPT.value,) else None,
        )
        if status == AppointmentStatus.CANCELLED.value:
            appt.cancel_reason = RNG.choice(["老人临时住院", "家属自行安排", "重复下单"])
            appt.is_chargeable_cancel = RNG.random() < 0.4
            appt.cancellation_fee = Decimal("20.00") if appt.is_chargeable_cancel else Decimal("0")
        db.add(appt)
        db.flush()
        db.add(
            AppointmentLog(
                appointment_id=appt.id,
                from_status=None,
                to_status=AppointmentStatus.PENDING_ACCEPT.value,
                action="create",
                operator_id=appt.created_by,
                remark="提交预约",
            )
        )
        if status == AppointmentStatus.ACCEPTED.value:
            db.add(
                AppointmentLog(
                    appointment_id=appt.id,
                    from_status=AppointmentStatus.PENDING_ACCEPT.value,
                    to_status=AppointmentStatus.ACCEPTED.value,
                    action="accept",
                    operator_id=users["grid1"].id,
                    remark="网格员受理",
                )
            )
        appts.append(appt)
    db.commit()
    return appts


def seed_orders(
    db: Session,
    elders: list[Elder],
    items: dict[str, ServiceItem],
    staffs: dict[str, Staff],
    appts: list[Appointment],
    users: dict[str, User],
) -> list[WorkOrder]:
    plan = [
        (OrderStatus.COMPLETED.value, 86),
        (OrderStatus.PENDING_EVALUATION.value, 8),
        (OrderStatus.IN_SERVICE.value, 4),
        (OrderStatus.ACCEPTED.value, 6),
        (OrderStatus.PENDING_ACCEPT.value, 8),
        (OrderStatus.ANOMALY.value, 5),
        (OrderStatus.CANCELLED.value, 3),
    ]
    statuses: list[str] = [status for status, count in plan for _ in range(count)]
    assert len(statuses) == TOTAL_ORDERS, len(statuses)
    RNG.shuffle(statuses)

    nurse_list = list(staffs.items())
    orders: list[WorkOrder] = []
    for idx, status in enumerate(statuses, start=1):
        elder = elders[(idx * 7) % len(elders)]
        item_code, *_ = SERVICE_ITEMS[idx % len(SERVICE_ITEMS)]
        item = items[item_code]
        price_version = _price(db, item)
        nurse_username, staff = nurse_list[(idx * 3) % len(nurse_list)]
        standard = int(item.standard_duration_min or 60)

        if status in (OrderStatus.PENDING_ACCEPT.value, OrderStatus.ACCEPTED.value):
            scheduled = (NOW + timedelta(hours=RNG.randint(1, 8))).replace(minute=RNG.choice([0, 30]), second=0)
        elif status == OrderStatus.IN_SERVICE.value:
            scheduled = (NOW - timedelta(minutes=RNG.randint(20, 70))).replace(second=0)
        else:
            scheduled = (NOW - timedelta(days=RNG.randint(0, 29))).replace(
                hour=RNG.randint(8, 17), minute=RNG.choice([0, 30]), second=0
            )
        amount = _round_money((price_version.unit_price if price_version else Decimal("80.00")) * 1)

        order = WorkOrder(
            code=f"W{idx:04d}",
            appointment_id=appts[idx % len(appts)].id if idx % 3 == 0 else None,
            elder_id=elder.id,
            service_item_id=item.id,
            nurse_id=None if status == OrderStatus.PENDING_ACCEPT.value else staff.id,
            status=status,
            scheduled_start=scheduled,
            scheduled_end=scheduled + timedelta(minutes=standard),
            price_version_id=price_version.id if price_version else None,
            unit_price=price_version.unit_price if price_version else Decimal("80.00"),
            quantity=1,
            amount=amount,
            community_org_id=elder.community_org_id,
            created_by=users["grid1"].id,
        )
        if status not in (OrderStatus.PENDING_ACCEPT.value, OrderStatus.CANCELLED.value):
            order.accepted_at = scheduled - timedelta(minutes=RNG.randint(5, 40))

        # 签到 / 签退留痕
        if status in (OrderStatus.IN_SERVICE.value, OrderStatus.PENDING_EVALUATION.value,
                      OrderStatus.COMPLETED.value, OrderStatus.ANOMALY.value):
            checkin_at = scheduled + timedelta(minutes=RNG.randint(0, 12))
            duration = max(20, standard + RNG.randint(-10, 25))
            if idx % 23 == 0:                     # 显著时长异常（>30 分钟偏差）
                duration = standard + RNG.randint(31, 70)
            order.checkin_at = checkin_at
            order.checkin_lat = round((elder.lat or 31.23) + RNG.uniform(-0.0004, 0.0004), 6)
            order.checkin_lng = round((elder.lng or 121.47) + RNG.uniform(-0.0004, 0.0004), 6)
            order.checkin_distance_m = round(RNG.uniform(8, 90), 1)
            if idx % 17 == 0:                     # 签到距离 >200 米，必须填写说明
                order.checkin_distance_m = round(RNG.uniform(205, 320), 1)
                order.checkin_note = "小区门禁绕行，已提前与老人电话确认"
            if status in (OrderStatus.PENDING_EVALUATION.value, OrderStatus.COMPLETED.value):
                order.checkout_at = checkin_at + timedelta(minutes=duration)
                order.duration_min = duration
                order.duration_anomaly = abs(duration - standard) > 30
                order.summary = RNG.choice([
                    "完成既定服务，老人精神状态良好。",
                    "服务过程中老人主诉轻微头晕，已测量血压并告知家属。",
                    "按项目清单完成服务，居室清洁到位。",
                    "协助进餐并记录进食量，提醒按时服药。",
                ])
        db.add(order)
        db.flush()
        # 轨迹（签到定位留痕），需在工单入库后写入
        if order.checkin_at is not None:
            db.add(
                WorkOrderTrack(
                    work_order_id=order.id,
                    kind="checkin",
                    lat=order.checkin_lat,
                    lng=order.checkin_lng,
                    distance_m=order.checkin_distance_m,
                    note=order.checkin_note,
                    created_at=order.checkin_at,
                )
            )
            if order.checkout_at is not None:
                db.add(
                    WorkOrderTrack(
                        work_order_id=order.id,
                        kind="checkout",
                        lat=order.checkin_lat,
                        lng=order.checkin_lng,
                        distance_m=order.checkin_distance_m,
                        note="签退",
                        created_at=order.checkout_at,
                    )
                )
        db.flush()

        # 项目勾选清单
        names = CHECKLISTS[item_code]
        done_all = status in (OrderStatus.PENDING_EVALUATION.value, OrderStatus.COMPLETED.value)
        for pos, name in enumerate(names):
            completed = done_all or (status in (OrderStatus.IN_SERVICE.value, OrderStatus.ANOMALY.value) and pos < 2)
            db.add(
                WorkOrderItem(
                    work_order_id=order.id,
                    item_name=name,
                    is_required=pos < len(names) - 1,
                    completed=completed,
                    completed_at=(order.checkin_at + timedelta(minutes=10 * pos)) if completed and order.checkin_at else None,
                )
            )

        # 异常上报
        if status == OrderStatus.ANOMALY.value:
            db.add(
                AnomalyReport(
                    work_order_id=order.id,
                    elder_id=elder.id,
                    anomaly_type=RNG.choice([AnomalyType.NOT_HOME.value, AnomalyType.REFUSED.value,
                                             AnomalyType.SUDDEN_ILLNESS.value, AnomalyType.OTHER.value]),
                    description=RNG.choice([
                        "老人不在家，电话无人接听，已联系家属。",
                        "老人拒绝本次服务，情绪不稳定。",
                        "老人突发胸闷，已联系社区卫生服务中心。",
                    ]),
                    reported_by=staff.user_id,
                    reported_at=order.checkin_at or scheduled,
                    status="pending",
                )
            )

        if status == OrderStatus.COMPLETED.value:
            order.completed_at = (order.checkout_at or scheduled) + timedelta(minutes=5)
        orders.append(order)

    db.commit()
    return orders


# ============================================================
# 11. 评价 / 回访 / 投诉
# ============================================================
def seed_quality(db: Session, orders: list[WorkOrder], staffs: dict[str, Staff], users: dict[str, User]) -> dict[str, int]:
    staff_by_id = {s.id: s for s in staffs.values()}
    evaluations = 0
    callbacks = 0
    completed_orders = [o for o in orders if o.status == OrderStatus.COMPLETED.value]
    for idx, order in enumerate(completed_orders):
        if RNG.random() > 0.85:
            continue
        scores = [RNG.choice([5, 5, 5, 5, 4, 4, 3, 3, 2]) for _ in range(4)]
        if idx % 15 == 0:                       # 构造低分评价（触发回访）
            scores = [3, 2, 3, 3]
        avg = round(sum(scores) / 4, 2)
        evaluation = Evaluation(
            work_order_id=order.id,
            elder_id=order.elder_id,
            nurse_id=order.nurse_id,
            punctuality=scores[0],
            attitude=scores[1],
            professionalism=scores[2],
            effect=scores[3],
            avg_score=avg,
            comment=RNG.choice([
                "服务很专业，态度好，感谢！",
                "准时到达，服务细致。",
                "整体满意，希望下次再快一点。",
                "服务一般，时间有点赶。",
                "",
            ]),
            rated_by=order.elder_id,
            created_at=(order.completed_at or order.scheduled_start) + timedelta(hours=RNG.randint(1, 30)),
        )
        db.add(evaluation)
        db.flush()
        order.rating = avg
        evaluations += 1
        if avg <= 3:
            db.add(
                CallbackTask(
                    evaluation_id=evaluation.id,
                    elder_id=order.elder_id,
                    assigned_to=users["grid1"].id,
                    due_at=evaluation.created_at + timedelta(hours=48),
                    status=CallbackStatus.PENDING.value,
                )
            )
            callbacks += 1

    # 3 条投诉
    complaints_data = [
        ("C0001", completed_orders[0], ComplaintStatus.NEW.value, False, None, "护理员未按约定时间上门，迟到 40 分钟。"),
        ("C0002", completed_orders[3], ComplaintStatus.REVIEWING.value, False, None, "服务项目未做完就离开，清洁不彻底。"),
        ("C0003", completed_orders[6], ComplaintStatus.CONCLUDED.value, True, "refund", "护理员服务时态度生硬，且时长不足。"),
    ]
    complaints = 0
    for code, order, status, established, resolution, content in complaints_data:
        if db.execute(select(Complaint).where(Complaint.code == code)).scalars().first():
            continue
        complaint = Complaint(
            code=code,
            elder_id=order.elder_id,
            work_order_id=order.id,
            nurse_id=order.nurse_id,
            content=content,
            status=status,
            handler_id=users["orgadmin"].id if status != ComplaintStatus.NEW.value else None,
            institution_statement="已调取服务轨迹与照片核实，正在与家属沟通。" if status != ComplaintStatus.NEW.value else None,
            conclusion="经核实护理员服务时长不足，投诉成立，已退费处理。" if status == ComplaintStatus.CONCLUDED.value else None,
            is_established=established,
            resolution=resolution,
            concluded_at=NOW - timedelta(days=2) if status == ComplaintStatus.CONCLUDED.value else None,
        )
        db.add(complaint)
        db.flush()
        if status == ComplaintStatus.CONCLUDED.value:
            db.add(
                CallbackTask(
                    complaint_id=complaint.id,
                    elder_id=complaint.elder_id,
                    assigned_to=users["grid1"].id,
                    due_at=NOW + timedelta(hours=12),
                    status=CallbackStatus.PENDING.value,
                )
            )
        complaints += 1
    db.commit()

    # 护理员评分缓存
    for staff in staff_by_id.values():
        rows = [o.rating for o in orders if o.nurse_id == staff.id and o.rating]
        if rows:
            staff.rating_cached = round(sum(rows) / len(rows), 2)
        staff.order_count_cached = sum(1 for o in orders if o.nurse_id == staff.id)
    db.commit()
    return {"evaluations": evaluations, "callbacks": callbacks, "complaints": complaints}


def seed_performance(db: Session, orders: list[WorkOrder], staffs: dict[str, Staff]) -> int:
    period = (NOW.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    rows: list[StaffPerformanceMonthly] = []
    for staff in staffs.values():
        mine = [o for o in orders if o.nurse_id == staff.id]
        done = [o for o in mine if o.status == OrderStatus.COMPLETED.value]
        scores = [o.rating for o in done if o.rating]
        anomaly = [o for o in done if o.duration_anomaly]
        rows.append(
            StaffPerformanceMonthly(
                staff_id=staff.id,
                period=period,
                avg_score=round(sum(scores) / len(scores), 2) if scores else 0.0,
                punctuality_rate=round(1 - (len(anomaly) / len(done)) if done else 0.95, 4),
                anomaly_rate=round(len(anomaly) / len(done), 4) if done else 0.0,
                order_count=len(done),
                complaint_count=0,
            )
        )
    rows.sort(key=lambda r: (-(r.avg_score or 0), -(r.order_count or 0)))
    for idx, row in enumerate(rows, start=1):
        row.rank_no = idx
        existing = db.execute(
            select(StaffPerformanceMonthly).where(
                StaffPerformanceMonthly.staff_id == row.staff_id, StaffPerformanceMonthly.period == period
            )
        ).scalars().first()
        if existing:
            existing.avg_score = row.avg_score
            existing.punctuality_rate = row.punctuality_rate
            existing.anomaly_rate = row.anomaly_rate
            existing.order_count = row.order_count
            existing.complaint_count = row.complaint_count
            existing.rank_no = row.rank_no
        else:
            db.add(row)
    db.commit()
    return len(rows)


# ============================================================
# 12. 上月账单（2 张，含差异）
# ============================================================
def seed_bills(db: Session, orders: list[WorkOrder], elders: list[Elder], users: dict[str, User]) -> dict[str, int]:
    period = (NOW.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    if db.execute(select(Bill).where(Bill.period == period)).scalars().first():
        return {"bills": 0, "details": 0, "diffs": 0}

    elder_map = {e.id: e for e in elders}
    community_orgs = [COMMUNITIES[0], COMMUNITIES[1]]
    completed = [o for o in orders if o.status == OrderStatus.COMPLETED.value]
    bills = details = diffs = 0
    for idx, community in enumerate(community_orgs, start=1):
        org_id = db.execute(select(Org).where(Org.code == community["code"])).scalars().one().id
        picked = [o for o in completed if o.community_org_id == org_id][:8]
        if not picked:
            picked = completed[:8]
        total = Decimal("0")
        subsidy_total = Decimal("0")
        ltc_total = Decimal("0")
        self_pay_total = Decimal("0")
        bill = Bill(
            code=f"B{idx:04d}-{period.replace('-', '')}",
            community_org_id=org_id,
            period=period,
            status=BillStatus.CONFIRMED.value if idx == 1 else BillStatus.PENDING_REVIEW.value,
            created_by=users["orgadmin"].id,
            reviewed_by=users["orgadmin"].id if idx == 1 else None,
            confirmed_at=NOW - timedelta(days=2) if idx == 1 else None,
            diff_ratio=0.008 if idx == 1 else 0.016,
            needs_review=idx != 1,
            spot_check_passed=idx == 1,
            spot_check_ratio=0.1 if idx == 1 else 0.0,
        )
        db.add(bill)
        db.flush()
        bills += 1

        for order in picked:
            elder = elder_map.get(order.elder_id)
            policy = None
            if elder:
                policy = db.execute(
                    select(SubsidyPolicy).where(
                        SubsidyPolicy.ability_level == elder.ability_level,
                        SubsidyPolicy.difficulty_type == elder.difficulty_type,
                        SubsidyPolicy.enabled.is_(True),
                    )
                ).scalars().first()
            amount = _round_money(order.amount or 0)
            subsidy = _round_money(amount * Decimal(str(policy.subsidy_ratio if policy else 0.3)))
            ltc = _round_money(amount * Decimal(str(policy.ltc_ratio if policy else 0.2)))
            self_pay = _round_money(max(Decimal("0"), amount - subsidy - ltc))
            db.add(
                BillDetail(
                    bill_id=bill.id,
                    work_order_id=order.id,
                    elder_id=order.elder_id,
                    price_version_id=order.price_version_id,
                    unit_price=order.unit_price or Decimal("0"),
                    quantity=order.quantity or 1,
                    amount=amount,
                    subsidy_amount=subsidy,
                    ltc_amount=ltc,
                    self_pay_amount=self_pay,
                )
            )
            details += 1
            total += amount
            subsidy_total += subsidy
            ltc_total += ltc
            self_pay_total += self_pay

        bill.total_amount = _round_money(total)
        bill.subsidy_amount = _round_money(subsidy_total)
        bill.ltc_amount = _round_money(ltc_total)
        bill.self_pay_amount = _round_money(self_pay_total)
        bill.diff_amount = _round_money(total * Decimal(str(bill.diff_ratio)))

        if idx == 1:
            for payer_type, amount in (
                ("government", bill.subsidy_amount),
                ("insurance", bill.ltc_amount),
                ("self", bill.self_pay_amount),
            ):
                db.add(
                    PaymentRecord(
                        bill_id=bill.id,
                        payer_type=payer_type,
                        amount=amount,
                        status="paid",
                        paid_at=NOW - timedelta(days=1),
                        remark="演示数据：上月账期统一结算",
                    )
                )
        else:
            elder = elders[3]
            db.add(
                ReconciliationDiff(
                    bill_id=bill.id,
                    work_order_id=picked[0].id if picked else None,
                    elder_id=elder.id,
                    rule_code="duplicate",
                    description="同一工单在同账期出现两次计费明细（重复计费）",
                    amount=Decimal("120.00"),
                    status="open",
                )
            )
            db.add(
                ReconciliationDiff(
                    bill_id=bill.id,
                    work_order_id=picked[1].id if len(picked) > 1 else None,
                    elder_id=elders[5].id,
                    rule_code="duration",
                    description="服务时长超过标准时长 3 倍，疑似虚假计费",
                    amount=Decimal("200.00"),
                    status="open",
                )
            )
            diffs += 2
    db.commit()
    return {"bills": bills, "details": details, "diffs": diffs}


# ============================================================
# 13. 设备 / 照片 / 配置 / 审计 / 变更日志
# ============================================================
def seed_devices(db: Session, elders: list[Elder]) -> int:
    count = 0
    for idx, elder in enumerate(elders[:6], start=1):
        _, created = get_or_create(
            db,
            Device,
            {
                "elder_id": elder.id,
                "device_type": ["band", "watch", "bp"][idx % 3],
                "status": "online",
                "last_seen_at": NOW - timedelta(minutes=RNG.randint(5, 200)),
            },
            device_code=f"DEV{idx:05d}",
        )
        count += int(created)
    db.commit()
    return count


def seed_photos(db: Session, orders: list[WorkOrder]) -> int:
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001 - Pillow 不可用时跳过照片
        return 0
    from app.core.config import settings

    upload_dir = settings.upload_path
    upload_dir.mkdir(parents=True, exist_ok=True)
    colors = [(20, 80, 140), (27, 127, 59), (166, 100, 0), (106, 27, 154)]
    completed = [o for o in orders if o.status == OrderStatus.COMPLETED.value][:3]
    count = 0
    for order in completed:
        for k in range(2):
            filename = f"seed_{order.code}_{k + 1}.jpg"
            path = upload_dir / filename
            if not path.exists():
                img = Image.new("RGB", (960, 640), colors[(order.id + k) % len(colors)])
                draw = ImageDraw.Draw(img)
                draw.text((36, 36), f"{order.code}  service photo {k + 1}", fill=(255, 255, 255))
                draw.rectangle([36, 100, 924, 604], outline=(255, 255, 255), width=4)
                draw.text((60, 130), "DEMO - service evidence", fill=(255, 255, 255))
                img.save(path, "JPEG", quality=82)
            db.add(
                WorkOrderPhoto(
                    work_order_id=order.id,
                    path=filename,
                    size_kb=max(1, path.stat().st_size // 1024),
                    uploaded_at=(order.checkout_at or order.scheduled_start) + timedelta(minutes=5),
                    uploaded_by=order.nurse_id,
                )
            )
            count += 1
    db.commit()
    return count


def seed_config(db: Session, users: dict[str, User]) -> int:
    items = [
        ("dispatch_weights", "0.4,0.3,0.2,0.1", "派单四因子权重（技能/距离/负荷/评分），支持页面热更新"),
        ("order_daily_limit", "6", "护理员当日最大接单量"),
        ("service_radius_km", "3.0", "派单服务半径（公里）"),
        ("alert_cooldown_minutes", "30", "同老人同规则预警冷却分钟数"),
        ("alert_escalate_minutes", "30", "预警超时升级街道分钟数"),
        ("diff_ratio_threshold", "0.01", "账单差异率复核阈值"),
        ("checkin_distance_threshold_m", "200", "签到定位偏差阈值（米）"),
        ("duration_anomaly_minutes", "30", "服务时长偏差异常阈值（分钟）"),
        (SEED_KEY, SEED_VERSION, "演示数据版本（幂等标记，勿删）"),
    ]
    count = 0
    for key, value, description in items:
        _, created = get_or_create(
            db,
            ConfigItem,
            {"value": value, "description": description, "updated_by": users["super"].id},
            key=key,
        )
        count += int(created)
    db.commit()
    return count


def seed_audit(db: Session, users: dict[str, User], orders: list[WorkOrder]) -> int:
    from app.core.audit import write_audit

    if db.execute(select(AuditLog).limit(1)).scalars().first():
        return 0
    sample = orders[:3]
    write_audit(db, user=users["super"], action="login", entity_type="user", entity_id=users["super"].id,
                detail="演示数据：超级管理员登录")
    write_audit(db, user=users["grid1"], action="create", entity_type="elder", entity_id=1,
                detail="演示数据：新增老人档案")
    for order in sample:
        write_audit(db, user=users["grid1"], action="dispatch", entity_type="work_order", entity_id=order.id,
                    after={"nurse_id": order.nurse_id}, detail="演示数据：智能派单")
    write_audit(db, user=users["orgadmin"], action="export", entity_type="elder", detail="演示数据：导出老人档案 CSV")
    write_audit(db, user=users["street"], action="backup", entity_type="system", detail="演示数据：手动备份")
    db.commit()
    return db.execute(select(func.count()).select_from(AuditLog)).scalar() or 0


def seed_change_logs(db: Session, elders: list[Elder], users: dict[str, User]) -> int:
    elder = elders[0]
    count = 0
    entries = [
        (["ability_level"], {"ability_level": "mild"}, {"ability_level": "severe"}, "复评后调整失能等级"),
        (["address_masked"], {"address_masked": "上海市幸福****"}, {"address_masked": "上海市幸福路****"},
         "家属申请更正居住地址"),
    ]
    for fields, before, after, reason in entries:
        exists = db.execute(
            select(ArchiveChangeLog).where(
                ArchiveChangeLog.elder_id == elder.id, ArchiveChangeLog.reason == reason
            )
        ).scalars().first()
        if exists:
            continue
        db.add(
            ArchiveChangeLog(
                elder_id=elder.id,
                changed_by=users["grid1"].id,
                changed_fields=fields,
                before_json=before,
                after_json=after,
                reason=reason,
            )
        )
        count += 1
    db.commit()
    return count


# ============================================================
# 14. 统计输出
# ============================================================
def summary(db: Session) -> dict[str, int]:
    tables = {
        "组织 org": Org,
        "角色 role": Role,
        "权限 permission": Permission,
        "账号 user": User,
        "护理员 staff": Staff,
        "老人 elder": Elder,
        "老人标签 elder_tag": ElderTag,
        "慢病 elder_disease": ElderDisease,
        "用药 elder_medication": ElderMedication,
        "能力评估 ability_assessment": AbilityAssessment,
        "档案变更 archive_change_log": ArchiveChangeLog,
        "服务项目 service_item": ServiceItem,
        "价格版本 service_price_version": ServicePriceVersion,
        "补贴政策 subsidy_policy": SubsidyPolicy,
        "预约 appointment": Appointment,
        "预约日志 appointment_log": AppointmentLog,
        "工单 work_order": WorkOrder,
        "工单项目 work_order_item": WorkOrderItem,
        "工单照片 work_order_photo": WorkOrderPhoto,
        "定位轨迹 work_order_track": WorkOrderTrack,
        "异常单 anomaly_report": AnomalyReport,
        "健康指标 health_metric": HealthMetric,
        "设备 device": Device,
        "预警规则 alert_rule": AlertRule,
        "预警 alert": Alert,
        "预警动作 alert_action": AlertAction,
        "评价 evaluation": Evaluation,
        "回访 callback_task": CallbackTask,
        "投诉 complaint": Complaint,
        "绩效 staff_performance_monthly": StaffPerformanceMonthly,
        "通知 notification": Notification,
        "账单 bill": Bill,
        "账单明细 bill_detail": BillDetail,
        "对账差异 reconciliation_diff": ReconciliationDiff,
        "缴费记录 payment_record": PaymentRecord,
        "审计日志 audit_log": AuditLog,
        "配置项 config_item": ConfigItem,
    }
    return {name: int(db.execute(select(func.count()).select_from(model)).scalar() or 0) for name, model in tables.items()}


# ============================================================
# 主流程
# ============================================================
def run(reset: bool = False, force: bool = False) -> dict[str, int]:
    if reset:
        print("⚠️  删除演示数据库文件并重建 …")
        _reset_database()
    else:
        init_db()

    with SessionLocal() as db:
        marker = db.execute(select(ConfigItem).where(ConfigItem.key == SEED_KEY)).scalars().first()
        seeded = marker is not None
        if seeded and not force:
            print(f"检测到演示数据已初始化（{SEED_KEY}={marker.value}），主数据幂等补齐后跳过交易数据生成。")
            print("如需重新生成交易数据，请执行：python -m scripts.seed --force")
        if not seeded and not force:
            has_txn = bool(db.execute(select(func.count()).select_from(Appointment)).scalar()) or bool(
                db.execute(select(func.count()).select_from(WorkOrder)).scalar()
            )
            if has_txn:
                print("检测到上次未完成的演示数据，先清理交易数据后重建 …")
                _clear_transactional(db)

        orgs = seed_orgs(db)
        roles = seed_rbac(db)
        users = seed_accounts(db, orgs, roles)
        staffs = seed_staff(db, orgs, users)
        items = seed_catalog(db)
        seed_policies(db)
        rule_count = seed_alert_rules(db)
        elders = seed_elders(db, orgs, users)
        devices = seed_devices(db, elders)

        if seeded and not force:
            stats = summary(db)
            _print_summary(stats, accounts=users)
            return stats

        if force:
            _clear_transactional(db)

        appts = seed_appointments(db, elders, items, users)
        orders = seed_orders(db, elders, items, staffs, appts, users)
        metrics = seed_metrics(db, elders)
        alerts = seed_alerts(db, elders, users)
        quality = seed_quality(db, orders, staffs, users)
        performance = seed_performance(db, orders, staffs)
        bills = seed_bills(db, orders, elders, users)
        photos = seed_photos(db, orders)
        configs = seed_config(db, users)
        audit_count = seed_audit(db, users, orders)
        change_logs = seed_change_logs(db, elders, users)

        # 写入幂等标记
        marker = db.execute(select(ConfigItem).where(ConfigItem.key == SEED_KEY)).scalars().first()
        if marker is None:
            marker = ConfigItem(key=SEED_KEY, value=SEED_VERSION, description="演示数据版本（幂等标记，勿删）")
            db.add(marker)
        else:
            marker.value = SEED_VERSION
        db.commit()

        stats = summary(db)
        print(
            f"生成完成：组织 {len(orgs)} / 账号 {len(users)} / 老人 {len(elders)} / 护理员 {len(staffs)} / "
            f"服务项目 {len(items)} / 预约 {len(appts)} / 工单 {len(orders)} / 健康指标 {len(metrics)} / "
            f"预警 {len(alerts)}（规则 {rule_count}）/ 评价 {quality['evaluations']} / 回访 {quality['callbacks']} / "
            f"投诉 {quality['complaints']} / 绩效 {performance} / 账单 {bills['bills']}（明细 {bills['details']}，"
            f"差异 {bills['diffs']}）/ 照片 {photos} / 配置 {configs} / 审计 {audit_count} / 变更日志 {change_logs} / "
            f"设备 {devices}"
        )
        _print_summary(stats, accounts=users)
        return stats


def _clear_transactional(db: Session) -> None:
    """清空交易类演示数据（主数据保留）。"""
    print("清理交易类演示数据 …")
    for model in (
        AlertAction,
        Alert,
        Notification,
        CallbackTask,
        Evaluation,
        Complaint,
        HealthMetric,
        Device,
        WorkOrderPhoto,
        WorkOrderTrack,
        WorkOrderItem,
        AnomalyReport,
        StaffPerformanceMonthly,
        ReconciliationDiff,
        BillDetail,
        PaymentRecord,
        Bill,
        AppointmentLog,
        WorkOrder,
        Appointment,
    ):
        db.query(model).delete()
    db.query(ConfigItem).filter(ConfigItem.key == SEED_KEY).delete()
    db.commit()


def _print_summary(stats: dict[str, int], accounts: dict[str, User]) -> None:
    print("\n=== 各表记录数 ===")
    for name, count in stats.items():
        print(f"  {name:32s} {count:>6d}")
    print("\n=== 演示账号（密码统一 Admin@123）===")
    print("  super / street / orgadmin / grid1 / grid2 / nurse1..nurse8 / elder1..elder20")
    print(f"  已创建账号数：{len(accounts)}")


def _reset_database() -> None:
    """删库重建：SQLite 直接删除数据库文件（避免表间循环外键导致 drop_all 失败）。"""
    if not str(engine.url).startswith("sqlite"):
        raise SystemExit("--reset 仅支持 SQLite 演示库；PostgreSQL 请手工清库后重跑")
    db_path = Path(str(engine.url.database or ""))
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    Base.metadata.create_all(bind=engine)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成社区智慧养老平台演示数据")
    parser.add_argument("--force", action="store_true", help="清空交易数据后重新生成")
    parser.add_argument("--reset", action="store_true", help="删库重建（清空所有表）")
    args = parser.parse_args()
    run(reset=args.reset, force=args.force)


if __name__ == "__main__":
    main()
