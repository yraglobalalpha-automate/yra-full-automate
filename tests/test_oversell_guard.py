"""oversell_guard: OnBuy stock must be 0 wherever the sheet says 0 - and it must
cost almost nothing in API terms (2026-10-02)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gspread
import pytest

import oversell_guard as g
from retry_utils import RateLimitError

HEADER = ["SKU", "Supplier URL", "Title", "Stock", "Status", "Sync Status", "OnBuy Product Created", "Selling Price (£)"]


def row(sku, stock, status="INACTIVE", sync="Synced", created=True, price=20.0, tab="Sheet1", n=2):
    return {"tab": tab, "row": n, "sku": sku, "stock": stock, "status": status, "sync": sync,
            "created": created, "price": price}


def names(pairs):
    return [s for s, _p in pairs]


# ---------------------------------------------------------------- candidates
def test_a_live_priced_row_that_says_stock_zero_is_a_candidate_with_its_sheet_price():
    out, st = g.candidates([row("111", 0, price=79.64)])
    assert out == [("111", 79.64)] and st["eligible"] == 1


def test_in_stock_blank_and_unfetched_rows_are_left_alone():
    out, _ = g.candidates([row("1", 5, status="ACTIVE"), row("2", None), row("3", 1, status="ACTIVE")])
    assert out == []


def test_stock_and_status_must_agree():
    out, st = g.candidates([row("111", 0, status="ACTIVE")])
    assert out == [] and st["status_disagrees"] == 1
    out2, _ = g.candidates([row("222", 0, status="")])      # Status column empty: Stock alone decides
    assert names(out2) == ["222"]


def test_statuses_follow_the_sync_oos_pass_including_frozen_but_live():
    rows = [row("1", 0, sync="Pending Approval"), row("2", 0, sync="Awaiting OnBuy go-live"),
            row("3", 0, sync="Failed: supplier link already used on row 9", created=True),
            row("4", 0, sync="Failed: supplier link already used on row 9", created=False),
            row("5", 0, sync="Skipped: dead eBay link"), row("6", 0, sync="")]
    assert names(g.candidates(rows)[0]) == ["1", "2", "3"]


def test_a_sku_on_more_than_one_row_is_never_touched():
    rows = [row("111", 0, tab="Sheet1"), row("111", 5, status="ACTIVE", tab="Amazon")]
    out, st = g.candidates(rows)
    assert out == [] and st["repeated_sku"] == 1


def test_rows_without_a_usable_price_are_skipped_not_given_a_placeholder():
    out, st = g.candidates([row("1", 0, price=0.0), row("2", 0, price=0.5), row("3", 0, price=1.0)])
    assert names(out) == ["3"] and st["no_price"] == 1 and st["below_min_price"] == 1


# ------------------------------------------------------------------- breaker
def test_breaker_trips_only_when_out_of_stock_looks_like_a_broken_read():
    assert not g.tripped(300, 6000)                      # a normal day
    assert not g.tripped(990, 1000)                      # near-total but under the absolute floor
    assert not g.tripped(2375, 4395)                     # OpenMaal's real 54%: a market, not a broken read
    assert g.tripped(5000, 6000)                         # 83% of a big catalogue
    assert not g.tripped(10, 0)                          # no created flags at all: never trips on nothing


# ------------------------------------------------------------- fakes for run()
def colnum(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


class FakeWS:
    def __init__(self, title, rows):
        self.title, self._rows, self.reads = title, rows, 0

    def row_values(self, n):
        return list(HEADER)

    def batch_get(self, ranges):
        self.reads += 1
        out = []
        for rng in ranges:
            idx = colnum(re.match(r"([A-Z]+)2:", rng).group(1)) - 1
            col = [[r[idx]] if idx < len(r) and r[idx] != "" else [] for r in self._rows]
            while col and not col[-1]:
                col.pop()                                  # Sheets drops trailing blanks
            out.append(col)
        return out


class FakeBook:
    def __init__(self, tabs):
        self._tabs = {t.title: t for t in tabs}

    def worksheets(self):
        return list(self._tabs.values())

    def worksheet(self, name):
        if name not in self._tabs:
            raise gspread.exceptions.WorksheetNotFound(name)
        return self._tabs[name]


def sheet_row(sku, stock, status="INACTIVE", sync="Synced", created="TRUE", price="25.5"):
    return [sku, "https://www.ebay.co.uk/itm/1", "title", str(stock), status, sync, created, price]


class FakeOnBuy:
    def __init__(self, fail_skus=(), raise_exc=None):
        self.calls, self.fail_skus, self.raise_exc = [], set(fail_skus), raise_exc

    def update_listings_by_sku_batch(self, listings):
        self.calls.append(list(listings))
        if self.raise_exc:
            raise self.raise_exc
        return [{"sku": s, **({"error": "SKU does not exist"} if s in self.fail_skus else {})} for s, _p, _st in listings]


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ONBUY_API_PUSH_ENABLED", "true")
    monkeypatch.delenv("OVERSELL_GUARD", raising=False)
    monkeypatch.delenv("GUARD_DRY_RUN", raising=False)


def book_with(rows_sheet1, rows_amazon=()):
    return FakeBook([FakeWS("Sheet1", rows_sheet1), FakeWS("Amazon", list(rows_amazon))])


# ------------------------------------------------------------------ read_rows
def test_read_rows_aligns_cells_even_with_blanks_in_the_middle_and_trailing_trimmed():
    b = book_with([sheet_row("111", 0), ["", "", "", "", "", "", "", ""], sheet_row("333", 4, status="ACTIVE", price="")])
    rows = g.read_rows(b)
    assert [(r["sku"], r["stock"], r["row"]) for r in rows] == [("111", 0, 2), ("333", 4, 4)]
    assert rows[1]["price"] == 0.0 and rows[0]["created"] is True


def test_read_rows_reads_both_product_tabs_with_small_batch_reads_only():
    b = book_with([sheet_row("111", 0)], [sheet_row("A1", 0)])
    rows = g.read_rows(b)
    assert {r["tab"] for r in rows} == {"Sheet1", "Amazon"}
    assert all(t.reads == 1 for t in b.worksheets())       # ONE batch read per tab


# ------------------------------------------------------------------------ run
def test_run_re_zeroes_sheet_oos_rows_in_one_put_and_nothing_else():
    b = book_with([sheet_row("111", 0, price="79.64"), sheet_row("222", 5, status="ACTIVE"), sheet_row("333", 0)])
    ob = FakeOnBuy()
    stats = g.run(b, ob)
    assert ob.calls == [[("111", 79.64, 0), ("333", 25.5, 0)]]
    assert stats["requests"] == 1 and stats["pushed"] == 2 and stats["bounced"] == 0


def test_run_batches_at_500_so_a_big_day_still_costs_only_a_few_requests(monkeypatch):
    b = book_with([sheet_row(f"{i:012d}", 0) for i in range(1, 1204)])
    monkeypatch.setattr(g, "BREAKER_MIN", 10 ** 9)         # breaker is tested separately
    ob = FakeOnBuy()
    stats = g.run(b, ob)
    assert [len(c) for c in ob.calls] == [500, 500, 203] and stats["requests"] == 3


def test_dry_run_pushes_nothing():
    ob = FakeOnBuy()
    stats = g.run(book_with([sheet_row("111", 0)]), ob, dry_run=True)
    assert ob.calls == [] and stats["candidates"] == 1


def test_a_rate_limited_push_is_dropped_quietly_and_never_raises():
    ob = FakeOnBuy(raise_exc=RateLimitError())
    stats = g.run(book_with([sheet_row("111", 0)]), ob)
    assert stats["pushed"] == 0 and stats["requests"] == 1 and stats["skipped"] == ""


def test_per_item_bounces_are_counted_not_fatal():
    ob = FakeOnBuy(fail_skus=["222"])
    stats = g.run(book_with([sheet_row("111", 0), sheet_row("222", 0)]), ob)
    assert stats["pushed"] == 1 and stats["bounced"] == 1


def test_kill_switch_and_push_switch_are_honoured(monkeypatch):
    ob = FakeOnBuy()
    monkeypatch.setenv("OVERSELL_GUARD", "0")
    assert g.run(book_with([sheet_row("111", 0)]), ob)["skipped"].startswith("disabled")
    monkeypatch.delenv("OVERSELL_GUARD")
    monkeypatch.setenv("ONBUY_API_PUSH_ENABLED", "false")
    assert g.run(book_with([sheet_row("111", 0)]), ob)["skipped"].startswith("OnBuy pushes")
    assert ob.calls == []


def test_breaker_stops_a_mass_zero_and_pushes_nothing(monkeypatch):
    monkeypatch.setattr(g, "BREAKER_MIN", 2)
    monkeypatch.setattr(g, "BREAKER_FRACTION", 0.5)
    ob = FakeOnBuy()
    stats = g.run(book_with([sheet_row("1", 0), sheet_row("2", 0), sheet_row("3", 0), sheet_row("4", 5, status="ACTIVE")]), ob)
    assert stats["skipped"] == "breaker" and ob.calls == []


def test_an_unreadable_sheet_skips_the_pass_without_raising():
    class Broken:
        def worksheet(self, name):
            raise RuntimeError("sheets down")

        def worksheets(self):
            return []
    stats = g.run(Broken(), FakeOnBuy())
    assert stats["skipped"] == "error"
