"""pytest 共享夹具：内存库、TestClient、RBAC 账号、档案与目录种子。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.enums import (
    ROLE_NAMES,
    AbilityLevel,
    DataScope,
    DifficultyType,
    PricingMode,
    RoleCode,
)
from app.core.permissions import PERMISSIONS, ROLE_DATA_SCOPE, ROLE_PERMISSIONS
from app.core.security import create_access_token, hash_password
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app
from app.models import (
    AbilityAssessment,
    Elder,
    ElderTag,
    Org,
    Permission,
    Role,
    ServiceItem,
    ServicePriceVersion,
    Staff,
    User,
)

TEST_PASSWORD = "Passw0rd!"
BASE_TIME = datetime(2026, 10, 15, 9, 0, 0)


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture()
def db(engine):
    Local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Local()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def app(db):
    application = create_app(startup_db=False, enable_scheduler=False)

    def _override():
        yield db

    application.dependency_overrides[get_db] = _override
    return application


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        c.headers.update({"X-Test": "1"})
        yield c


@pytest.fixture()
def orgs(db):
    street = Org(code="ST01", name="幸福街道", level="street")
    db.add(street)
    db.flush()
    community1 = Org(code="CM01", name="幸福社区", level="community", parent_id=street.id)
    community2 = Org(code="CM02", name="安康社区", level="community", parent_id=street.id)
    db.add_all([community1, community2])
    db.flush()
    grid1 = Org(code="GD01", name="幸福社区一网格", level="grid", parent_id=community1.id)
    grid2 = Org(code="GD02", name="幸福社区二网格", level="grid", parent_id=community1.id)
    grid3 = Org(code="GD03", name="安康社区一网格", level="grid", parent_id=community2.id)
    db.add_all([grid1, grid2, grid3])
    db.commit()
    return {
        "street": street,
        "community1": community1,
        "community2": community2,
        "grid1": grid1,
        "grid2": grid2,
        "grid3": grid3,
    }


@pytest.fixture()
def rbac(db):
    perms: dict[str, Permission] = {}
    for code, name, module in PERMISSIONS:
        p = Permission(code=code, name=name, module=module)
        db.add(p)
        perms[code] = p
    db.flush()
    roles: dict[str, Role] = {}
    for role_code in RoleCode:
        role = Role(
            code=role_code.value,
            name=ROLE_NAMES[role_code],
            data_scope=ROLE_DATA_SCOPE[role_code],
        )
        db.add(role)
        db.flush()
        role.permissions = [perms[c] for c in ROLE_PERMISSIONS[role_code.value] if c in perms]
        roles[role_code.value] = role
    db.commit()
    return roles


@pytest.fixture()
def accounts(db, orgs, rbac):
    """六个角色的账号 + 一名护理员 + 一位绑定账号的老人 + 一个服务项目。"""
    def make_user(username: str, role_code: str, org_id: int | None, scope: str, password: str = TEST_PASSWORD) -> User:
        user = User(
            username=username,
            password_hash=hash_password(password),
            real_name=username,
            role_id=rbac[role_code].id,
            org_id=org_id,
            data_scope=scope,
        )
        db.add(user)
        db.flush()
        return user

    users = {
        "super": make_user("super", RoleCode.SUPER.value, orgs["street"].id, DataScope.STREET),
        "street": make_user("street", RoleCode.STREET.value, orgs["street"].id, DataScope.STREET),
        "org_admin": make_user("orgadmin", RoleCode.ORG_ADMIN.value, orgs["community1"].id, DataScope.ORG),
        "grid": make_user("grid", RoleCode.GRID.value, orgs["grid1"].id, DataScope.GRID),
        "grid2": make_user("grid2", RoleCode.GRID.value, orgs["grid2"].id, DataScope.GRID),
        "nurse": make_user("nurse", RoleCode.NURSE.value, orgs["community1"].id, DataScope.SELF),
        "elder": make_user("elder", RoleCode.ELDER.value, orgs["community1"].id, DataScope.SELF),
    }

    staff = Staff(
        user_id=users["nurse"].id,
        employee_no="N0001",
        name="王护理",
        skills=["shower", "bedcare", "cleaning"],
        service_radius_km=3.0,
        base_lat=31.2304,
        base_lng=121.4737,
        org_id=orgs["community1"].id,
    )
    db.add(staff)
    db.flush()

    elder = Elder(
        code="E0001",
        name="张爷爷",
        gender="male",
        community_org_id=orgs["community1"].id,
        grid_org_id=orgs["grid1"].id,
        ability_level=AbilityLevel.MODERATE,
        difficulty_type=DifficultyType.LOW_INCOME,
        lat=31.2310,
        lng=121.4740,
        user_id=users["elder"].id,
    )
    db.add(elder)
    db.flush()
    db.add(ElderTag(elder_id=elder.id, tag="living_alone"))
    db.add(AbilityAssessment(elder_id=elder.id, level=AbilityLevel.MODERATE, score=45,
                             assessed_at=BASE_TIME, basis="初评"))

    item = ServiceItem(
        code="S0001",
        name="助浴",
        category="bath",
        pricing_mode=PricingMode.PER_TIME,
        standard_duration_min=60,
        required_skills=["shower"],
        applicable_levels=[AbilityLevel.MODERATE, AbilityLevel.SEVERE],
        is_subsidized=True,
        subsidy_ratio=0.5,
    )
    db.add(item)
    db.flush()
    price = ServicePriceVersion(
        service_item_id=item.id, version=1, unit_price=Decimal("120.00"), effective_from=BASE_TIME
    )
    db.add(price)
    db.flush()
    item.current_price_version_id = price.id
    db.commit()

    return {
        "users": users,
        "staff": staff,
        "elder": elder,
        "item": item,
        "price": price,
    }


@pytest.fixture()
def headers(accounts):
    def _headers(role: str) -> dict[str, str]:
        user = accounts["users"][role]
        token = create_access_token(user.id, {"role": user.role.code, "username": user.username})
        return {"Authorization": f"Bearer {token}"}

    return _headers


@pytest.fixture()
def login(client):
    def _login(username: str, password: str = TEST_PASSWORD) -> dict:
        resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert resp.status_code == 200, resp.text
        return resp.json()

    return _login


@pytest.fixture()
def make_elder(db, orgs):
    """工厂：快速创建老人档案。"""
    counter = {"n": 0}

    def _make(**kwargs) -> Elder:
        counter["n"] += 1
        defaults = dict(
            code=f"E9{counter['n']:03d}",
            name=f"测试老人{counter['n']}",
            gender="female",
            community_org_id=orgs["community1"].id,
            grid_org_id=orgs["grid1"].id,
            ability_level=AbilityLevel.MILD,
            difficulty_type=DifficultyType.NORMAL,
            lat=31.2305,
            lng=121.4735,
        )
        defaults.update(kwargs)
        elder = Elder(**defaults)
        db.add(elder)
        db.flush()
        return elder

    return _make


@pytest.fixture()
def make_staff(db, orgs):
    counter = {"n": 0}

    def _make(**kwargs) -> Staff:
        counter["n"] += 1
        from app.models import User as _User  # 避免循环导入

        user = _User(
            username=f"nurse9{counter['n']}",
            password_hash=hash_password(TEST_PASSWORD),
            real_name=f"护理员{counter['n']}",
            role_id=db.query(Role).filter(Role.code == RoleCode.NURSE.value).one().id,
            org_id=orgs["community1"].id,
            data_scope=DataScope.SELF,
        )
        db.add(user)
        db.flush()
        defaults = dict(
            user_id=user.id,
            employee_no=f"N9{counter['n']:03d}",
            name=f"护理员{counter['n']}",
            skills=["shower"],
            service_radius_km=3.0,
            base_lat=31.2304,
            base_lng=121.4737,
            org_id=orgs["community1"].id,
        )
        defaults.update(kwargs)
        staff = Staff(**defaults)
        db.add(staff)
        db.flush()
        return staff

    return _make
