"""Barcode/SKU validation rules from generate_xml.py - GS1 check digit,
the coupon-range rejection (2026-07-28: 97 real feed rows), and SKU
decoration stripping (user policy 2026-07-13). Extracted from source so no
gspread import is needed."""
import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "generate_xml.py"


def _extract(*names):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SRC), "exec"), ns)
    return [ns[n] for n in names]


is_valid_gtin, sku_numeric_part = _extract("is_valid_gtin", "sku_numeric_part")


def test_valid_barcodes_pass():
    assert is_valid_gtin("4006381333931")   # EAN-13, valid check digit
    assert is_valid_gtin("036000291452")    # UPC-A, valid check digit
    assert is_valid_gtin("5012345678900")   # GS1 UK prefix, 13-digit


def test_bad_check_digit_fails():
    assert not is_valid_gtin("4006381333932")
    assert not is_valid_gtin("036000291453")


def test_wrong_length_or_non_digits_fail():
    assert not is_valid_gtin("12345")
    assert not is_valid_gtin("40063813339312345")
    assert not is_valid_gtin("40063813339a1")
    assert not is_valid_gtin("")


def test_coupon_range_rejected():
    # GS1 reserves the "5" number system of 12-digit UPCs for coupons -
    # valid check digit, but never a product code (2026-07-28: OnBuy
    # rejected 23 YRA + 74 Arden rows with "not a valid product code").
    assert not is_valid_gtin("512345678903")   # 12-digit starting 5
    assert not is_valid_gtin("051234567890" + "5")  # 13-digit starting 05 (zero-padded coupon)


def test_gs1_uk_13_digit_starting_50_stays_valid():
    assert is_valid_gtin("5000112637922")  # real GS1 UK style barcode


def test_sku_decoration_strips_to_digits():
    assert sku_numeric_part("GTV-5012345678900") == "5012345678900"
    assert sku_numeric_part("5012345678900") == "5012345678900"


def test_sku_with_no_digits_yields_empty():
    assert sku_numeric_part("NO-DIGITS-HERE") == ""
