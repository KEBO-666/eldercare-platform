"""页面路由（F 模块）：Jinja2 服务端渲染 + 页面内 fetch 调用 /api/v1 接口。

设计原则：
1. 所有页面必须登录：未登录 303 跳转 /login（带 next 回跳）；
2. 首屏数据由服务端直接查库渲染（接口尚未就绪也能演示），接口失败时优雅降级为"暂无数据"；
3. 交互动作（受理/派单/签到/处置/复核等）全部由页面内 fetch 调 API，失败只提示不白屏；
4. 适老化：大字号、高对比、按钮 ≥44px、表格斑马纹、红色预警置顶。
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, false, func, or_, select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.config import BASE_DIR, settings
from app.core.enums import (
    ABILITY_NAMES,
    ALERT_LEVEL_NAMES,
    DIFFICULTY_NAMES,
    ELDER_TAG_NAMES,
    METRIC_NAMES,
    ROLE_NAMES,
    AlertLevel,
    AlertStatus,
    AppointmentStatus,
    BillStatus,
    ComplaintStatus,
    DataScope,
    ItemStatus,
    MetricCode,
    OrderStatus,
    RoleCode,
)
from app.core.security import (
    create_access_token,
    decode_token,
    mask_address,
    mask_id_card,
    mask_name,
    mask_phone,
    verify_password,
)
from app.db.session import get_db
from app.models import (
    AbilityAssessment,
    Alert,
    AlertRule,
    AnomalyReport,
    Appointment,
    AppointmentLog,
    ArchiveChangeLog,
    AuditLog,
    BackupRecord,
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

logger = logging.getLogger("eldercare.web")

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


# ============================================================
# 一、模板过滤器与全局常量
# ============================================================
def _fmt_money(value: Any) -> str:
    try:
        return f"{Decimal(str(value or 0)):,.2f}"
    except (InvalidOperation, TypeError, ValueError):
        return "0.00"


def _fmt_dt(value: Any, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not value:
        return "-"
    if isinstance(value, str):
        return value
    try:
        return value.strftime(fmt)
    except (AttributeError, ValueError):
        return str(value)


def _fmt_date(value: Any) -> str:
    return _fmt_dt(value, "%Y-%m-%d")


def _fmt_short(value: Any) -> str:
    return _fmt_dt(value, "%m-%d %H:%M")


def _stars(value: Any) -> str:
    try:
        score = int(round(float(value or 0)))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(5, score))
    return "★" * score + "☆" * (5 - score)


def _pct(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value or 0) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "0%"


def _photo_url(path: Any) -> str:
    """把上传照片路径转换为可访问 URL（仅取文件名，防止目录穿越）。"""
    if not path:
        return ""
    name = str(path).replace("\\", "/").rstrip("/").split("/")[-1]
    return "/files/" + quote(name)


templates.env.filters.update(
    {
        "money": _fmt_money,
        "dt": _fmt_dt,
        "date": _fmt_date,
        "short": _fmt_short,
        "mask_name": mask_name,
        "mask_phone": mask_phone,
        "mask_id": mask_id_card,
        "mask_addr": mask_address,
        "stars": _stars,
        "pct": _pct,
        "photo_url": _photo_url,
    }
)

_ABILITY = {getattr(k, "value", k): v for k, v in ABILITY_NAMES.items()}
_TAGS = {getattr(k, "value", k): v for k, v in ELDER_TAG_NAMES.items()}
_METRICS = {getattr(k, "value", k): v for k, v in METRIC_NAMES.items()}
_ROLES = {getattr(k, "value", k): v for k, v in ROLE_NAMES.items()}
_ALERT_LEVELS = {getattr(k, "value", k): v for k, v in ALERT_LEVEL_NAMES.items()}
_DIFFICULTY = {getattr(k, "value", k): v for k, v in DIFFICULTY_NAMES.items()}

APPOINTMENT_STATUS_NAMES = {
    "pending_accept": "待受理",
    "accepted": "已受理",
    "pending_dispatch": "待派单",
    "dispatched": "已派单",
    "in_service": "服务中",
    "completed": "已完成",
    "cancelled": "已取消",
    "no_show": "已爽约",
}
APPOINTMENT_STATUS_BADGE = {
    "pending_accept": "badge-yellow",
    "accepted": "badge-blue",
    "pending_dispatch": "badge-yellow",
    "dispatched": "badge-blue",
    "in_service": "badge-purple",
    "completed": "badge-green",
    "cancelled": "badge-gray",
    "no_show": "badge-gray",
}
ORDER_STATUS_NAMES = {
    "pending_accept": "待接单",
    "accepted": "已接单",
    "in_service": "服务中",
    "pending_evaluation": "待评价",
    "completed": "已完成",
    "anomaly": "异常",
    "cancelled": "已取消",
}
ORDER_STATUS_BADGE = {
    "pending_accept": "badge-yellow",
    "accepted": "badge-blue",
    "in_service": "badge-purple",
    "pending_evaluation": "badge-yellow",
    "completed": "badge-green",
    "anomaly": "badge-red",
    "cancelled": "badge-gray",
}
ALERT_STATUS_NAMES = {
    "pending": "待处置",
    "resolved": "已处置",
    "false_alarm": "误报",
    "referred": "已转诊",
}
BILL_STATUS_NAMES = {
    "draft": "草稿",
    "pending_review": "待复核",
    "confirmed": "已确认",
    "paid": "已缴费",
    "reversed": "已红冲",
}
BILL_STATUS_BADGE = {
    "draft": "badge-gray",
    "pending_review": "badge-yellow",
    "confirmed": "badge-blue",
    "paid": "badge-green",
    "reversed": "badge-red",
}
COMPLAINT_STATUS_NAMES = {
    "new": "待受理",
    "assigned": "已指派",
    "reviewing": "复核中",
    "concluded": "已结案",
    "archived": "已归档",
}
CALLBACK_STATUS_NAMES = {"pending": "待回访", "done": "已完成"}
DIFF_STATUS_NAMES = {"open": "待处理", "explained": "已说明", "handled": "已处理"}
GENDER_NAMES = {"male": "男", "female": "女", "unknown": "未知"}
LEVEL_RANK = {"blue": 1, "yellow": 2, "red": 3}
ANOMALY_TYPE_NAMES = {
    "not_home": "老人不在家",
    "refused": "拒绝服务",
    "sudden_illness": "突发不适",
    "other": "其他",
}

templates.env.globals.update(
    ABILITY_NAMES=_ABILITY,
    TAG_NAMES=_TAGS,
    METRIC_NAMES=_METRICS,
    ROLE_NAMES=_ROLES,
    ALERT_LEVEL_NAMES=_ALERT_LEVELS,
    DIFFICULTY_NAMES=_DIFFICULTY,
    APPOINTMENT_STATUS_NAMES=APPOINTMENT_STATUS_NAMES,
    APPOINTMENT_STATUS_BADGE=APPOINTMENT_STATUS_BADGE,
    ORDER_STATUS_NAMES=ORDER_STATUS_NAMES,
    ORDER_STATUS_BADGE=ORDER_STATUS_BADGE,
    ALERT_STATUS_NAMES=ALERT_STATUS_NAMES,
    BILL_STATUS_NAMES=BILL_STATUS_NAMES,
    BILL_STATUS_BADGE=BILL_STATUS_BADGE,
    COMPLAINT_STATUS_NAMES=COMPLAINT_STATUS_NAMES,
    CALLBACK_STATUS_NAMES=CALLBACK_STATUS_NAMES,
    DIFF_STATUS_NAMES=DIFF_STATUS_NAMES,
    GENDER_NAMES=GENDER_NAMES,
    LEVEL_RANK=LEVEL_RANK,
    ANOMALY_TYPE_NAMES=ANOMALY_TYPE_NAMES,
)

ALL_ROLES = ("elder", "nurse", "grid", "org_admin", "street", "super")


# ============================================================
# 二、通用工具：登录态、导航、查询、渲染
# ============================================================
def page_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """从 Cookie 解析页面登录态；无效/过期返回 None（不抛异常）。"""
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = decode_token(token)
        user = db.get(User, int(payload.get("sub", 0)))
    except Exception:  # noqa: BLE001 - 任何非法 token 都视为未登录
        return None
    if user is None or not user.is_active:
        return None
    return user


def _role_code(user: User) -> str:
    return user.role.code if user.role else RoleCode.ELDER.value


def _perm_codes(user: User) -> set[str]:
    try:
        from app.core.deps import permission_codes

        return set(permission_codes(user))
    except Exception:  # noqa: BLE001 - 模块异常时降级为无权限展示
        return set()


def _has_perm(user: User | None, code: str) -> bool:
    if user is None:
        return False
    perms = _perm_codes(user)
    return "*" in perms or code in perms


def _user_view(user: User) -> dict[str, Any]:
    role = user.role
    org = user.org
    return {
        "id": user.id,
        "username": user.username,
        "real_name": user.real_name,
        "role_code": _role_code(user),
        "role_name": _ROLES.get(_role_code(user), role.name if role else ""),
        "org_id": user.org_id,
        "org_name": org.name if org else "社区养老服务中心",
        "data_scope": user.data_scope,
    }


def _login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    query = request.url.query
    url = "/login"
    if target and target != "/login":
        back = target + (f"?{query}" if query else "")
        url = "/login?next=" + quote(back, safe="")
    return RedirectResponse(url, status_code=303)


def _render(request: Request, template: str, context: dict[str, Any], status_code: int = 200) -> Response:
    """渲染模板，任何异常都降级为友好错误页（页面不出现裸 500）。"""
    try:
        return templates.TemplateResponse(
            request=request, name=template, context=context, status_code=status_code
        )
    except Exception:  # noqa: BLE001
        logger.exception("页面渲染失败: %s", template)
        return HTMLResponse(
            "<!DOCTYPE html><html lang='zh-CN'><meta charset='utf-8'>"
            "<body style='font-family:Microsoft YaHei;font-size:18px;padding:40px'>"
            "<h1 style='color:#14508c'>页面数据加载异常</h1>"
            "<p>该页面暂时无法展示，请稍后重试或返回 <a href='/'>首页</a>。</p>"
            "</body></html>",
            status_code=status_code,
        )


def _error_page(
    request: Request, user: User | None, title: str, message: str, status_code: int = 404
) -> Response:
    ctx = _base_ctx(request, user, "home", title, notice=message, notice_type="danger")
    ctx["page_title"] = title
    return _render(request, "errors/error.html", ctx, status_code=status_code)


def _base_ctx(
    request: Request,
    user: User | None,
    active: str,
    page_title: str,
    breadcrumb: str = "",
    notice: str = "",
    notice_type: str = "",
    **extra: Any,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "request": request,
        "app_name": settings.app_name,
        "version": settings.version,
        "user": _user_view(user) if user else {},
        "nav_items": _build_nav(user),
        "active": active,
        "page_title": page_title,
        "breadcrumb": breadcrumb,
        "notice": notice,
        "notice_type": notice_type,
        "now": datetime.now(),
        "perms": _perm_codes(user) if user else set(),
    }
    ctx.update(extra)
    return ctx


NAV_DEFS: list[dict[str, Any]] = [
    {"key": "home", "label": "首页看板", "url": "/", "icon": "🏠", "roles": ALL_ROLES},
    {"divider": True, "label": "档案与服务", "roles": ALL_ROLES},
    {
        "key": "elders",
        "label": "老人档案",
        "url": "/elders",
        "icon": "👵",
        "roles": ("nurse", "grid", "org_admin", "street", "super"),
    },
    {
        "key": "appointments",
        "label": "预约服务",
        "labels": {"elder": "我的预约"},
        "url": "/appointments",
        "icon": "📅",
        "roles": ALL_ROLES,
    },
    {"key": "dispatch", "label": "智能派单", "url": "/dispatch", "icon": "🧭", "roles": ("grid", "super")},
    {
        "key": "orders",
        "label": "工单管理",
        "labels": {"elder": "我的工单", "nurse": "我的工单"},
        "url": "/orders",
        "icon": "🧰",
        "roles": ALL_ROLES,
    },
    {"divider": True, "label": "健康与预警", "roles": ALL_ROLES},
    {
        "key": "health",
        "label": "健康监测",
        "labels": {"elder": "我的健康", "nurse": "健康录入"},
        "url": "/monitoring",
        "icon": "❤️",
        "roles": ALL_ROLES,
    },
    {"key": "alerts", "label": "预警看板", "url": "/alerts", "icon": "🚨", "roles": ALL_ROLES},
    {"divider": True, "label": "质量与费用", "roles": ALL_ROLES},
    {
        "key": "quality",
        "label": "评价投诉",
        "labels": {"elder": "评价与建议"},
        "url": "/quality",
        "icon": "⭐",
        "roles": ALL_ROLES,
    },
    {
        "key": "billing",
        "label": "费用账单",
        "labels": {"elder": "我的账单"},
        "url": "/billing",
        "icon": "🧾",
        "roles": ("elder", "grid", "org_admin", "street", "super"),
    },
    {
        "key": "stats",
        "label": "统计看板",
        "url": "/stats",
        "icon": "📊",
        "roles": ("grid", "org_admin", "street", "super"),
    },
    {"divider": True, "label": "系统管理", "roles": ("org_admin", "street", "super")},
    {
        "key": "admin_users",
        "label": "用户管理",
        "url": "/admin/users",
        "icon": "👥",
        "roles": ("org_admin", "super"),
    },
    {"key": "admin_orgs", "label": "组织管理", "url": "/admin/orgs", "icon": "🏢", "roles": ("super",)},
    {
        "key": "admin_audit",
        "label": "审计日志",
        "url": "/admin/audit",
        "icon": "🛡️",
        "roles": ("street", "super"),
    },
    {
        "key": "admin_backup",
        "label": "备份恢复",
        "url": "/admin/backup",
        "icon": "💾",
        "roles": ("street", "super"),
    },
    {"key": "admin_config", "label": "系统配置", "url": "/admin/config", "icon": "⚙️", "roles": ("super",)},
]


def _build_nav(user: User | None) -> list[dict[str, Any]]:
    if user is None:
        return []
    role = _role_code(user)
    items: list[dict[str, Any]] = []
    for item in NAV_DEFS:
        if role not in item["roles"]:
            continue
        if item.get("divider"):
            items.append({"divider": True, "label": item["label"]})
            continue
        label = (item.get("labels") or {}).get(role) or item["label"]
        items.append(
            {"key": item["key"], "label": label, "url": item["url"], "icon": item["icon"], "divider": False}
        )
    # 去掉连续/末尾分隔线
    cleaned: list[dict[str, Any]] = []
    for entry in items:
        if entry.get("divider") and (not cleaned or cleaned[-1].get("divider")):
            continue
        cleaned.append(entry)
    while cleaned and cleaned[-1].get("divider"):
        cleaned.pop()
    return cleaned


# ---------------- 查询辅助（全部容错，避免页面 500） ----------------
def _scoped(stmt: Any, model: Any, user: User, db: Session) -> Any:
    try:
        from app.core.deps import scope_condition

        cond = scope_condition(model, user, db)
        if cond is not None:
            stmt = stmt.where(cond)
    except Exception:  # noqa: BLE001
        logger.exception("数据范围过滤失败，拒绝返回数据")
        stmt = stmt.where(false())
    return stmt


def _visible_elder_ids(db: Session, user: User) -> list[int] | None:
    try:
        from app.core.deps import visible_elder_ids

        return visible_elder_ids(db, user)
    except Exception:  # noqa: BLE001
        return []


def _rows(db: Session, stmt: Any) -> list[Any]:
    try:
        return list(db.execute(stmt).scalars().all())
    except Exception:  # noqa: BLE001
        logger.exception("页面查询失败（已降级为空列表）")
        return []


def _scalar(db: Session, stmt: Any, default: Any = 0) -> Any:
    try:
        value = db.execute(stmt).scalar()
        return default if value is None else value
    except Exception:  # noqa: BLE001
        logger.exception("页面统计查询失败（已降级为默认值）")
        return default


def _count(db: Session, model: Any, *conds: Any) -> int:
    stmt = select(func.count()).select_from(model)
    for cond in conds:
        stmt = stmt.where(cond)
    try:
        return int(db.execute(stmt).scalar() or 0)
    except Exception:  # noqa: BLE001
        logger.exception("页面计数失败（已降级为 0）")
        return 0


def _scoped_count(db: Session, model: Any, user: User, *conds: Any) -> int:
    stmt = select(func.count()).select_from(model)
    for cond in conds:
        stmt = stmt.where(cond)
    stmt = _scoped(stmt, model, user, db)
    try:
        return int(db.execute(stmt).scalar() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _paged(db: Session, stmt: Any, page: int, size: int = 20) -> tuple[list[Any], int, int, int]:
    page = max(1, int(page or 1))
    size = max(5, min(100, int(size or 20)))
    try:
        total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0)
        rows = list(db.execute(stmt.offset((page - 1) * size).limit(size)).scalars().all())
    except Exception:  # noqa: BLE001
        logger.exception("分页查询失败（已降级为空列表）")
        return [], 0, page, 0
    pages = (total + size - 1) // size if size else 0
    return rows, total, page, pages


def _id_name_map(db: Session, model: Any, ids: set[int] | None = None) -> dict[int, Any]:
    stmt = select(model)
    if ids is not None:
        if not ids:
            return {}
        stmt = stmt.where(model.id.in_(ids))
    rows = _rows(db, stmt)
    return {row.id: row for row in rows}


def _staff_map(db: Session) -> dict[int, Staff]:
    return {s.id: s for s in _rows(db, select(Staff))}


def _user_name_map(db: Session) -> dict[int, str]:
    return {u.id: u.real_name for u in _rows(db, select(User))}


def _elder_map(db: Session, elders: list[Elder]) -> dict[int, Elder]:
    return {e.id: e for e in elders}


def _orgs_by_level(db: Session, level: str) -> list[Org]:
    return _rows(db, select(Org).where(Org.level == level).order_by(Org.sort.asc(), Org.id.asc()))


# ============================================================
# 三、登录 / 登出
# ============================================================
@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next: str = "/",
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is not None:
        return RedirectResponse(next or "/", status_code=303)
    return _render(
        request,
        "auth/login.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "version": settings.version,
            "next_url": next or "/",
            "error": "",
        },
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
    db: Session = Depends(get_db),
) -> Response:
    username = (username or "").strip()
    target = next if (next or "").startswith("/") else "/"
    if not username or not password:
        return _render(
            request,
            "auth/login.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "version": settings.version,
                "next_url": target,
                "username": username,
                "error": "请输入用户名和密码",
            },
            status_code=400,
        )

    user = None
    try:
        user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        logger.exception("登录查询用户失败")

    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        try:
            write_audit(
                db,
                user=type("_Anonymous", (), {"id": None, "username": username})(),
                action="login_fail",
                entity_type="user",
                detail=f"页面登录失败：{username}",
                request=request,
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        return _render(
            request,
            "auth/login.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "version": settings.version,
                "next_url": target,
                "username": username,
                "error": "用户名或密码错误，或账号已被停用",
            },
            status_code=401,
        )

    try:
        user.last_login_at = datetime.now()
        write_audit(
            db,
            user=user,
            action="login",
            entity_type="user",
            entity_id=user.id,
            detail=f"页面登录成功：{user.username}",
            request=request,
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()

    token = create_access_token(
        user.id, {"role": _role_code(user), "username": user.username, "real_name": user.real_name}
    )
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(
        "access_token",
        token,
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
        httponly=True,
        samesite="lax",
    )
    return resp


@router.get("/logout")
@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("access_token", path="/")
    return resp


# ============================================================
# 四、首页看板
# ============================================================
def _daily_buckets(rows: list[Any], days: int, field: str, start: datetime) -> tuple[list[str], list[int]]:
    labels = [(start + timedelta(days=i)).strftime("%m-%d") for i in range(days)]
    key_index = {(start + timedelta(days=i)).date(): i for i in range(days)}
    counts = [0] * days
    for row in rows:
        value = getattr(row, field, None)
        if not value:
            continue
        idx = key_index.get(value.date())
        if idx is not None:
            counts[idx] += 1
    return labels, counts


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)

    role = _role_code(user)
    now = datetime.now()
    start_30d = now - timedelta(days=30)
    days = 14
    start_14d = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    # ---- 基础统计 ----
    orders_30d = _scoped_count(
        db, WorkOrder, user, WorkOrder.scheduled_start >= start_30d, WorkOrder.status == OrderStatus.COMPLETED.value
    )
    todo_appointments = _scoped_count(
        db,
        Appointment,
        user,
        Appointment.status.in_(
            [
                AppointmentStatus.PENDING_ACCEPT.value,
                AppointmentStatus.ACCEPTED.value,
                AppointmentStatus.PENDING_DISPATCH.value,
            ]
        ),
    )
    todo_orders = _scoped_count(
        db,
        WorkOrder,
        user,
        WorkOrder.status.in_(
            [OrderStatus.PENDING_ACCEPT.value, OrderStatus.ACCEPTED.value, OrderStatus.IN_SERVICE.value]
        ),
    )
    alert_ids = _visible_elder_ids(db, user)
    alert_conds: list[Any] = []
    if alert_ids is not None:
        alert_conds.append(Alert.elder_id.in_(alert_ids or [-1]))
    pending_alerts = _count(db, Alert, Alert.status == AlertStatus.PENDING.value, *alert_conds)
    red_alerts = _count(
        db,
        Alert,
        Alert.status == AlertStatus.PENDING.value,
        Alert.level == AlertLevel.RED.value,
        *alert_conds,
    )
    last_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    bill_total = _scalar(
        db,
        select(func.coalesce(func.sum(Bill.total_amount), 0)).where(Bill.period == last_month),
        0,
    )
    bill_unpaid = _scalar(
        db,
        select(func.coalesce(func.sum(Bill.self_pay_amount), 0)).where(
            Bill.period == last_month, Bill.status.in_([BillStatus.CONFIRMED.value, BillStatus.PENDING_REVIEW.value])
        ),
        0,
    )
    elders_total = _scoped_count(db, Elder, user, Elder.is_deleted == 0)

    cards: list[dict[str, Any]] = []
    if role in ("elder", "nurse"):
        cards = [
            {
                "label": "近30天服务次数",
                "value": orders_30d,
                "hint": "已完成上门服务",
                "icon": "🧰",
                "url": "/orders",
            },
            {
                "label": "待办事项",
                "value": todo_appointments + todo_orders,
                "hint": f"预约 {todo_appointments} · 工单 {todo_orders}",
                "icon": "⏳",
                "url": "/orders",
                "cls": "warn" if (todo_appointments + todo_orders) else "",
            },
            {
                "label": "待处置预警",
                "value": pending_alerts,
                "hint": f"其中红色 {red_alerts} 条",
                "icon": "🚨",
                "url": "/alerts",
                "cls": "danger" if red_alerts else "",
            },
            {
                "label": "上月账单金额",
                "value": _fmt_money(bill_total),
                "hint": f"个人自付 {_fmt_money(bill_unpaid)} 元",
                "icon": "🧾",
                "url": "/billing",
                "cls": "ok",
            },
        ]
    else:
        cards = [
            {
                "label": "近30天服务量",
                "value": orders_30d,
                "hint": "已完成工单数",
                "icon": "🧰",
                "url": "/orders",
            },
            {
                "label": "待办事项",
                "value": todo_appointments + todo_orders,
                "hint": f"预约 {todo_appointments} · 工单 {todo_orders}",
                "icon": "⏳",
                "url": "/appointments",
                "cls": "warn",
            },
            {
                "label": "待处置预警",
                "value": pending_alerts,
                "hint": f"红色 {red_alerts} 条（5 分钟内响应）",
                "icon": "🚨",
                "url": "/alerts",
                "cls": "danger" if red_alerts else "",
            },
            {
                "label": "上月账单金额",
                "value": _fmt_money(bill_total),
                "hint": f"{last_month} · 待缴费 {_fmt_money(bill_unpaid)} 元",
                "icon": "🧾",
                "url": "/billing",
                "cls": "ok",
            },
        ]

    # ---- 趋势图（近 14 天） ----
    order_rows = _rows(
        db,
        _scoped(
            select(WorkOrder).where(WorkOrder.scheduled_start >= start_14d),
            WorkOrder,
            user,
            db,
        ),
    )
    appt_rows = _rows(
        db,
        _scoped(
            select(Appointment).where(Appointment.desired_start >= start_14d),
            Appointment,
            user,
            db,
        ),
    )
    labels, order_counts = _daily_buckets(order_rows, days, "scheduled_start", start_14d)
    _, appt_counts = _daily_buckets(appt_rows, days, "desired_start", start_14d)
    trend_spec = {
        "type": "line",
        "x": labels,
        "area": True,
        "series": [
            {"name": "工单量", "data": order_counts, "color": "#14508c"},
            {"name": "预约量", "data": appt_counts, "color": "#c62828"},
        ],
    }

    status_counter = Counter(getattr(o, "status", "") for o in order_rows)
    pie_spec = {
        "type": "pie",
        "pie": [
            {"name": ORDER_STATUS_NAMES.get(status, status or "未知"), "value": count}
            for status, count in status_counter.most_common()
        ]
        or [{"name": "暂无数据", "value": 1}],
    }

    # ---- 待办与预警 ----
    recent_alerts = _rows(db, select(Alert).where(Alert.status == AlertStatus.PENDING.value, *alert_conds).order_by(Alert.id.desc()).limit(6))
    recent_alerts.sort(key=lambda a: (LEVEL_RANK.get(a.level, 0), a.id), reverse=True)
    alert_elder_ids = {a.elder_id for a in recent_alerts}
    alert_elders = _elder_map(db, _rows(db, select(Elder).where(Elder.id.in_(alert_elder_ids)))) if alert_elder_ids else {}

    pending_appts = _rows(
        db,
        _scoped(
            select(Appointment)
            .where(Appointment.status.in_([AppointmentStatus.PENDING_ACCEPT.value, AppointmentStatus.ACCEPTED.value]))
            .order_by(Appointment.desired_start.asc())
            .limit(5),
            Appointment,
            user,
            db,
        ),
    )
    appt_elder_ids = {a.elder_id for a in pending_appts}
    appt_elders = _elder_map(db, _rows(db, select(Elder).where(Elder.id.in_(appt_elder_ids)))) if appt_elder_ids else {}
    item_ids = {a.service_item_id for a in pending_appts}
    appt_items = _id_name_map(db, ServiceItem, item_ids) if item_ids else {}

    pending_order_rows = _rows(
        db,
        _scoped(
            select(WorkOrder)
            .where(
                WorkOrder.status.in_(
                    [OrderStatus.PENDING_ACCEPT.value, OrderStatus.ACCEPTED.value, OrderStatus.IN_SERVICE.value]
                )
            )
            .order_by(WorkOrder.scheduled_start.asc())
            .limit(5),
            WorkOrder,
            user,
            db,
        ),
    )
    wo_elder_ids = {o.elder_id for o in pending_order_rows}
    wo_elders = _elder_map(db, _rows(db, select(Elder).where(Elder.id.in_(wo_elder_ids)))) if wo_elder_ids else {}
    wo_item_ids = {o.service_item_id for o in pending_order_rows}
    wo_items = _id_name_map(db, ServiceItem, wo_item_ids) if wo_item_ids else {}

    ctx = _base_ctx(
        request,
        user,
        "home",
        f"{_ROLES.get(role, '用户')}看板",
        breadcrumb="首页 / 数据看板",
        cards=cards,
        trend_json=json.dumps(trend_spec, ensure_ascii=False),
        pie_json=json.dumps(pie_spec, ensure_ascii=False),
        recent_alerts=recent_alerts,
        alert_elders=alert_elders,
        pending_appts=pending_appts,
        appt_elders=appt_elders,
        appt_items=appt_items,
        pending_orders=pending_order_rows,
        wo_elders=wo_elders,
        wo_items=wo_items,
        elders_total=elders_total,
        red_alerts=red_alerts,
        last_month=last_month,
        role=role,
    )
    return _render(request, "index.html", ctx)


# ============================================================
# 五、老人档案
# ============================================================
@router.get("/elders", response_class=HTMLResponse)
def elder_list(
    request: Request,
    keyword: str = "",
    ability_level: str = "",
    tag: str = "",
    grid_id: int | None = None,
    community_id: int | None = None,
    page: int = 1,
    size: int = 20,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)

    stmt = select(Elder).where(Elder.is_deleted == 0)
    stmt = _scoped(stmt, Elder, user, db)
    keyword = (keyword or "").strip()
    if keyword:
        stmt = stmt.where(or_(Elder.name.contains(keyword), Elder.code.contains(keyword)))
    if ability_level:
        stmt = stmt.where(Elder.ability_level == ability_level)
    if tag:
        stmt = stmt.where(Elder.id.in_(select(ElderTag.elder_id).where(ElderTag.tag == tag)))
    if grid_id:
        stmt = stmt.where(Elder.grid_org_id == grid_id)
    if community_id:
        stmt = stmt.where(Elder.community_org_id == community_id)
    stmt = stmt.order_by(Elder.id.asc())
    rows, total, page, pages = _paged(db, stmt, page, size)

    tag_rows = _rows(db, select(ElderTag).where(ElderTag.elder_id.in_([e.id for e in rows] or [-1])))
    tag_map: dict[int, list[str]] = {}
    for t in tag_rows:
        tag_map.setdefault(t.elder_id, []).append(t.tag)

    communities = _orgs_by_level(db, "community")
    grids = _orgs_by_level(db, "grid")
    all_orgs = _rows(db, select(Org))
    org_map = {o.id: o.name for o in all_orgs}

    ctx = _base_ctx(
        request,
        user,
        "elders",
        "老人档案",
        breadcrumb="首页 / 老人档案",
        elders=rows,
        tag_map=tag_map,
        org_map=org_map,
        total=total,
        page=page,
        pages=pages,
        size=size,
        communities=communities,
        grids=grids,
        filters={
            "keyword": keyword,
            "ability_level": ability_level,
            "tag": tag,
            "grid_id": grid_id,
            "community_id": community_id,
        },
        can_write=_has_perm(user, "elder:write"),
        can_export=_has_perm(user, "elder:export"),
    )
    return _render(request, "elders/list.html", ctx)


@router.get("/elders/{elder_id}", response_class=HTMLResponse)
def elder_detail(
    request: Request,
    elder_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)

    elder = None
    try:
        elder = db.get(Elder, elder_id)
    except Exception:  # noqa: BLE001
        logger.exception("查询老人失败")
    if elder is None or elder.is_deleted:
        return _error_page(request, user, "档案不存在", "未找到该老人档案，可能已被删除。")

    visible = _visible_elder_ids(db, user)
    if visible is not None and elder_id not in visible:
        return _error_page(request, user, "无权查看", "该档案不在您的数据范围内（越权访问已拦截）。", status_code=403)

    tags = _rows(db, select(ElderTag).where(ElderTag.elder_id == elder_id))
    diseases = _rows(db, select(ElderDisease).where(ElderDisease.elder_id == elder_id))
    meds = _rows(db, select(ElderMedication).where(ElderMedication.elder_id == elder_id))
    assessments = _rows(
        db, select(AbilityAssessment).where(AbilityAssessment.elder_id == elder_id).order_by(AbilityAssessment.id.desc())
    )
    changes = _rows(
        db, select(ArchiveChangeLog).where(ArchiveChangeLog.elder_id == elder_id).order_by(ArchiveChangeLog.id.desc()).limit(20)
    )
    user_names = _user_name_map(db)

    appts = _rows(
        db,
        select(Appointment)
        .where(Appointment.elder_id == elder_id)
        .order_by(Appointment.desired_start.desc())
        .limit(8),
    )
    item_ids = {a.service_item_id for a in appts}
    appt_items = _id_name_map(db, ServiceItem, item_ids) if item_ids else {}
    orders = _rows(
        db,
        select(WorkOrder).where(WorkOrder.elder_id == elder_id).order_by(WorkOrder.scheduled_start.desc()).limit(8),
    )
    order_item_ids = {o.service_item_id for o in orders}
    order_items = _id_name_map(db, ServiceItem, order_item_ids) if order_item_ids else {}
    staffs = _staff_map(db)
    org = None
    try:
        org = db.get(Org, elder.community_org_id)
        grid = db.get(Org, elder.grid_org_id) if elder.grid_org_id else None
    except Exception:  # noqa: BLE001
        grid = None

    ctx = _base_ctx(
        request,
        user,
        "elders",
        f"档案详情 · {elder.name[:1]}**",
        breadcrumb=f"首页 / 老人档案 / {elder.code}",
        elder=elder,
        tags=tags,
        tag_map={elder.id: [t.tag for t in tags]},
        diseases=diseases,
        meds=meds,
        assessments=assessments,
        changes=changes,
        user_names=user_names,
        appts=appts,
        appt_items=appt_items,
        orders=orders,
        order_items=order_items,
        staffs=staffs,
        community=org,
        grid=grid,
        can_grant=_has_perm(user, "elder:grant"),
        can_write=_has_perm(user, "elder:write"),
    )
    return _render(request, "elders/detail.html", ctx)


# ============================================================
# 六、预约
# ============================================================
@router.get("/appointments", response_class=HTMLResponse)
def appointment_list(
    request: Request,
    status: str = "",
    elder_id: int | None = None,
    page: int = 1,
    size: int = 20,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)

    stmt = _scoped(select(Appointment), Appointment, user, db)
    if status:
        stmt = stmt.where(Appointment.status == status)
    if elder_id:
        stmt = stmt.where(Appointment.elder_id == elder_id)
    stmt = stmt.order_by(Appointment.desired_start.desc(), Appointment.id.desc())
    rows, total, page, pages = _paged(db, stmt, page, size)

    elder_ids = {a.elder_id for a in rows}
    elders = _elder_map(db, _rows(db, select(Elder).where(Elder.id.in_(elder_ids)))) if elder_ids else {}
    item_ids = {a.service_item_id for a in rows}
    items = _id_name_map(db, ServiceItem, item_ids) if item_ids else {}
    user_names = _user_name_map(db)

    ctx = _base_ctx(
        request,
        user,
        "appointments",
        "预约服务" if _role_code(user) != "elder" else "我的预约",
        breadcrumb="首页 / 预约服务",
        appointments=rows,
        elders=elders,
        items=items,
        user_names=user_names,
        total=total,
        page=page,
        pages=pages,
        size=size,
        status=status,
        elder_id=elder_id,
        can_accept=_has_perm(user, "appointment:accept"),
        can_cancel=_has_perm(user, "appointment:cancel"),
        can_create=_has_perm(user, "appointment:create"),
        can_dispatch=_has_perm(user, "order:dispatch"),
        cancel_free_hours=settings.cancel_free_hours,
    )
    return _render(request, "appointments/list.html", ctx)


@router.get("/appointments/new", response_class=HTMLResponse)
def appointment_new(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)

    role = _role_code(user)
    elder_stmt = _scoped(select(Elder).where(Elder.is_deleted == 0), Elder, user, db)
    elder_options = _rows(db, elder_stmt.order_by(Elder.id.asc()).limit(300))
    my_elder = None
    if role == "elder":
        from app.core.deps import get_elder_of_user

        my_elder = get_elder_of_user(db, user)
        elder_options = [my_elder] if my_elder is not None and not my_elder.is_deleted else []
        my_elder = elder_options[0] if elder_options else None
    items = _rows(db, select(ServiceItem).where(
        ServiceItem.status == ItemStatus.ON.value, ServiceItem.is_deleted == 0,
    ).order_by(ServiceItem.id.asc()))
    prices: dict[int, Any] = {}
    version_ids = {i.current_price_version_id for i in items if i.current_price_version_id}
    if version_ids:
        versions = _rows(db, select(ServicePriceVersion).where(ServicePriceVersion.id.in_(version_ids)))
        prices = {v.id: v for v in versions}

    ctx = _base_ctx(
        request,
        user,
        "appointments",
        "提交预约",
        breadcrumb="首页 / 预约服务 / 提交预约",
        elders=elder_options,
        my_elder=my_elder,
        items=items,
        prices=prices,
        role=role,
        cancel_free_hours=settings.cancel_free_hours,
    )
    return _render(request, "appointments/new.html", ctx)


# ============================================================
# 七、智能派单
# ============================================================
@router.get("/dispatch", response_class=HTMLResponse)
def dispatch_board(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)
    if not _has_perm(user, "order:dispatch"):
        return _error_page(request, user, "无权访问", "您没有派单权限。", status_code=403)

    pending = _rows(
        db,
        _scoped(
            select(Appointment)
            .where(
                Appointment.status.in_(
                    [
                        AppointmentStatus.PENDING_DISPATCH.value,
                        AppointmentStatus.ACCEPTED.value,
                        AppointmentStatus.PENDING_ACCEPT.value,
                    ]
                )
            )
            .order_by(Appointment.desired_start.asc())
            .limit(60),
            Appointment,
            user,
            db,
        ),
    )
    elder_ids = {a.elder_id for a in pending}
    elders = _elder_map(db, _rows(db, select(Elder).where(Elder.id.in_(elder_ids)))) if elder_ids else {}
    item_ids = {a.service_item_id for a in pending}
    items = _id_name_map(db, ServiceItem, item_ids) if item_ids else {}
    staffs = _rows(db, select(Staff).order_by(Staff.id.asc()))
    nurses = []
    for s in staffs:
        nurses.append(
            {
                "id": s.id,
                "name": s.name,
                "employee_no": s.employee_no,
                "skills": s.skills or [],
                "rating": s.rating_cached or 0,
                "order_count": s.order_count_cached or 0,
                "is_focus": s.is_focus,
                "org_id": s.org_id,
            }
        )

    # 当日已派单数
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_dispatched = _scoped_count(db, WorkOrder, user, WorkOrder.scheduled_start >= today)
    in_dispatch = _scoped_count(
        db, Appointment, user, Appointment.status == AppointmentStatus.PENDING_DISPATCH.value
    )

    ctx = _base_ctx(
        request,
        user,
        "dispatch",
        "智能派单",
        breadcrumb="首页 / 智能派单",
        appointments=pending,
        elders=elders,
        items=items,
        nurses=nurses,
        nurses_json=json.dumps(nurses, ensure_ascii=False),
        today_dispatched=today_dispatched,
        in_dispatch=in_dispatch,
        weights=settings.dispatch_weights,
        top_n=settings.dispatch_top_n,
        can_reassign=_has_perm(user, "order:reassign"),
    )
    return _render(request, "dispatch/index.html", ctx)


# ============================================================
# 八、工单
# ============================================================
@router.get("/orders", response_class=HTMLResponse)
def order_list(
    request: Request,
    status: str = "",
    elder_id: int | None = None,
    page: int = 1,
    size: int = 20,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)

    stmt = _scoped(select(WorkOrder), WorkOrder, user, db)
    if status:
        stmt = stmt.where(WorkOrder.status == status)
    if elder_id:
        stmt = stmt.where(WorkOrder.elder_id == elder_id)
    stmt = stmt.order_by(WorkOrder.scheduled_start.desc(), WorkOrder.id.desc())
    rows, total, page, pages = _paged(db, stmt, page, size)

    elder_ids = {o.elder_id for o in rows}
    elders = _elder_map(db, _rows(db, select(Elder).where(Elder.id.in_(elder_ids)))) if elder_ids else {}
    item_ids = {o.service_item_id for o in rows}
    items = _id_name_map(db, ServiceItem, item_ids) if item_ids else {}
    staffs = _staff_map(db)
    nurse_ids = {o.nurse_id for o in rows if o.nurse_id}
    nurse_staffs = {sid: staffs[sid] for sid in nurse_ids if sid in staffs}

    ctx = _base_ctx(
        request,
        user,
        "orders",
        "工单管理" if _role_code(user) not in ("elder", "nurse") else "我的工单",
        breadcrumb="首页 / 工单",
        orders=rows,
        elders=elders,
        items=items,
        nurse_staffs=nurse_staffs,
        total=total,
        page=page,
        pages=pages,
        size=size,
        status=status,
        elder_id=elder_id,
        can_execute=_has_perm(user, "order:execute"),
    )
    return _render(request, "orders/list.html", ctx)


@router.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail(
    request: Request,
    order_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)

    order = None
    try:
        order = db.get(WorkOrder, order_id)
    except Exception:  # noqa: BLE001
        logger.exception("查询工单失败")
    if order is None:
        return _error_page(request, user, "工单不存在", "未找到该工单。")

    if not _has_perm(user, "order:read"):
        return _error_page(request, user, "无权查看", "您没有查看工单的权限。", status_code=403)

    items = _rows(db, select(WorkOrderItem).where(WorkOrderItem.work_order_id == order_id))
    photos = _rows(db, select(WorkOrderPhoto).where(WorkOrderPhoto.work_order_id == order_id))
    tracks = _rows(
        db, select(WorkOrderTrack).where(WorkOrderTrack.work_order_id == order_id).order_by(WorkOrderTrack.id.asc())
    )
    anomalies = _rows(
        db, select(AnomalyReport).where(AnomalyReport.work_order_id == order_id).order_by(AnomalyReport.id.desc())
    )
    elder = None
    service_item = None
    nurse = None
    appointment = None
    evaluation = None
    try:
        elder = db.get(Elder, order.elder_id)
        service_item = db.get(ServiceItem, order.service_item_id)
        nurse = db.get(Staff, order.nurse_id) if order.nurse_id else None
        appointment = db.get(Appointment, order.appointment_id) if order.appointment_id else None
        evaluation = db.execute(
            select(Evaluation).where(Evaluation.work_order_id == order_id).order_by(Evaluation.id.desc())
        ).scalars().first()
    except Exception:  # noqa: BLE001
        logger.exception("工单关联数据查询失败")

    metrics = _rows(
        db,
        select(HealthMetric).where(HealthMetric.work_order_id == order_id).order_by(HealthMetric.measured_at.desc()),
    )
    appointment_logs = (
        _rows(
            db,
            select(AppointmentLog)
            .where(AppointmentLog.appointment_id == order.appointment_id)
            .order_by(AppointmentLog.id.asc()),
        )
        if order.appointment_id
        else []
    )

    ctx = _base_ctx(
        request,
        user,
        "orders",
        f"工单详情 · {order.code}",
        breadcrumb="首页 / 工单 / 详情",
        order=order,
        items=items,
        photos=photos,
        tracks=tracks,
        anomalies=anomalies,
        elder=elder,
        service_item=service_item,
        nurse=nurse,
        appointment=appointment,
        appointment_logs=appointment_logs,
        evaluation=evaluation,
        metrics=metrics,
        can_execute=_has_perm(user, "order:execute"),
        settings=settings,
    )
    return _render(request, "orders/detail.html", ctx)


# ============================================================
# 九、健康监测
# ============================================================
def _default_elder(db: Session, user: User) -> Elder | None:
    """为当前用户挑选默认老人（老人本人 → 数据范围内第一位）。"""
    role = _role_code(user)
    if role == "elder":
        try:
            elder = db.execute(
                select(Elder).where(Elder.user_id == user.id, Elder.is_deleted == 0)
            ).scalars().first()
        except Exception:  # noqa: BLE001
            elder = None
        if elder is not None:
            return elder
    ids = _visible_elder_ids(db, user)
    stmt = select(Elder).where(Elder.is_deleted == 0).order_by(Elder.id.asc()).limit(1)
    if ids is not None:
        stmt = stmt.where(Elder.id.in_(ids or [-1]))
    rows = _rows(db, stmt)
    return rows[0] if rows else None


def _metric_series(
    metrics: list[Any], code: str, labels: list[str], day_keys: dict[Any, int]
) -> list[float | None]:
    buckets: dict[int, list[float]] = {}
    for metric in metrics:
        if metric.metric_code != code or not metric.measured_at:
            continue
        idx = day_keys.get(metric.measured_at.date())
        if idx is None:
            continue
        buckets.setdefault(idx, []).append(float(metric.value or 0))
    data: list[float | None] = []
    for i in range(len(labels)):
        values = buckets.get(i)
        data.append(round(sum(values) / len(values), 1) if values else None)
    return data


@router.get("/monitoring", response_class=HTMLResponse)
def health_entry(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)
    elder = _default_elder(db, user)
    if elder is None:
        return _error_page(request, user, "暂无老人档案", "系统中还没有可查看的老人档案，请先运行种子数据脚本。")
    return RedirectResponse(f"/monitoring/{elder.id}", status_code=303)


@router.get("/monitoring/{elder_id}", response_class=HTMLResponse)
def health_detail(
    request: Request,
    elder_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)

    elder = None
    try:
        elder = db.get(Elder, elder_id)
    except Exception:  # noqa: BLE001
        logger.exception("查询老人失败")
    if elder is None or elder.is_deleted:
        return _error_page(request, user, "档案不存在", "未找到该老人档案。")
    visible = _visible_elder_ids(db, user)
    if visible is not None and elder_id not in visible:
        return _error_page(request, user, "无权查看", "该档案不在您的数据范围内。", status_code=403)

    days = 14
    start = (datetime.now() - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    metrics = _rows(
        db,
        select(HealthMetric)
        .where(HealthMetric.elder_id == elder_id, HealthMetric.measured_at >= start)
        .order_by(HealthMetric.measured_at.asc()),
    )
    labels = [(start + timedelta(days=i)).strftime("%m-%d") for i in range(days)]
    day_keys = {(start + timedelta(days=i)).date(): i for i in range(days)}

    vitals_spec = {
        "type": "line",
        "x": labels,
        "series": [
            {"name": "收缩压", "data": _metric_series(metrics, "sbp", labels, day_keys), "color": "#c62828"},
            {"name": "舒张压", "data": _metric_series(metrics, "dbp", labels, day_keys), "color": "#14508c"},
            {"name": "心率", "data": _metric_series(metrics, "hr", labels, day_keys), "color": "#a86400"},
        ],
    }
    spo2_spec = {
        "type": "line",
        "x": labels,
        "area": True,
        "series": [{"name": "血氧(%)", "data": _metric_series(metrics, "spo2", labels, day_keys), "color": "#1b7f3b"}],
    }
    steps_spec = {
        "type": "bar",
        "x": labels,
        "series": [{"name": "步数", "data": _metric_series(metrics, "steps", labels, day_keys), "color": "#283593"}],
    }

    recent_metrics = _rows(
        db,
        select(HealthMetric)
        .where(HealthMetric.elder_id == elder_id)
        .order_by(HealthMetric.measured_at.desc())
        .limit(30),
    )
    alerts = _rows(
        db,
        select(Alert).where(Alert.elder_id == elder_id).order_by(Alert.id.desc()).limit(8),
    )
    devices = _rows(db, select(Device).where(Device.elder_id == elder_id))

    ctx = _base_ctx(
        request,
        user,
        "health",
        f"健康监测 · {elder.name[:1]}**",
        breadcrumb=f"首页 / 健康监测 / {elder.code}",
        elder=elder,
        metrics=recent_metrics,
        alerts=alerts,
        devices=devices,
        vitals_json=json.dumps(vitals_spec, ensure_ascii=False),
        spo2_json=json.dumps(spo2_spec, ensure_ascii=False),
        steps_json=json.dumps(steps_spec, ensure_ascii=False),
        days=days,
        can_write=_has_perm(user, "health:write"),
        metric_options=[(m.value, _METRICS.get(m.value, m.value)) for m in MetricCode],
    )
    return _render(request, "health/detail.html", ctx)


# ============================================================
# 十、预警看板
# ============================================================
@router.get("/alerts", response_class=HTMLResponse)
def alert_board(
    request: Request,
    level: str = "",
    status: str = "",
    page: int = 1,
    size: int = 20,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)

    visible = _visible_elder_ids(db, user)
    level_rank = case(
        (Alert.level == AlertLevel.RED.value, 3),
        (Alert.level == AlertLevel.YELLOW.value, 2),
        (Alert.level == AlertLevel.BLUE.value, 1),
        else_=0,
    )
    stmt = select(Alert)
    if visible is not None:
        stmt = stmt.where(Alert.elder_id.in_(visible or [-1]))
    if level:
        stmt = stmt.where(Alert.level == level)
    if status:
        stmt = stmt.where(Alert.status == status)
    stmt = stmt.order_by(level_rank.desc(), Alert.id.desc())
    rows, total, page, pages = _paged(db, stmt, page, size)

    elder_ids = {a.elder_id for a in rows}
    elders = _elder_map(db, _rows(db, select(Elder).where(Elder.id.in_(elder_ids)))) if elder_ids else {}
    rule_ids = {a.rule_id for a in rows if a.rule_id}
    rules = _id_name_map(db, AlertRule, rule_ids) if rule_ids else {}

    base_conds: list[Any] = []
    if visible is not None:
        base_conds.append(Alert.elder_id.in_(visible or [-1]))
    stats = {
        "pending": _count(db, Alert, Alert.status == AlertStatus.PENDING.value, *base_conds),
        "red": _count(
            db, Alert, Alert.status == AlertStatus.PENDING.value, Alert.level == AlertLevel.RED.value, *base_conds
        ),
        "yellow": _count(
            db, Alert, Alert.status == AlertStatus.PENDING.value, Alert.level == AlertLevel.YELLOW.value, *base_conds
        ),
        "blue": _count(
            db, Alert, Alert.status == AlertStatus.PENDING.value, Alert.level == AlertLevel.BLUE.value, *base_conds
        ),
        "resolved": _count(db, Alert, Alert.status != AlertStatus.PENDING.value, *base_conds),
    }
    all_rules = _rows(db, select(AlertRule).order_by(AlertRule.id.asc()))

    ctx = _base_ctx(
        request,
        user,
        "alerts",
        "预警看板",
        breadcrumb="首页 / 预警看板",
        alerts=rows,
        elders=elders,
        rules=rules,
        all_rules=all_rules,
        stats=stats,
        total=total,
        page=page,
        pages=pages,
        size=size,
        level=level,
        status=status,
        can_handle=_has_perm(user, "alert:handle"),
        can_rule=_has_perm(user, "alert:rule"),
    )
    return _render(request, "alerts/index.html", ctx)


# ============================================================
# 十一、评价 / 回访 / 投诉
# ============================================================
@router.get("/quality", response_class=HTMLResponse)
def quality_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)

    evaluations = _rows(db, select(Evaluation).order_by(Evaluation.id.desc()).limit(30))
    callbacks = _rows(db, select(CallbackTask).order_by(CallbackTask.id.desc()).limit(30))
    complaints = _rows(db, select(Complaint).order_by(Complaint.id.desc()).limit(30))

    elder_ids = {e.elder_id for e in evaluations} | {c.elder_id for c in callbacks} | {c.elder_id for c in complaints}
    elders = _elder_map(db, _rows(db, select(Elder).where(Elder.id.in_(elder_ids)))) if elder_ids else {}
    staffs = _staff_map(db)
    users = _rows(db, select(User).order_by(User.id.asc()).limit(200))
    order_ids = {e.work_order_id for e in evaluations}
    orders = _id_name_map(db, WorkOrder, order_ids) if order_ids else {}

    avg_score = _scalar(db, select(func.avg(Evaluation.avg_score)), 0)
    pending_callback = _count(db, CallbackTask, CallbackTask.status == "pending")
    open_complaint = _count(db, Complaint, Complaint.status != ComplaintStatus.CONCLUDED.value)
    performance = _rows(
        db, select(StaffPerformanceMonthly).order_by(StaffPerformanceMonthly.period.desc(), StaffPerformanceMonthly.rank_no.asc()).limit(20)
    )

    ctx = _base_ctx(
        request,
        user,
        "quality",
        "评价与投诉",
        breadcrumb="首页 / 评价投诉",
        evaluations=evaluations,
        callbacks=callbacks,
        complaints=complaints,
        elders=elders,
        staffs=staffs,
        users=users,
        orders=orders,
        performance=performance,
        avg_score=round(float(avg_score or 0), 2),
        pending_callback=pending_callback,
        open_complaint=open_complaint,
        can_handle=_has_perm(user, "quality:handle"),
        can_evaluate=_has_perm(user, "quality:evaluate"),
    )
    return _render(request, "quality/index.html", ctx)


# ============================================================
# 十二、费用账单
# ============================================================
@router.get("/billing", response_class=HTMLResponse)
def billing_page(
    request: Request,
    period: str = "",
    status: str = "",
    page: int = 1,
    size: int = 20,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)

    from types import SimpleNamespace

    from app.services import billing_service

    stmt = billing_service._bill_scope(select(Bill), user, db)
    if period:
        stmt = stmt.where(Bill.period == period)
    if status:
        stmt = stmt.where(Bill.status == status)
    stmt = stmt.order_by(Bill.period.desc(), Bill.id.desc())
    rows, total, page, pages = _paged(db, stmt, page, size)
    visible_bills = [billing_service.bill_view(db, bill, user) for bill in _rows(db, stmt)]
    rows = [SimpleNamespace(**billing_service.bill_view(db, bill, user)) for bill in rows]

    org_ids = {b.community_org_id for b in rows}
    orgs = _id_name_map(db, Org, org_ids) if org_ids else {}
    bill_ids = {b.id for b in rows}
    diffs = (
        _rows(
            db,
            select(ReconciliationDiff)
            .where(ReconciliationDiff.bill_id.in_(bill_ids))
            .order_by(ReconciliationDiff.id.desc()),
        )
        if bill_ids
        else []
    )
    open_diffs = _rows(
        db, _scoped(select(ReconciliationDiff), ReconciliationDiff, user, db)
        .where(ReconciliationDiff.status == "open").order_by(ReconciliationDiff.id.desc()).limit(30)
    )
    diffs = billing_service._visible_details(diffs, db, user)
    diff_elder_ids = {d.elder_id for d in open_diffs if d.elder_id}
    diff_elders = _elder_map(db, _rows(db, select(Elder).where(Elder.id.in_(diff_elder_ids)))) if diff_elder_ids else {}

    totals = {
        "amount": sum(bill["total_amount"] for bill in visible_bills),
        "subsidy": sum(bill["subsidy_amount"] for bill in visible_bills),
        "self_pay": sum(bill["self_pay_amount"] for bill in visible_bills),
        "diff": sum(bill["diff_amount"] for bill in visible_bills),
    }
    periods = sorted({b.period for b in _rows(db, billing_service._bill_scope(select(Bill), user, db))}, reverse=True)

    # 账单明细 / 缴费记录：供"查看明细"弹窗本地渲染（接口未就绪时也可展示）
    details = _rows(db, select(BillDetail).where(BillDetail.bill_id.in_(bill_ids))) if bill_ids else []
    details = billing_service._visible_details(details, db, user)
    payments = _rows(db, select(PaymentRecord).where(PaymentRecord.bill_id.in_(bill_ids))) if bill_ids else []
    if user.data_scope in (DataScope.SELF, DataScope.GRID):
        payments = []
    detail_elder_ids = {d.elder_id for d in details}
    detail_elders = (
        _elder_map(db, _rows(db, select(Elder).where(Elder.id.in_(detail_elder_ids)))) if detail_elder_ids else {}
    )
    bill_payload: dict[str, Any] = {}
    for bill in rows:
        bill_details = [d for d in details if d.bill_id == bill.id]
        bill_diffs = [d for d in diffs if d.bill_id == bill.id]
        bill_payload[str(bill.id)] = {
            "code": bill.code,
            "period": bill.period,
            "status": BILL_STATUS_NAMES.get(bill.status, bill.status),
            "total": _fmt_money(bill.total_amount),
            "subsidy": _fmt_money(bill.subsidy_amount),
            "ltc": _fmt_money(bill.ltc_amount),
            "self_pay": _fmt_money(bill.self_pay_amount),
            "diff": _fmt_money(bill.diff_amount),
            "diff_ratio": f"{float(bill.diff_ratio or 0) * 100:.2f}%",
            "diff_ratio_num": float(bill.diff_ratio or 0),
            "subsidy_num": float(bill.subsidy_amount or 0),
            "ltc_num": float(bill.ltc_amount or 0),
            "self_pay_num": float(bill.self_pay_amount or 0),
            "needs_review": bool(bill.needs_review),
            "details": [
                {
                    "elder": mask_name(detail_elders[d.elder_id].name) if d.elder_id in detail_elders else "-",
                    "unit_price": _fmt_money(d.unit_price),
                    "quantity": d.quantity,
                    "amount": _fmt_money(d.amount),
                    "subsidy": _fmt_money(d.subsidy_amount),
                    "self_pay": _fmt_money(d.self_pay_amount),
                    "work_order_id": d.work_order_id,
                }
                for d in bill_details
            ],
            "payments": [
                {"payer": p.payer_type, "amount": _fmt_money(p.amount), "status": p.status}
                for p in payments
                if p.bill_id == bill.id
            ],
            "diffs": [
                {
                    "rule_code": d.rule_code,
                    "description": d.description,
                    "amount": _fmt_money(d.amount),
                    "status": DIFF_STATUS_NAMES.get(d.status, d.status),
                }
                for d in bill_diffs
            ],
        }

    ctx = _base_ctx(
        request,
        user,
        "billing",
        "费用账单",
        breadcrumb="首页 / 费用账单",
        bills=rows,
        orgs=orgs,
        diffs=diffs,
        open_diffs=open_diffs,
        diff_elders=diff_elders,
        totals=totals,
        periods=periods,
        total=total,
        page=page,
        pages=pages,
        size=size,
        period=period,
        status=status,
        can_review=_has_perm(user, "bill:review"),
        can_pay=_has_perm(user, "bill:pay"),
        can_refund=_has_perm(user, "bill:refund"),
        can_generate=_has_perm(user, "bill:generate"),
        diff_threshold=settings.diff_ratio_threshold,
        bill_payload_json=json.dumps(bill_payload, ensure_ascii=False),
    )
    return _render(request, "billing/index.html", ctx)


# ============================================================
# 十三、统计看板
# ============================================================
@router.get("/stats", response_class=HTMLResponse)
def stats_page(
    request: Request,
    days: int = 30,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)
    if not _has_perm(user, "stats:read"):
        return _error_page(request, user, "无权查看", "您没有统计看板权限。", status_code=403)

    days = 7 if days <= 7 else 90 if days >= 90 else 30
    now = datetime.now()
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    orders = _rows(
        db,
        _scoped(select(WorkOrder).where(WorkOrder.scheduled_start >= start), WorkOrder, user, db),
    )
    appointments = _rows(
        db, _scoped(select(Appointment).where(Appointment.desired_start >= start), Appointment, user, db)
    )
    completed = [o for o in orders if o.status == OrderStatus.COMPLETED.value]
    labels = [(start + timedelta(days=i)).strftime("%m-%d") for i in range(days)]
    key_index = {(start + timedelta(days=i)).date(): i for i in range(days)}
    order_counts = [0] * days
    appt_counts = [0] * days
    for order in completed:
        idx = key_index.get(order.scheduled_start.date()) if order.scheduled_start else None
        if idx is not None:
            order_counts[idx] += 1
    for appt in appointments:
        idx = key_index.get(appt.desired_start.date()) if appt.desired_start else None
        if idx is not None:
            appt_counts[idx] += 1

    trend_spec = {
        "type": "line",
        "x": labels,
        "area": True,
        "series": [
            {"name": "完成工单", "data": order_counts, "color": "#14508c"},
            {"name": "新增预约", "data": appt_counts, "color": "#1b7f3b"},
        ],
    }

    status_counter = Counter(o.status for o in orders)
    status_pie = {
        "type": "pie",
        "pie": [
            {"name": ORDER_STATUS_NAMES.get(s, s), "value": c} for s, c in status_counter.most_common()
        ]
        or [{"name": "暂无数据", "value": 1}],
    }

    evaluations = _rows(db, _scoped(select(Evaluation).where(Evaluation.created_at >= start), Evaluation, user, db))
    sat_counter = Counter()
    for ev in evaluations:
        try:
            sat_counter[int(round(float(ev.avg_score or 0)))] += 1
        except (TypeError, ValueError):
            continue
    satisfaction_pie = {
        "type": "pie",
        "pie": [{"name": f"{score} 星", "value": sat_counter.get(score, 0)} for score in (5, 4, 3, 2, 1)]
        or [{"name": "暂无数据", "value": 1}],
    }

    nurse_counter: Counter = Counter()
    for order in completed:
        if order.nurse_id:
            nurse_counter[order.nurse_id] += 1
    staffs = _staff_map(db)
    top_nurses = nurse_counter.most_common(8)
    ranking_spec = {
        "type": "hbar",
        "x": [staffs[sid].name if sid in staffs else f"护理员{sid}" for sid, _ in top_nurses] or ["暂无数据"],
        "series": [{"name": "完成工单", "data": [c for _, c in top_nurses] or [0], "color": "#c62828"}],
    }

    elder_rows = _rows(db, _scoped(select(Elder).where(Elder.is_deleted == 0), Elder, user, db))
    ability_counter = Counter(e.ability_level for e in elder_rows)
    ability_spec = {
        "type": "bar",
        "x": list(_ABILITY.values()),
        "series": [
            {
                "name": "老人数量",
                "data": [ability_counter.get(key, 0) for key in _ABILITY.keys()],
                "color": "#283593",
            }
        ],
    }

    community_counter: Counter = Counter()
    orgs = _orgs_by_level(db, "community")
    org_map = {o.id: o.name for o in orgs}
    for order in completed:
        if order.community_org_id:
            community_counter[order.community_org_id] += 1
    community_spec = {
        "type": "bar",
        "x": [org_map.get(oid, f"社区{oid}") for oid, _ in community_counter.most_common(8)] or ["暂无数据"],
        "series": [{"name": "完成工单", "data": [c for _, c in community_counter.most_common(8)] or [0], "color": "#00695c"}],
    }

    amount = sum(float(o.amount or 0) for o in completed)
    avg_score = _scalar(db, select(func.avg(Evaluation.avg_score)), 0)
    alert_visible = _visible_elder_ids(db, user)
    alert_conds: list[Any] = [Alert.created_at >= start]
    if alert_visible is not None:
        alert_conds.append(Alert.elder_id.in_(alert_visible or [-1]))
    kpis = [
        {"label": "完成工单量", "value": len(completed), "hint": f"近 {days} 天", "cls": ""},
        {"label": "服务金额", "value": _fmt_money(amount), "hint": "元（按价格快照）", "cls": "ok"},
        {"label": "服务满意度", "value": f"{float(avg_score or 0):.2f}", "hint": "5 分制平均", "cls": "warn"},
        {"label": "预警数量", "value": _count(db, Alert, *alert_conds), "hint": "含已处置", "cls": "danger"},
    ]

    ctx = _base_ctx(
        request,
        user,
        "stats",
        "统计看板",
        breadcrumb="首页 / 统计看板",
        days=days,
        kpis=kpis,
        trend_json=json.dumps(trend_spec, ensure_ascii=False),
        status_pie_json=json.dumps(status_pie, ensure_ascii=False),
        satisfaction_json=json.dumps(satisfaction_pie, ensure_ascii=False),
        ranking_json=json.dumps(ranking_spec, ensure_ascii=False),
        ability_json=json.dumps(ability_spec, ensure_ascii=False),
        community_json=json.dumps(community_spec, ensure_ascii=False),
        can_export=_has_perm(user, "stats:export"),
        date_from=start.strftime("%Y-%m-%d"),
        date_to=now.strftime("%Y-%m-%d"),
    )
    return _render(request, "stats/index.html", ctx)


# ============================================================
# 十四、系统管理
# ============================================================
@router.get("/admin/users", response_class=HTMLResponse)
def admin_users(
    request: Request,
    keyword: str = "",
    page: int = 1,
    size: int = 20,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)
    if not _has_perm(user, "admin:user"):
        return _error_page(request, user, "无权访问", "您没有用户管理权限。", status_code=403)

    stmt = select(User)
    if _role_code(user) == "org_admin":
        stmt = stmt.where(User.org_id == user.org_id)
    if keyword:
        stmt = stmt.where(or_(User.username.contains(keyword), User.real_name.contains(keyword)))
    stmt = stmt.order_by(User.id.asc())
    rows, total, page, pages = _paged(db, stmt, page, size)

    roles = _rows(db, select(Role).order_by(Role.id.asc()))
    orgs = _rows(db, select(Org).order_by(Org.sort.asc(), Org.id.asc()))
    perms = _rows(db, select(Permission).order_by(Permission.module.asc(), Permission.id.asc()))
    perm_groups: dict[str, list[Permission]] = {}
    for perm in perms:
        perm_groups.setdefault(perm.module, []).append(perm)

    ctx = _base_ctx(
        request,
        user,
        "admin_users",
        "用户管理",
        breadcrumb="首页 / 系统管理 / 用户",
        users=rows,
        roles=roles,
        orgs=orgs,
        perm_groups=perm_groups,
        total=total,
        page=page,
        pages=pages,
        size=size,
        keyword=keyword,
        is_super=_role_code(user) == "super",
        scope_options=[
            (DataScope.SELF.value, "本人"),
            (DataScope.GRID.value, "本网格"),
            (DataScope.ORG.value, "本机构/社区"),
            (DataScope.STREET.value, "全街道"),
        ],
    )
    return _render(request, "admin/users.html", ctx)


@router.get("/admin/orgs", response_class=HTMLResponse)
def admin_orgs(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)
    if not _has_perm(user, "admin:org"):
        return _error_page(request, user, "无权访问", "您没有组织管理权限。", status_code=403)

    orgs = _rows(db, select(Org).order_by(Org.sort.asc(), Org.id.asc()))
    by_parent: dict[int | None, list[Org]] = {}
    for org in orgs:
        by_parent.setdefault(org.parent_id, []).append(org)

    def build(parent_id: int | None, depth: int = 0) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        for org in by_parent.get(parent_id, []):
            nodes.append({"org": org, "depth": depth})
            nodes.extend(build(org.id, depth + 1))
        return nodes

    user_counts = Counter(
        u.org_id for u in _rows(db, select(User)) if u.org_id
    )
    elder_counts = Counter(
        e.community_org_id for e in _rows(db, select(Elder).where(Elder.is_deleted == 0)) if e.community_org_id
    )

    ctx = _base_ctx(
        request,
        user,
        "admin_orgs",
        "组织管理",
        breadcrumb="首页 / 系统管理 / 组织",
        tree=build(None),
        orgs=orgs,
        user_counts=user_counts,
        elder_counts=elder_counts,
    )
    return _render(request, "admin/orgs.html", ctx)


@router.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(
    request: Request,
    action: str = "",
    entity_type: str = "",
    page: int = 1,
    size: int = 30,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)
    if not _has_perm(user, "admin:audit"):
        return _error_page(request, user, "无权访问", "您没有审计日志权限。", status_code=403)

    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    stmt = stmt.order_by(AuditLog.id.desc())
    rows, total, page, pages = _paged(db, stmt, page, size)

    actions = sorted({log.action for log in _rows(db, select(AuditLog).limit(500))})
    ctx = _base_ctx(
        request,
        user,
        "admin_audit",
        "审计日志",
        breadcrumb="首页 / 系统管理 / 审计日志",
        logs=rows,
        total=total,
        page=page,
        pages=pages,
        size=size,
        action=action,
        entity_type=entity_type,
        actions=actions,
    )
    return _render(request, "admin/audit.html", ctx)


@router.get("/admin/backup", response_class=HTMLResponse)
def admin_backup(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)
    if not _has_perm(user, "admin:backup"):
        return _error_page(request, user, "无权访问", "您没有备份恢复权限。", status_code=403)

    backups = _rows(db, select(BackupRecord).order_by(BackupRecord.id.desc()).limit(50))
    ctx = _base_ctx(
        request,
        user,
        "admin_backup",
        "备份与恢复",
        breadcrumb="首页 / 系统管理 / 备份恢复",
        backups=backups,
        keep_versions=settings.backup_keep_versions,
        db_url=settings.database_url,
    )
    return _render(request, "admin/backup.html", ctx)


@router.get("/admin/config", response_class=HTMLResponse)
def admin_config(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(page_user),
) -> Response:
    if user is None:
        return _login_redirect(request)
    if not _has_perm(user, "admin:config"):
        return _error_page(request, user, "无权访问", "您没有系统配置权限。", status_code=403)

    configs = _rows(db, select(ConfigItem).order_by(ConfigItem.key.asc()))
    rules = _rows(db, select(AlertRule).order_by(AlertRule.id.asc()))
    policies = _rows(db, select(SubsidyPolicy).order_by(SubsidyPolicy.id.asc()))
    ctx = _base_ctx(
        request,
        user,
        "admin_config",
        "系统配置",
        breadcrumb="首页 / 系统管理 / 系统配置",
        configs=configs,
        rules=rules,
        policies=policies,
        dispatch_weights=settings.dispatch_weights,
        order_daily_limit=settings.order_daily_limit,
        service_radius_km=settings.service_radius_km,
        checkin_distance=settings.checkin_distance_threshold_m,
        alert_silence_hours=settings.alert_silence_hours,
        diff_ratio_threshold=settings.diff_ratio_threshold,
    )
    return _render(request, "admin/config.html", ctx)


# ============================================================
# 十五、上传文件访问（服务照片等，需登录）
# ============================================================
@router.get("/files/{filename}")
def serve_upload_file(
    filename: str, user: User | None = Depends(page_user), db: Session = Depends(get_db),
) -> Response:
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not _has_perm(user, "order:read"):
        return Response("无权查看服务照片", status_code=403)
    from app.services.order_service import ensure_order_visible, get_order

    photo = db.scalars(select(WorkOrderPhoto).where(
        WorkOrderPhoto.path.endswith("/" + filename, autoescape=True),
    )).first()
    if photo is None:
        return Response("文件不存在", status_code=404)
    ensure_order_visible(db, user, get_order(db, photo.work_order_id))
    base = settings.upload_path.resolve()
    path = Path(photo.path)
    target = (path if path.is_absolute() else BASE_DIR / path).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return Response("非法文件路径", status_code=400, media_type="text/plain; charset=utf-8")
    if not target.is_file():
        return Response("文件不存在", status_code=404, media_type="text/plain; charset=utf-8")
    return FileResponse(str(target), headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
