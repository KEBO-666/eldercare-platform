"""可解释评分、电子表格导出和照片隐私边界。"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from PIL import Image

from app.algorithms.dispatch import DEFAULT_WEIGHTS, parse_weights, score_candidates
from app.core.config import settings
from app.core.exceptions import RuleViolation
from app.core.export import safe_cell
from app.models import Bill, BillDetail
from app.services import archive_service, billing_service, order_service


@pytest.mark.parametrize("weights", [
    [-1, 1, 1, 1], [float("nan"), 1, 1, 1], [float("inf"), 1, 1, 1],
    [0, 0, 0, 0], "-1,1,1,1", {"skill": -1, "distance": 1, "load": 1, "rating": 1},
])
def test_invalid_weights_fall_back_to_valid_defaults(weights):
    assert parse_weights(weights) == DEFAULT_WEIGHTS
    assert parse_weights(weights, (1, 0, 0, 0)) == (1, 0, 0, 0)


def test_score_explains_contributions_and_focus_penalty():
    candidate = {"staff_id": 1, "distance_km": 0.5, "skills": ["shower"], "is_focus": True}
    result = score_candidates([candidate], required_skills=["shower"])[0]
    assert result["penalty"] == 0.8
    assert sum(result["contributions"].values()) * 0.8 == pytest.approx(result["score"], abs=0.0002)


@pytest.mark.parametrize("value", ["=1+1", "+SUM(A1)", "-cmd", "@SUM(A1)", " \t=1+1", "\r\n=1+1"])
def test_formula_text_is_escaped(value):
    assert safe_cell(value) == "'" + value


def test_export_numbers_remain_numeric():
    assert safe_cell(-10) == -10
    assert safe_cell(Decimal("-0.01")) == Decimal("-0.01")
    assert safe_cell("张爷爷") == "张爷爷"


def test_archive_csv_escapes_user_text(db, accounts):
    accounts["elder"].code = "=1+1"
    db.commit()
    content, _ = archive_service.export_csv(db, accounts["users"]["super"])
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    assert rows[1][0] == "'=1+1"


def test_bill_xlsx_stores_user_text_without_formula(db, accounts):
    elder = accounts["elder"]
    elder.name = "=HYPERLINK(\"https://example.com\")"
    bill = Bill(code="TEST", period="2026-11", community_org_id=elder.community_org_id,
                total_amount=Decimal("1.00"), subsidy_amount=Decimal("0"),
                ltc_amount=Decimal("0"), self_pay_amount=Decimal("1.00"))
    detail = BillDetail(elder_id=elder.id, unit_price=Decimal("1"), quantity=1, amount=Decimal("1"),
                        subsidy_amount=Decimal("0"), ltc_amount=Decimal("0"), self_pay_amount=Decimal("1"))
    content = billing_service.build_bill_workbook(bill, [detail], {elder.id: elder}, {})
    sheet = load_workbook(io.BytesIO(content)).active
    cells = [cell for row in sheet for cell in row]
    assert not any(cell.data_type == "f" for cell in cells)
    assert any(cell.value == "'" + elder.name for cell in cells)


def test_photo_compression_removes_exif_and_limits_pixels(monkeypatch):
    raw = io.BytesIO()
    exif = Image.Exif()
    exif[270] = "private location"
    exif[274] = 6
    Image.new("RGB", (80, 40)).save(raw, format="JPEG", exif=exif)
    content, width, height = order_service.compress_image(raw.getvalue())
    assert (width, height) == (40, 80)
    with Image.open(io.BytesIO(content)) as image:
        assert not image.getexif()
    monkeypatch.setattr(settings, "photo_max_pixels", 100)
    with pytest.raises(RuleViolation, match="像素"):
        order_service.compress_image(raw.getvalue())


def test_oversize_photo_rejected_before_decode(monkeypatch):
    monkeypatch.setattr(settings, "photo_upload_max_mb", 1)
    with pytest.raises(RuleViolation, match="原始照片"):
        order_service.compress_image(b"x" * (1024 * 1024 + 1))
