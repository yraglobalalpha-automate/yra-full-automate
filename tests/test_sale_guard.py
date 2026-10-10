"""sale_guard / listing_sweep: no listing of ours may carry a SALE price (2026-10-10).

OnBuy has no "empty" spelling for a sale (null is ignored, 0 and "" are refused); what works - proven on Makstore listings the same day - is to END
the sale: the listing's own sale price with a window in the past, no price and no stock in the update."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import listing_sweep as ls
import sale_guard as g

NOW = lambda s: None            # noqa: E731 - sleep stand-in


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text or str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeOnBuy:
    site_id, seller_id = 2000, 99

    def __init__(self, pages=None, put_answers=None):
        self.pages = pages or {}            # offset -> list of listing dicts (None = answer an empty page)
        self.put_answers = list(put_answers or [])
        self.gets, self.puts = [], []

    def _send(self, method, url, *, what, **kw):
        if method == "GET":
            off = kw["params"]["offset"]
            self.gets.append(off)
            return Resp(200, {"results": self._page(off)})
        self.puts.append(kw["json"])
        if self.put_answers:
            ans = self.put_answers.pop(0)
            return ans if isinstance(ans, Resp) else Resp(200, {"success": True, "results": ans})
        return Resp(200, {"success": True, "results": [{"sku": i["sku"], "product_listing_id": "1"} for i in kw["json"]["listings"]]})

    def _page(self, off):
        v = self.pages.get(off, [])
        if v and isinstance(v[0], list):          # a queue of answers: first call glitchy, second call right
            return v.pop(0) if len(v) > 1 else v[0]
        return v


def rec(sku, price="10.00", sale="9.50", start="2026-10-05 00:00:00", end="2026-10-10 00:00:00"):
    return {"sku": sku, "price": price, "stock": 5, "sale_price": sale, "sale_start_date": start, "sale_end_date": end}


# ---------------------------------------------------------------- what counts as a sale
def test_a_listing_has_a_sale_only_with_a_sale_price_above_zero():
    assert ls.has_sale(rec("1"))
    for none in ({}, {"sale_price": None}, {"sale_price": ""}, {"sale_price": "0.00"}, {"sale_price": 0}, {"sale_price": "None"}, {"sale_price": "abc"}, None):
        assert not ls.has_sale(none)


# ---------------------------------------------------------------- the update that ends a sale
def test_the_clearing_item_ends_the_sale_with_a_past_window_and_carries_no_price_or_stock():
    item = g.clearing_item(rec("111", price="67.99", sale="65.11"))
    assert item == {"sku": "111", "boost_marketing_commission": 0, "sale_price": "65.11",
                    "sale_start_date": "2026-09-01 00:00:00", "sale_end_date": "2026-09-02 00:00:00"}
    assert "price" not in item and "stock" not in item


def test_the_window_lies_in_the_past_and_is_ordered():
    assert g.PAST_START < g.PAST_END < "2026-10-01"


def test_a_sale_above_the_price_is_brought_down_to_the_price():
    assert g.clearing_item(rec("1", price="10.00", sale="168.41"))["sale_price"] == "10.00"
    assert g.clearing_item(rec("1", price="0", sale="5"))["sale_price"] == "5.00"


def test_a_listing_without_a_sale_gets_no_item():
    assert g.clearing_item({"sku": "1", "price": "10", "sale_price": None}) is None
    assert g.clearing_item({"sku": "", "price": "10", "sale_price": "5"}) is None


def test_chunks_split_the_list_at_the_batch_size():
    assert [len(c) for c in g.chunks(list(range(1007)), 500)] == [500, 500, 7]
    assert list(g.chunks([], 500)) == []


def test_a_dry_run_sends_nothing():
    onbuy = FakeOnBuy()
    stats = g.end_sales(onbuy, [rec("1"), rec("2")], dry_run=True, sleep=NOW)
    assert stats["planned"] == 2 and stats["sent"] == 0 and onbuy.puts == []


def test_a_live_run_sends_one_put_per_chunk_and_counts_what_was_accepted():
    onbuy = FakeOnBuy()
    rows = [rec(str(i)) for i in range(7)] + [{"sku": "no-sale", "price": "5", "sale_price": None}]
    stats = g.end_sales(onbuy, rows, dry_run=False, chunk=3, sleep=NOW)
    assert [len(p["listings"]) for p in onbuy.puts] == [3, 3, 1]
    assert stats["ok"] == 7 and stats["errors"] == 0 and stats["sent"] == 7
    assert onbuy.puts[0]["site_id"] == 2000 and onbuy.puts[0]["seller_id"] == 99
    assert all("price" not in i and "stock" not in i for p in onbuy.puts for i in p["listings"])


def test_per_item_errors_are_counted_and_named():
    onbuy = FakeOnBuy(put_answers=[[{"sku": "1", "product_listing_id": "1"},
                                    {"sku": "2", "error": "SKU does not exist"},
                                    {"sku": "3", "error": "SKU does not exist"}]])
    stats = g.end_sales(onbuy, [rec("1"), rec("2"), rec("3")], dry_run=False, sleep=NOW)
    assert (stats["ok"], stats["errors"]) == (1, 2)
    assert stats["error_text"]["SKU does not exist"] == 2 and stats["ended_skus"] == ["1"]


def test_max_fix_caps_the_listings_ended_in_one_run():
    onbuy = FakeOnBuy()
    stats = g.end_sales(onbuy, [rec(str(i)) for i in range(10)], dry_run=False, max_fix=4, sleep=NOW)
    assert stats["sent"] == 4 and sum(len(p["listings"]) for p in onbuy.puts) == 4


def test_a_rate_limited_chunk_waits_and_retries_then_succeeds():
    waits = []
    onbuy = FakeOnBuy(put_answers=[Resp(429, {}, "slow down"), Resp(503, {}, "busy"), [{"sku": "1", "product_listing_id": "1"}]])
    stats = g.end_sales(onbuy, [rec("1")], dry_run=False, sleep=waits.append)
    assert stats["ok"] == 1 and len(onbuy.puts) == 3 and waits.count(120) == 2


def test_a_chunk_that_keeps_failing_is_reported_not_raised():
    onbuy = FakeOnBuy(put_answers=[Resp(400, {}, "bad request")])
    stats = g.end_sales(onbuy, [rec("1"), rec("2")], dry_run=False, sleep=NOW)
    assert stats["failed_requests"] == 1 and stats["errors"] == 2 and stats["ok"] == 0
    assert any("HTTP 400" in k for k in stats["error_text"])


def test_describe_summarises_dates_and_ratios_without_costs():
    lines = g.describe([rec("1", price="10", sale="9.5"), rec("2", price="10", sale="8")])
    text = "\n".join(lines)
    assert "2026-10-05" in text and "0.7-0.9" in text and "0.9-0.99" in text and "cost" not in text.lower()


def test_spread_takes_evenly_spaced_samples():
    assert g.spread(list(range(100)), 5) == [0, 20, 40, 60, 80]
    assert g.spread([1, 2], 5) == [1, 2] and g.spread([], 5) == [] and g.spread([1], 0) == []


# ---------------------------------------------------------------- the sweep
def full(start, n):
    return [{"sku": str(start + i), "price": "1.00"} for i in range(n)]


def test_a_sweep_reads_every_page_and_stops_after_two_empty_ones():
    onbuy = FakeOnBuy(pages={0: full(0, 100), 100: full(100, 100), 200: full(200, 37)})
    out, short, meta, pages = ls.sweep(onbuy, limit=100, sleep=NOW)
    assert len(out) == 237 and short == [(200, 37)]
    assert onbuy.gets[:3] == [0, 100, 200]
    assert onbuy.gets.count(300) >= 1 and onbuy.gets[-1] == 400


def test_a_glitchy_short_page_in_the_middle_does_not_end_the_sweep():
    # the page at offset 100 comes back with 99 items once, 100 on the re-fetch; a shorter answer in the middle must not stop the sweep either
    onbuy = FakeOnBuy(pages={0: full(0, 100), 100: [full(100, 99), full(100, 100)], 200: full(200, 100), 300: full(300, 10)})
    out, short, _meta, _pages = ls.sweep(onbuy, limit=100, sleep=NOW)
    assert len(out) == 310 and short == [(300, 10)]


def test_a_really_short_page_in_the_middle_is_noted_and_the_sweep_goes_on():
    onbuy = FakeOnBuy(pages={0: full(0, 100), 100: full(100, 98), 200: full(200, 100), 300: full(300, 5)})
    out, short, _meta, _pages = ls.sweep(onbuy, limit=100, sleep=NOW)
    assert len(out) == 303 and short == [(100, 98), (300, 5)]


def test_a_repeated_sku_is_kept_once():
    onbuy = FakeOnBuy(pages={0: full(0, 100), 100: full(50, 100)})          # offset paging over a moving list repeats 50 of them
    out, _short, _meta, _pages = ls.sweep(onbuy, limit=100, sleep=NOW)
    assert len(out) == 150 and len({r["sku"] for r in out}) == 150


# ---------------------------------------------------------------- census + dump
def test_the_census_summarises_a_sweep_and_survives_odd_records():
    listings = [rec("1", price="10", sale="9.5"),
                {"sku": "2", "price": "5", "stock": 0, "sale_price": None, "created_at": "2026-10-06 10:00:00"},
                {"sku": "3", "price": None, "sale_price": "x"}]
    text = " | ".join(ls.census_lines(listings, [(100, 98)], {"metadata": {"total": 3}}))
    assert "3 distinct listings, 1 with a sale price" in text and "(100, 98)" in text and "2026-10-06" in text and "0.950" in text


def test_the_dump_keeps_selling_facts_only(tmp_path):
    import json
    path = tmp_path / "dump.json"
    ls.dump([{**rec("1"), "name": "a product name", "cost": 3.2}], str(path))
    row = json.loads(path.read_text(encoding="utf-8"))[0]
    assert row["sku"] == "1" and row["sale_price"] == "9.50" and "name" not in row and "cost" not in row
