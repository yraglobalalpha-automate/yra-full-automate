"""purge_dead_rows.py - the out-of-stock and brand-blocked rows of the eBay tab are deleted from OnBuy and the sheet (user 2026-10-12: "All the out of stock and
brand blocked rows of eBay should be immediately deleted from store as well as sheet").

What these tests pin: which rows count as dead (brand verdict + unchanged brand; Stock 0 on a visited eBay row), that an out-of-stock row is only deleted when eBay
confirms it NOW (definitive reasons at once, ambiguous ones twice, an item in stock or a failed fetch never), what is never touched (duplicates, protected SKUs, open
orders, unreadable orders), the plausibility breakers, the OnBuy batch outcomes, which rows may leave the sheet, and that get_ebay_data now says WHY an item is gone."""
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("gspread")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate_xml as gx  # noqa: E402
import purge_dead_rows as pd  # noqa: E402
from retry_utils import PermanentError, RateLimitError, TransientError  # noqa: E402

TODAY = date(2026, 10, 12)
EBAY = "https://www.ebay.co.uk/itm/123456789012"
FLAG = ("BRAND BLOCKED (2026-10-01) - OnBuy says the brand 'SOL' is owned by another seller, so this product cannot be listed under it. "
        "Replace the link with a different product, or correct the Brand cell - the row retries as soon as the brand changes, and is re-tested anyway after 7 days.")


def row(n=2, sku="111", stock=None, sync="Synced", brand="Acme", url=EBAY, checked="2026-10-11 10:00:00", created=False, title="A thing"):
    return {"tab": "Sheet1", "row": n, "sku": sku, "url": url, "stock": stock, "sync": sync, "brand": brand, "checked": checked, "created": created,
            "title": title, "cells": [sku, title, "9.99"]}


# ---------------------------------------------------------------- what is a dead row
def test_a_brand_verdict_counts_while_the_brand_cell_still_holds_the_refused_brand():
    assert pd.is_brand_blocked(FLAG, "SOL", TODAY)
    assert pd.is_brand_blocked(FLAG, "sol", TODAY)                                  # case-insensitive, like the sync's own test
    assert pd.is_brand_blocked(FLAG.replace("2026-10-01", "2026-08-01"), "SOL", TODAY)   # the 7-day retry no longer applies: it is deleted, not re-tested


def test_a_corrected_brand_lifts_the_block():
    assert not pd.is_brand_blocked(FLAG, "Geepas", TODAY)


def test_the_raw_refusal_counts_and_other_failures_do_not():
    assert pd.is_brand_blocked("Failed: 400 supplied brand is owned by another seller", "Whatever", TODAY)
    assert not pd.is_brand_blocked("Failed: SKU appears on 2 sheet rows - one product per SKU", "SOL", TODAY)
    assert not pd.is_brand_blocked("Synced", "SOL", TODAY)
    assert not pd.is_brand_blocked("", "", TODAY)


def test_stock_zero_on_a_visited_ebay_row_is_out_of_stock():
    assert pd.classify(row(stock=0), TODAY) == "oos"


@pytest.mark.parametrize("over", [{"stock": None}, {"stock": 3}, {"stock": 0, "checked": ""}, {"stock": 0, "url": "https://www.amazon.co.uk/dp/B0ABCDEFGH"},
                                   {"stock": 0, "url": ""}])
def test_everything_else_is_not_out_of_stock(over):
    assert pd.classify(row(**over), TODAY) is None


def test_a_brand_verdict_wins_over_stock_zero():
    assert pd.classify(row(stock=0, sync=FLAG, brand="SOL"), TODAY) == "brand"


# ---------------------------------------------------------------- the plan
def test_plan_keeps_candidates_and_holds_what_must_not_be_touched():
    rows = [row(2, "A", stock=0), row(3, "B", sync=FLAG, brand="SOL"), row(4, "", stock=0), row(5, "D", stock=0), row(6, "E", stock=0), row(7, "F", stock=5),
            row(8, "0777", stock=0)]
    cands, held = pd.plan_purge(rows, protected=frozenset({"777", "0777"}), counts={"D": 2, "A": 1}, kinds=("brand", "oos"), today=TODAY)
    assert [(c["sku"], c["kind"]) for c in cands] == [("A", "oos"), ("B", "brand"), ("E", "oos")]
    assert sorted(w for _r, _k, w in held) == ["SKU is on more than one sheet row", "no SKU", "protected listing (protected_skus.txt)"]


