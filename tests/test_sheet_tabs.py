"""Product tabs are found by NAME, never by position (2026-10-01 incident:
"OnBuy Categories" was moved ahead of "Sheet1" and every `book.sheet1`
reader silently read the wrong tab)."""
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gspread
import pytest

import sheet_tabs

ROOT = Path(__file__).resolve().parents[1]

PRODUCT_HEADERS = ["SKU", "Supplier URL", "Title", "Status"]


class FakeWS:
    def __init__(self, title, headers):
        self.title = title
        self._headers = headers

    def row_values(self, n):
        return list(self._headers) if n == 1 else []


class FakeBook:
    def __init__(self, tabs):
        self._tabs = tabs

    def worksheets(self):
        return list(self._tabs)

    def worksheet(self, name):
        for t in self._tabs:
            if t.title == name:
                return t
        raise gspread.exceptions.WorksheetNotFound(name)


def test_named_sheet1_wins_even_when_another_tab_is_first():
    cats = FakeWS("OnBuy Categories", ["Arts, Crafts & Sewing > Art Paper"])
    s1 = FakeWS("Sheet1", PRODUCT_HEADERS)
    book = FakeBook([cats, s1, FakeWS("Amazon", PRODUCT_HEADERS)])
    assert sheet_tabs.product_sheet(book) is s1


def test_renamed_main_tab_falls_back_to_first_non_amazon_product_tab():
    cats = FakeWS("OnBuy Categories", ["Arts, Crafts & Sewing > Art Paper"])
    products = FakeWS("Products", PRODUCT_HEADERS)
    book = FakeBook([FakeWS("Amazon", PRODUCT_HEADERS), cats, products])
    assert sheet_tabs.product_sheet(book) is products


def test_no_product_tab_is_an_error_not_a_quiet_wrong_read():
    book = FakeBook([FakeWS("OnBuy Categories", ["x"]), FakeWS("Amazon", PRODUCT_HEADERS)])
    with pytest.raises(RuntimeError):
        sheet_tabs.product_sheet(book)


# Scripts that run on a schedule or feed a destructive chain must never take
# the first tab by position again.
AUTOMATED = ("generate_xml.py", "buybox_defense.py", "deletion_reconciler.py",
             "backfill_onbuy_status.py", "audit_price_drift.py", "scan_content_mismatch.py",
             "zero_stock_mismatched.py", "plan_bulk_delete.py")


@pytest.mark.parametrize("name", AUTOMATED)
def test_no_positional_sheet1_in_automated_scripts(name):
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} not present in this repo")
    src = io.open(path, encoding="utf-8").read()
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    assert ".sheet1" not in code, f"{name} reads the first tab by position - use sheet_tabs.product_sheet()"
