"""老人档案相关 Pydantic 模型（请求体校验，服务层复用）。

约定：
- 敏感字段（身份证 / 手机号 / 住址 / 紧急联系电话）入库前由服务层加密，
  这里只做格式与枚举校验；
- 更新模型全部可选，服务层用 ``model_dump(exclude_unset=True)`` 做逐字段 diff；
- ``code`` 在更新时仍然出现在模型中，以便明确拒绝"修改编码"的非法请求。
"""
from __future__ import annotations

import re
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.enums import AbilityLevel, DifficultyType

GENDERS = {"male", "female", "unknown"}
ABILITY_LEVELS = {item.value for item in AbilityLevel}
DIFFICULTY_TYPES = {item.value for item in DifficultyType}

# 允许通过二次授权查看明文的字段（与 Elder 模型的加密列一一对应）
SENSITIVE_FIELDS: tuple[str, ...] = ("id_card", "phone", "address", "emergency_phone")

_ID_CARD_RE = re.compile(r"^(\d{15}|\d{17}[\dXx])$")
_PHONE_RE = re.compile(r"^1\d{10}$")


def _clean_text(value: str | None, *, field: str = "字段") -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_tags(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError("tags 必须是字符串数组")
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        if len(text) > 32:
            raise ValueError("标签长度不能超过 32 个字符")
        if text not in result:
            result.append(text)
    return result


class _OptionalElderFields(BaseModel):
    """新增/编辑共用的可选字段与校验（更新时可缺省）。"""

    model_config = ConfigDict(extra="ignore")

    gender: str | None = None
    birth_date: date | None = None
    id_card: str | None = None
    phone: str | None = None
    address: str | None = None
    grid_org_id: int | None = None
    ability_level: str | None = None
    difficulty_type: str | None = None
    insurance_type: str | None = None
    ltc_type: str | None = None
    emergency_name: str | None = None
    emergency_relation: str | None = None
    emergency_phone: str | None = None
    allergy: str | None = None
    notes: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("gender")
    @classmethod
    def _check_gender(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip().lower()
        if text not in GENDERS:
            raise ValueError(f"性别必须是 {sorted(GENDERS)} 之一")
        return text

    @field_validator("ability_level")
    @classmethod
    def _check_ability_level(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip().lower()
        if text not in ABILITY_LEVELS:
            raise ValueError(f"能力等级必须是 {sorted(ABILITY_LEVELS)} 之一")
        return text

    @field_validator("difficulty_type")
    @classmethod
    def _check_difficulty_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip().lower()
        if text not in DIFFICULTY_TYPES:
            raise ValueError(f"困难类型必须是 {sorted(DIFFICULTY_TYPES)} 之一")
        return text

    @field_validator("id_card")
    @classmethod
    def _check_id_card(cls, value: str | None) -> str | None:
        text = _clean_text(value)
        if text is None:
            return None
        if not _ID_CARD_RE.match(text):
            raise ValueError("身份证号格式不正确（需 15 位或 18 位）")
        return text.upper()

    @field_validator("phone", "emergency_phone")
    @classmethod
    def _check_phone(cls, value: str | None) -> str | None:
        text = _clean_text(value)
        if text is None:
            return None
        if not _PHONE_RE.match(text):
            raise ValueError("手机号格式不正确（需 11 位数字）")
        return text

    @field_validator("address")
    @classmethod
    def _check_address(cls, value: str | None) -> str | None:
        text = _clean_text(value)
        if text is not None and len(text) > 1000:
            raise ValueError("住址长度不能超过 1000 个字符")
        return text


class ElderCreate(_OptionalElderFields):
    """新增老人档案。"""

    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    community_org_id: int
    gender: str = "unknown"
    ability_level: str = AbilityLevel.INTACT.value
    difficulty_type: str = DifficultyType.NORMAL.value
    tags: list[str] = Field(default_factory=list)

    @field_validator("code", "name")
    @classmethod
    def _check_required_text(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("不能为空")
        return text

    @field_validator("tags", mode="before")
    @classmethod
    def _check_tags(cls, value: object) -> list[str]:
        return _normalize_tags(value)


class ElderUpdate(_OptionalElderFields):
    """编辑老人档案（仅更新显式传入的字段；code 不允许修改）。"""

    code: str | None = None
    name: str | None = None
    community_org_id: int | None = None
    reason: str | None = Field(default=None, max_length=255)  # 变更原因（写入变更日志）

    @field_validator("code", "name")
    @classmethod
    def _check_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("不能为空")
        return text


class TagCreate(BaseModel):
    """新增标签，兼容 ``{"tag": "x"}`` 与 ``{"tags": ["x", "y"]}``。"""

    model_config = ConfigDict(extra="ignore")

    tag: str | None = None
    tags: list[str] | None = None

    @model_validator(mode="after")
    def _require_at_least_one(self) -> TagCreate:
        if not self.normalized():
            raise ValueError("至少提供一个标签")
        return self

    def normalized(self) -> list[str]:
        raw: list[str] = []
        if self.tag:
            raw.append(self.tag)
        if self.tags:
            raw.extend(self.tags)
        return _normalize_tags(raw)


class DiseaseCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=64)
    diagnosed_at: date | None = None
    note: str | None = Field(default=None, max_length=255)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("疾病名称不能为空")
        return text


class MedicationCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=64)
    dosage: str | None = Field(default=None, max_length=64)
    frequency: str | None = Field(default=None, max_length=64)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("药品名称不能为空")
        return text


class AssessmentCreate(BaseModel):
    """新增能力评估（写入后同步 Elder.ability_level）。"""

    model_config = ConfigDict(extra="ignore")

    level: str = Field(min_length=1)
    score: int | None = Field(default=None, ge=0, le=100)
    assessed_at: datetime | None = None
    basis: str | None = None

    @field_validator("level")
    @classmethod
    def _check_level(cls, value: str) -> str:
        text = value.strip().lower()
        if text not in ABILITY_LEVELS:
            raise ValueError(f"能力等级必须是 {sorted(ABILITY_LEVELS)} 之一")
        return text


class GrantCreate(BaseModel):
    """二次授权申请：{fields: [...], reason: "..."}，默认 10 分钟有效。"""

    model_config = ConfigDict(extra="ignore")

    fields: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=255)

    @field_validator("fields")
    @classmethod
    def _check_fields(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            text = str(item).strip()
            if text not in SENSITIVE_FIELDS:
                raise ValueError(f"不支持的敏感字段：{text}（可选 {list(SENSITIVE_FIELDS)}）")
            if text not in result:
                result.append(text)
        if not result:
            raise ValueError("至少指定一个敏感字段")
        return result

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("授权理由不能为空")
        return text