def test_the_kinds_switch_limits_what_is_looked_at():
    rows = [row(2, "A", stock=0), row(3, "B", sync=FLAG, brand="SOL")]
    assert [c["kind"] for c in pd.plan_purge(rows, kinds=("brand",), today=TODAY)[0]] == ["brand"]
    assert [c["kind"] for c in pd.plan_purge(rows, kinds=("oos",), today=TODAY)[0]] == ["oos"]


def test_open_orders_hold_a_row_whatever_the_zero_spelling():
    cands = [dict(row(2, "0123", stock=0), kind="oos"), dict(row(3, "999", stock=0), kind="oos")]
    keep, held = pd.hold_open_orders(cands, {"123"})
    assert [c["sku"] for c in keep] == ["999"] and held[0][2] == "open order (awaiting dispatch)"


def test_when_the_orders_cannot_be_read_anything_that_may_be_live_is_held():
    cands = [dict(row(2, "A", stock=0), kind="oos"), dict(row(3, "B", sync=FLAG, created=True), kind="brand"), dict(row(4, "C", sync=FLAG, created=False), kind="brand")]
    keep, held = pd.hold_open_orders(cands, None)
    assert [c["sku"] for c in keep] == ["C"]                                         # only a never-created brand-blocked row cannot be on an order
    assert len(held) == 2


# ---------------------------------------------------------------- plausibility breakers
def test_the_mass_breaker():
    assert pd.mass_tripped(400, 1000, allow=False, min_count=300, fraction=0.35)
    assert not pd.mass_tripped(400, 5000, allow=False, min_count=300, fraction=0.35)       # 8% of the tab: a real clean-up
    assert not pd.mass_tripped(100, 200, allow=False, min_count=300, fraction=0.35)        # small tab: below the absolute floor
    assert not pd.mass_tripped(900, 1000, allow=True, min_count=300, fraction=0.35)        # a deliberate mass clean-up


def test_a_stock_column_that_is_mostly_blank_is_an_unread_tab():
    ok = [row(n, str(n), stock=1) for n in range(200)]
    assert not pd.stock_column_unread(ok)
    assert pd.stock_column_unread([row(n, str(n), stock=None) for n in range(200)])
    assert not pd.stock_column_unread([row(n, str(n), stock=None) for n in range(20)])      # too few rows to judge


