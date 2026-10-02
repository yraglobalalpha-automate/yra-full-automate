"""removal_plan: what may be removed and what is held back (2026-10-02)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import removal_plan


def row(tab, n, sku, ident="", created=True, stock=0):
    return {"tab": tab, "row": n, "sku": sku, "ident": ident, "created": created, "stock": stock}


def names(rs):
    return [(r["tab"], r["row"]) for r in rs]


def test_listed_out_of_stock_rows_are_removed():
    rows = [row("Sheet1", 2, "111", "ebay:1"), row("Sheet1", 3, "222", "ebay:2")]
    out = removal_plan.plan(rows, ["111", "222"], include_live_dups=False)
    assert names(out["remove"]) == [("Sheet1", 2), ("Sheet1", 3)]
    assert out["held"] == [] and out["absent"] == []


def test_a_listed_row_that_is_back_in_stock_is_held():
    rows = [row("Sheet1", 2, "111", "ebay:1", stock=8), row("Sheet1", 3, "222", "ebay:2")]
    out = removal_plan.plan(rows, ["111", "222"], include_live_dups=False)
    assert names(out["remove"]) == [("Sheet1", 3)]
    assert out["held"][0][0]["sku"] == "111" and "back in stock" in out["held"][0][1]


def test_listed_sku_missing_from_the_sheet_is_reported_absent():
    out = removal_plan.plan([row("Sheet1", 2, "111", "ebay:1")], ["111", "999"], include_live_dups=False)
    assert out["absent"] == ["999"] and names(out["remove"]) == [("Sheet1", 2)]


def test_a_sku_on_two_rows_is_never_touched():
    rows = [row("Sheet1", 2, "111", "ebay:1"), row("Amazon", 2, "111", "amazon:B0X")]
    out = removal_plan.plan(rows, ["111"], include_live_dups=False)
    assert out["remove"] == [] and "more than one" in out["held"][0][1]


def test_live_duplicate_goes_while_the_live_keeper_stays():
    rows = [row("Sheet1", 2, "111", "ebay:1", stock=5), row("Sheet1", 9, "222", "ebay:1", stock=5)]
    out = removal_plan.plan(rows, [], include_live_dups=True)
    assert names(out["remove"]) == [("Sheet1", 9)]
    assert out["remove"][0]["reason"].startswith("live duplicate of Sheet1 row 2")


def test_first_row_wins_across_tabs_eBay_tab_before_Amazon():
    rows = [row("Sheet1", 2, "111", "amazon:B0X", stock=5), row("Amazon", 2, "222", "amazon:B0X", stock=5)]
    assert names(removal_plan.plan(rows, [], True)["remove"]) == [("Amazon", 2)]


def test_a_duplicate_that_is_not_live_is_not_a_live_duplicate():
    rows = [row("Sheet1", 2, "111", "ebay:1"), row("Sheet1", 9, "222", "ebay:1", created=False)]
    assert removal_plan.plan(rows, [], True)["remove"] == []


def test_duplicate_is_held_when_its_keeper_is_not_live():
    rows = [row("Sheet1", 2, "111", "ebay:1", created=False), row("Sheet1", 9, "222", "ebay:1", stock=5)]
    out = removal_plan.plan(rows, [], True)
    assert out["remove"] == [] and "not live" in out["held"][0][1]


def test_duplicate_in_stock_is_held_when_the_surviving_original_shows_no_stock():
    rows = [row("Sheet1", 2, "111", "ebay:1", stock=0), row("Sheet1", 9, "222", "ebay:1", stock=5)]
    out = removal_plan.plan(rows, [], True)
    assert out["remove"] == [] and "disagree" in out["held"][0][1]
    # both out of stock: the copy may go
    rows2 = [row("Sheet1", 2, "111", "ebay:1", stock=0), row("Sheet1", 9, "222", "ebay:1", stock=0)]
    assert names(removal_plan.plan(rows2, [], True)["remove"]) == [("Sheet1", 9)]


def test_keeper_removed_as_listed_keeps_an_in_stock_copy_but_not_an_out_of_stock_one():
    keeper = row("Sheet1", 2, "111", "ebay:1", stock=0)
    in_stock_copy = row("Sheet1", 9, "222", "ebay:1", stock=5)
    oos_copy = row("Sheet1", 10, "333", "ebay:1", stock=0)
    out = removal_plan.plan([keeper, in_stock_copy, oos_copy], ["111"], True)
    assert names(out["remove"]) == [("Sheet1", 2), ("Sheet1", 10)]
    assert [h[0]["sku"] for h in out["held"]] == ["222"]


def test_a_row_both_listed_and_a_live_duplicate_is_removed_once():
    rows = [row("Sheet1", 2, "111", "ebay:1", stock=5), row("Sheet1", 9, "222", "ebay:1", stock=0)]
    out = removal_plan.plan(rows, ["222"], True)
    assert names(out["remove"]) == [("Sheet1", 9)]
    assert out["remove"][0]["reason"] == "listed (out of stock) and a live duplicate"


def test_duplicate_list_restricts_which_duplicates_may_go():
    rows = [row("Sheet1", 2, "111", "ebay:1", stock=5), row("Sheet1", 9, "222", "ebay:1", stock=5),
            row("Sheet1", 10, "333", "ebay:1", stock=5)]
    out = removal_plan.plan(rows, [], True, approved_dup_skus=["222"])
    assert names(out["remove"]) == [("Sheet1", 9)]
    assert "not in the approved" in out["held"][0][1]


def test_rows_without_a_supplier_id_are_never_duplicates():
    rows = [row("Sheet1", 2, "111", ""), row("Sheet1", 3, "222", "")]
    assert removal_plan.plan(rows, [], True)["remove"] == []
