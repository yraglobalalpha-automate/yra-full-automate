"""The two jobs that keep wrong-content listings at stock 0 (2026-10-05): the nightly audit's FIX and the zero step's selection."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit_price_drift as audit
import zero_stock_mismatched as zero


# ---------------------------------------------------------------- audit_price_drift.main

class FakeAuditOnBuy:
    pushed = []

    def authenticate(self):
        return True

    def update_listings_by_sku_batch(self, chunk):
        FakeAuditOnBuy.pushed.extend(chunk)
        return [{"sku": sku, "error": ""} for sku, _p, _s in chunk]


def _run_audit(monkeypatch, fix):
    FakeAuditOnBuy.pushed = []
    sheet = {"111": (19.99, 10, "Sheet1", 2),   # held, still sellable on OnBuy
             "222": (5.00, 4, "Sheet1", 3),     # not held, stock drifted
             "333": (8.00, 7, "Sheet1", 4)}     # held, already at stock 0
    live = {"111": (17.50, 10, "t"), "222": (5.00, 9, "t"), "333": (8.00, 0, "t")}
    monkeypatch.setattr(audit, "OnBuyClient", FakeAuditOnBuy)
    monkeypatch.setattr(audit, "sheet_rows", lambda: (sheet, set()))
    monkeypatch.setattr(audit, "live_listings", lambda onbuy: live)
    monkeypatch.setattr(audit.held_skus, "held_set", lambda *a, **k: {"111", "333"})
    monkeypatch.setattr(audit, "FIX", fix)
    audit.main()
    return sorted(FakeAuditOnBuy.pushed)


def test_audit_fix_takes_the_stock_off_held_listings_and_never_pushes_the_sheet_onto_them(monkeypatch):
    # 111 -> its OWN live price with stock 0 (not the sheet's 19.99 / 10); 333 already at 0 -> untouched; 222 -> normal drift fix
    assert _run_audit(monkeypatch, fix=True) == [("111", 17.50, 0), ("222", 5.00, 4)]


def test_audit_report_only_pushes_nothing(monkeypatch):
    assert _run_audit(monkeypatch, fix=False) == []


# ---------------------------------------------------------------- zero_stock_mismatched.main

class FakeSheet:
    title = "Sheet1"

    def __init__(self, rows):
        self.rows = rows

    def get_all_records(self):
        return [dict(r) for r in self.rows]

    def row_values(self, n):
        return ["SKU", "Title", "Selling Price (£)"]

    def col_values(self, n):
        return ["SKU"] + [str(r["SKU"]) for r in self.rows]


class FakeZeroOnBuy:
    calls = []

    def authenticate(self):
        return True

    def update_listing(self, sku, price, stock):
        FakeZeroOnBuy.calls.append((sku, price, stock))


def _run_zero(monkeypatch, zero_skus, only_listed, dry_run=False):
    FakeZeroOnBuy.calls = []
    remembered = []
    rows = [
        {"SKU": "100", "Title": "Red Garden Spade Steel Handle", "Selling Price (£)": 9.99},     # matches OnBuy: fine
        {"SKU": "200", "Title": "Cordless Leaf Blower Fan 21V", "Selling Price (£)": 20.0},      # OnBuy shows a kettle: mismatched
        {"SKU": "300", "Title": "ACME Mini 4K Projector Portable WiFi", "Selling Price (£)": 67.05},  # sibling title: passes the name check
        {"SKU": "400", "Title": "Standing Desk Frame Electric", "Selling Price (£)": 80.0},      # mismatched but already at stock 0
    ]
    items = [
        {"sku": "100", "name": "Red Garden Spade Steel Handle Digging", "stock": 5, "price": "9.99"},
        {"sku": "200", "name": "Stainless Steel Electric Kettle 1.7L", "stock": 6, "price": "20.00"},
        {"sku": "300", "name": "ACME 8K 4K Android Projector 1080P WiFi Bluetooth Home Theater", "stock": 10, "price": "67.05"},
        {"sku": "400", "name": "Ergonomic Office Chair Mesh", "stock": 0, "price": "80.00"},
    ]
    monkeypatch.setenv("GOOGLE_CREDENTIALS", "{}")
    monkeypatch.setattr(zero, "OnBuyClient", FakeZeroOnBuy)
    monkeypatch.setattr(zero.listings_cache, "load", lambda: items)
    monkeypatch.setattr(zero, "ServiceAccountCredentials", SimpleNamespace(from_json_keyfile_dict=lambda *a, **k: None))
    monkeypatch.setattr(zero, "gspread", SimpleNamespace(authorize=lambda creds: SimpleNamespace(open=lambda name: object())))
    monkeypatch.setattr(zero.sheet_tabs, "product_sheet", lambda book: FakeSheet(rows))
    monkeypatch.setattr(zero.time, "sleep", lambda s: None)
    monkeypatch.setattr(zero.held_skus, "remember", lambda skus, *a, **k: remembered.append(sorted(skus)) or len(skus))
    monkeypatch.setattr(zero, "DRY_RUN", dry_run)
    monkeypatch.setattr(zero, "ZERO_SKUS", set(zero_skus))
    monkeypatch.setattr(zero, "ZERO_ONLY_LISTED", only_listed)
    zero.main()
    return sorted(FakeZeroOnBuy.calls), remembered


def test_zero_default_zeroes_every_mismatched_listing_with_stock_and_holds_all_mismatched(monkeypatch):
    calls, remembered = _run_zero(monkeypatch, zero_skus=[], only_listed=False)
    assert calls == [("200", 20.0, 0)]                 # 300 passes the name check; 400 is already at 0; 100 matches
    assert remembered == [["200", "400"]]              # 400 is held too (the audit must not re-stock it)


def test_zero_forced_sku_that_passes_the_name_check_is_zeroed_alongside_the_mismatched(monkeypatch):
    calls, remembered = _run_zero(monkeypatch, zero_skus=["300"], only_listed=False)
    assert calls == [("200", 20.0, 0), ("300", 67.05, 0)]
    assert remembered == [["200", "300", "400"]]


def test_zero_only_listed_touches_nothing_but_the_named_skus(monkeypatch):
    calls, remembered = _run_zero(monkeypatch, zero_skus=["300"], only_listed=True)
    assert calls == [("300", 67.05, 0)]                # 200 is mismatched too, but was not named
    assert remembered == [["300"]]


def test_zero_dry_run_changes_and_remembers_nothing(monkeypatch):
    calls, remembered = _run_zero(monkeypatch, zero_skus=["300"], only_listed=True, dry_run=True)
    assert calls == [] and remembered == []