# ---------------------------------------------------------------- the eBay confirmation
class Seq:
    """fetch() that answers from a list: (available, reason) tuples or exceptions."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def __call__(self, url):
        self.calls += 1
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        available, reason = a
        return available, ({"reason": reason} if reason else {})


@pytest.mark.parametrize("reason", ["removed", "ended", "out_of_stock"])
def test_a_definitive_reason_is_final_after_one_call(reason):
    f = Seq((False, reason))
    assert pd.oos_verdict(f, EBAY, sleep=lambda s: None) == ("gone", reason, 1) and f.calls == 1


@pytest.mark.parametrize("reason", ["no_price", "no_availability", ""])
def test_an_ambiguous_answer_is_looked_at_twice_and_deleted_only_when_it_repeats(reason):
    slept = []
    f = Seq((False, reason), (False, reason))
    verdict, why, calls = pd.oos_verdict(f, EBAY, sleep=slept.append)
    assert (verdict, calls) == ("gone", 2) and "second look" in why and slept == [3]


def test_eBay_nulls_at_random_so_an_ambiguous_answer_followed_by_in_stock_keeps_the_row():
    assert pd.oos_verdict(Seq((False, "no_availability"), (True, "")), EBAY, sleep=lambda s: None)[:1] == ("alive",)


def test_an_item_eBay_shows_in_stock_is_never_deleted():
    assert pd.oos_verdict(Seq((True, "")), EBAY, sleep=lambda s: None) == ("alive", "eBay shows the item in stock", 1)


@pytest.mark.parametrize("exc", [TransientError("blip"), PermanentError("403"), ConnectionError("x")])
def test_a_failed_fetch_deletes_nothing(exc):
    if isinstance(exc, ConnectionError):
        import requests
        exc = requests.ConnectionError("down")
    verdict, why, _ = pd.oos_verdict(Seq(exc), EBAY, sleep=lambda s: None)
    assert verdict == "unknown" and "fetch failed" in why


def test_the_rate_limit_stops_the_confirmations():
    with pytest.raises(RateLimitError):
        pd.oos_verdict(Seq(RateLimitError("429")), EBAY, sleep=lambda s: None)


# ---------------------------------------------------------------- choosing this run's rows
def cand(n, sku, kind, checked="2026-10-11 10:00:00"):
    return dict(row(n, sku, stock=0 if kind == "oos" else None, checked=checked, sync=FLAG if kind == "brand" else "Synced", brand="SOL" if kind == "brand" else "X"), kind=kind)


def test_brand_rows_go_first_and_out_of_stock_rows_freshest_first(monkeypatch):
    monkeypatch.setattr(pd.time, "sleep", lambda s: None)
    cands = [cand(2, "old", "oos", "2026-09-01 10:00:00"), cand(3, "B", "brand"), cand(4, "new", "oos", "2026-10-12 06:00:00")]
    f = Seq((False, "removed"), (False, "removed"))
    final, stats = pd.select_final(cands, f, 10, 10, sleep=lambda s: None)
    assert [c["sku"] for c in final] == ["B", "new", "old"] and stats["gone"] == 2 and stats["calls"] == 2


def test_only_confirmed_rows_are_selected_and_the_verdicts_are_counted(monkeypatch):
    monkeypatch.setattr(pd.time, "sleep", lambda s: None)
    cands = [cand(2, "a", "oos"), cand(3, "b", "oos"), cand(4, "c", "oos")]
    f = Seq((False, "ended"), (True, ""), TransientError("blip"))
    final, stats = pd.select_final(cands, f, 10, 10, sleep=lambda s: None)
    assert [c["sku"] for c in final] == ["a"] and (stats["gone"], stats["alive"], stats["unknown"]) == (1, 1, 1)
    assert final[0]["confirmed"] == "ended"


def test_the_row_cap_and_the_check_cap_leave_the_rest_for_the_next_run(monkeypatch):
    monkeypatch.setattr(pd.time, "sleep", lambda s: None)
    cands = [cand(n, f"s{n}", "oos", f"2026-10-11 10:{n:02d}:00") for n in range(2, 12)]
    f = Seq(*[(False, "removed")] * 10)
    final, stats = pd.select_final(cands, f, 3, 100, sleep=lambda s: None)
    assert len(final) == 3 and stats["unconfirmed"] == 7 and f.calls == 3
    f = Seq(*[(False, "removed")] * 10)
    final, stats = pd.select_final(cands, f, 100, 4, sleep=lambda s: None)
    assert len(final) == 4 and stats["unconfirmed"] == 6


def test_the_rate_limit_ends_the_confirmations_but_keeps_what_was_confirmed(monkeypatch):
    monkeypatch.setattr(pd.time, "sleep", lambda s: None)
    cands = [cand(2, "a", "oos"), cand(3, "b", "oos"), cand(4, "c", "oos")]
    f = Seq((False, "removed"), RateLimitError("429"))
    final, stats = pd.select_final(cands, f, 10, 10, sleep=lambda s: None)
    assert [c["sku"] for c in final] == ["a"] and stats["rate_limited"] and stats["unconfirmed"] == 2


# ---------------------------------------------------------------- OnBuy
def test_open_order_skus_reads_every_page_and_spells_zeros_both_ways():
    pages = [{"results": [{"order_id": "T1", "products": [{"sku": "0123"}]}] * 100},
             {"results": [{"order_id": "T2", "products": [{"sku": "456"}]}]}]
    calls = []

    def _send(method, url, what=None, params=None, timeout=None):
        calls.append(params["offset"])
        body = pages[len(calls) - 1]
        return SimpleNamespace(status_code=200, json=lambda: body, text="")

    onbuy = SimpleNamespace(_send=_send, site_id=2000)
    assert pd.open_order_skus(onbuy, sleep=lambda s: None) == {"123", "456"} and calls == [0, 100]


def test_open_order_skus_raises_when_the_orders_cannot_be_read():
    onbuy = SimpleNamespace(_send=lambda *a, **k: SimpleNamespace(status_code=500, json=lambda: {}, text="boom"), site_id=1)
    with pytest.raises(RuntimeError):
        pd.open_order_skus(onbuy, sleep=lambda s: None)


def fake_send(answers):
    """dlb.send replacement: per batch -> a results node built from `answers` {sku: 'ok'|'gone'|'suspended'|None(=unanswered)}."""
    sent = []

    def send(onbuy, wire):
        sent.append(list(wire))
        results = []
        for s in wire:
            a = answers.get(s, "ok")
            if a is None:
                continue
            results.append({"sku": s, "status": "ok"} if a == "ok" else {"sku": s, "error": {"gone": "SKU not found", "suspended": "Listing is suspended"}.get(a, a)})
        return 200, {"results": results}, "text"
    return send, sent


def test_delete_on_onbuy_reads_each_skus_own_answer(monkeypatch):
    send, sent = fake_send({"b": "gone", "c": "suspended"})
    monkeypatch.setattr(pd.dlb, "send", send)
    out = pd.delete_on_onbuy(SimpleNamespace(), ["a", "b", "c"], sleep=lambda s: None)
    assert out == {"a": "ok", "b": "gone", "c": "suspended"} and len(sent) == 1


def test_a_batch_onbuy_does_not_answer_per_sku_is_split_and_singles_are_capped(monkeypatch):
    send, sent = fake_send({s: None for s in "abcdefghij"})
    monkeypatch.setattr(pd.dlb, "send", send)
    out = pd.delete_on_onbuy(SimpleNamespace(), list("abcdefghij"), max_singles=3, sleep=lambda s: None)
    assert all(v.startswith("error") or v == "not attempted" for v in out.values())
    assert sum(1 for v in out.values() if v == "not attempted") == 7 and len([b for b in sent if len(b) == 1]) == 3


def test_a_first_clean_batch_lets_the_batch_size_grow(monkeypatch):
    send, sent = fake_send({})
    monkeypatch.setattr(pd.dlb, "send", send)
    pd.delete_on_onbuy(SimpleNamespace(), [f"s{n}" for n in range(250)], sleep=lambda s: None)
    assert [len(b) for b in sent] == [10, 100, 100, 40]


def test_which_rows_may_leave_the_sheet():
    live_oos, live_brand, never_brand = (dict(row(), kind="oos", created=True), dict(row(), kind="brand", created=True), dict(row(), kind="brand", created=False))
    for outcome in ("ok", "gone", "suspended"):
        assert pd.may_remove_row(live_oos, outcome)
    assert not pd.may_remove_row(live_oos, "error: HTTP 500") and not pd.may_remove_row(live_oos, "not attempted")
    assert not pd.may_remove_row(live_brand, "error: HTTP 500")
    assert pd.may_remove_row(never_brand, "error: HTTP 500")                      # no listing to delete: nothing OnBuy could still hold


# ---------------------------------------------------------------- the sheet
class FakeWs:
    def __init__(self, skus, sheet_id=7):
        self.col = ["SKU"] + list(skus)
        self.id, self.requests = sheet_id, []
        self.spreadsheet = SimpleNamespace(batch_update=lambda body: self.requests.extend(body["requests"]))

    def row_values(self, n):
        return ["SKU", "Title"]

    def col_values(self, n):
        return list(self.col)


class FakeBook:
    def __init__(self, ws):
        self.ws = ws

    def worksheet(self, title):
        return self.ws


def test_rows_are_deleted_bottom_up_by_a_fresh_read_and_a_repeated_sku_is_skipped():
    ws = FakeWs(["a", "b", "c", "d", "d"])
    deleted, skipped = pd.delete_rows(FakeBook(ws), "Sheet1", ["a", "c", "d", "gone"])
    assert deleted == 2 and sorted(skipped) == [("d", 2), ("gone", 0)]
    starts = [r["deleteDimension"]["range"]["startIndex"] for r in ws.requests]
    assert starts == [3, 1]                                                       # sheet row 4 (c) before row 2 (a), 0-based: deleting bottom-up never shifts a pending row


class TabWs:
    def __init__(self, title, values):
        self.title, self._v = title, values

    def get_all_values(self):
        return self._v


def test_the_suspended_listings_are_read_back_from_the_purged_tabs(capsys):
    head = ["bk_removed_utc", "bk_tab", "bk_row", "bk_reason", "bk_SKU", "bk_Title"]
    tab = TabWs("Purged 2026-10", [head,
                                   ["t", "Sheet1", "5", "out of stock on eBay (removed) | OnBuy: ok", "A1", "fine"],
                                   ["t", "Sheet1", "6", "brand blocked | OnBuy: suspended", "B2", "a stuck one"],
                                   ["t", "Sheet1", "7", "out of stock on eBay (ended) | OnBuy: suspended", "C3", "another stuck one"]])
    other = TabWs("Sheet1", [["SKU"], ["X"]])
    book = SimpleNamespace(worksheets=lambda: [other, tab])
    assert pd.list_suspended(book) == [("B2", "a stuck one"), ("C3", "another stuck one")]
    out = capsys.readouterr().out
    assert "2 purged SKU(s)" in out and "SUSPENDED B2  a stuck one" in out and "A1" not in out


def test_the_log_never_carries_a_cost(capsys):
    r = dict(row(5, "S5", stock=0), kind="oos", confirmed="removed")
    r["cells"] = ["S5", "Title", "COST-12.34"]
    pd.report([r], [r], [], [r], {"gone": 1, "alive": 0, "unknown": 0, "calls": 1, "unconfirmed": 0, "rate_limited": False, "why": pd.Counter({"removed": 1})})
    out = capsys.readouterr().out
    assert "COST-12.34" not in out and "row 5 SKU S5" in out and "TO DELETE THIS RUN: 1" in out


# ---------------------------------------------------------------- get_ebay_data says WHY
class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.headers, self.text = status, body if body is not None else {}, {}, ""

    def json(self):
        return self._body


def item(**over):
    base = {"title": "Widget", "price": {"value": "10.00", "currency": "GBP"}, "legacyItemId": "123456789012",
            "estimatedAvailabilities": [{"estimatedAvailabilityStatus": "IN_STOCK", "estimatedAvailableQuantity": 7}],
            "description": "<p>Spec</p>", "condition": "New", "localizedAspects": []}
    base.update(over)
    return base


def fetch_with(monkeypatch, status=200, body=None, url=EBAY):
    monkeypatch.setattr(gx.requests, "get", lambda u, headers=None, params=None, timeout=None: Resp(status, body))
    monkeypatch.setattr(gx, "validate_images", lambda urls, max_images=10: [u for u in urls if u][:max_images])
    return gx.get_ebay_data(url, "token")


@pytest.mark.parametrize("status,body,over,url,reason", [
    (404, None, None, EBAY, "removed"),
    (200, "item", {"itemEndDate": "2020-01-01T00:00:00.000Z"}, EBAY, "ended"),
    (200, "item", {"price": {"value": "0", "currency": "GBP"}}, EBAY, "no_price"),
    (200, "item", {"estimatedAvailabilities": []}, EBAY, "no_availability"),
    (200, "item", {"estimatedAvailabilities": [{"estimatedAvailabilityStatus": "OUT_OF_STOCK"}]}, EBAY, "out_of_stock"),
    (200, "item", {"estimatedAvailabilities": [{"estimatedAvailabilityStatus": "UNAVAILABLE"}]}, EBAY, "out_of_stock"),
    (200, "item", None, "https://www.ebay.co.uk/not-an-item-link", "bad_link"),
])
def test_get_ebay_data_names_the_reason_an_item_is_unavailable(monkeypatch, status, body, over, url, reason):
    available, data = fetch_with(monkeypatch, status, item(**(over or {})) if body == "item" else body, url)
    assert available is False and data["reason"] == reason and data["stock"] == 0


def test_an_available_item_carries_no_reason_and_the_blank_answer_is_unchanged_without_one(monkeypatch):
    available, data = fetch_with(monkeypatch, 200, item())
    assert available is True and "reason" not in data
    assert gx.empty_ebay_response() == {"stock": 0, "price": 0, "description": "", "main_image": "", "additional_images": [], "title": "", "brand": "",
                                        "product_code": "", "condition": ""}

# ---------------------------------------------------------------- 2026-10-13 hardening (found by the log check)
def test_a_purge_crash_is_mailed_and_re_raised(monkeypatch):
    sent = []
    monkeypatch.setattr(pd.notify, "send_alert_email", lambda subject, body, routine=False: sent.append((subject, body)))

    def boom():
        raise RuntimeError("sheet gone")

    with pytest.raises(RuntimeError):
        pd.guarded_main(boom)
    assert len(sent) == 1 and "crashed" in sent[0][0] and "sheet gone" in sent[0][1]


def test_a_deliberate_exit_is_not_mailed_as_a_crash(monkeypatch):
    sent = []
    monkeypatch.setattr(pd.notify, "send_alert_email", lambda subject, body, routine=False: sent.append(subject))

    def refuse():
        raise SystemExit("purge refused")

    with pytest.raises(SystemExit):
        pd.guarded_main(refuse)
    assert sent == []


def test_the_syncs_hosted_xml_read_is_retried_and_never_replaced_by_an_empty_feed_when_it_fails():
    text = (Path(__file__).resolve().parents[1] / "generate_xml.py").read_text(encoding="utf-8")
    assert "with_retry(sheet.get_all_records, what=\"feed read\"" in text
    assert "if _feed_rows is None:" in text and "feed_url = None" in text
    assert "storage.upload_feed(local_path=FEED_FILE, remote_path=FEED_FILE)" in text
    # the upload sits in the else branch of that test, so a failed read can never publish the empty feed
    assert text.index("if _feed_rows is None:") < text.index("storage.upload_feed(")
