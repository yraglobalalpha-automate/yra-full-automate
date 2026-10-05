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


def test_duplicate_is_held_unless_its_original_is_synced_when_required():
    keeper = dict(row("Sheet1", 2, "111", "ebay:1", stock=5), sync="Awaiting OnBuy go-live")
    copy = row("Sheet1", 9, "222", "ebay:1", stock=5)
    assert names(removal_plan.plan([keeper, copy], [], True)["remove"]) == [("Sheet1", 9)]
    out = removal_plan.plan([keeper, copy], [], True, require_keeper_synced=True)
    assert out["remove"] == [] and "not Synced" in out["held"][0][1]
    keeper["sync"] = "Synced"
    assert names(removal_plan.plan([keeper, copy], [], True, require_keeper_synced=True)["remove"]) == [("Sheet1", 9)]


def test_excluded_skus_are_never_removed_whichever_rule_would_take_them():
    rows = [row("Sheet1", 2, "111", "ebay:1", stock=5), row("Sheet1", 9, "222", "ebay:1", stock=5)]
    out = removal_plan.plan(rows, ["111"], True, exclude_skus={"222", "111"})
    assert out["remove"] == [] and {h[0]["sku"] for h in out["held"]} == {"111", "222"}
    assert all("excluded" in h[1] for h in out["held"])
    # not excluded: the copy goes
    assert names(removal_plan.plan(rows, [], True, exclude_skus={"999"})["remove"]) == [("Sheet1", 9)]


def test_dead_copies_are_off_by_default_and_never_touch_a_live_row():
    rows = [row("Amazon", 95, "111", "amazon:B0X", stock=5), row("Amazon", 1968, "222", "amazon:B0X", created=False)]
    assert removal_plan.plan(rows, [], True)["remove"] == []
    out = removal_plan.plan(rows, [], False, include_dead_copies=True)
    assert names(out["remove"]) == [("Amazon", 1968)]
    assert out["remove"][0]["reason"].startswith("dead copy of Amazon row 95")


def test_a_dead_copy_needs_a_live_owner():
    rows = [row("Amazon", 95, "111", "amazon:B0X", created=False), row("Amazon", 1968, "222", "amazon:B0X", created=False)]
    assert removal_plan.plan(rows, [], False, include_dead_copies=True)["remove"] == []


def test_the_owner_is_the_first_live_row_so_an_earlier_never_listed_row_is_the_dead_copy():
    rows = [row("Amazon", 40, "111", "amazon:B0X", created=False), row("Amazon", 95, "222", "amazon:B0X", stock=5)]
    out = removal_plan.plan(rows, [], False, include_dead_copies=True)
    assert names(out["remove"]) == [("Amazon", 40)]
    assert removal_plan.owner([dict(r) for r in rows])["row"] == 95


def test_a_live_non_owner_is_not_a_dead_copy():
    rows = [row("Sheet1", 2, "111", "ebay:1", stock=5), row("Sheet1", 9, "222", "ebay:1", stock=5),
            row("Sheet1", 12, "333", "ebay:1", created=False)]
    out = removal_plan.plan(rows, [], False, include_dead_copies=True)
    assert names(out["remove"]) == [("Sheet1", 12)]


def test_dead_copy_guards_exclusions_approved_list_repeated_sku_and_synced_owner():
    keeper = dict(row("Sheet1", 2, "111", "ebay:1", stock=5), sync="Awaiting OnBuy go-live")
    dead = row("Sheet1", 9, "222", "ebay:1", created=False)
    assert names(removal_plan.plan([keeper, dead], [], False, include_dead_copies=True)["remove"]) == [("Sheet1", 9)]
    out = removal_plan.plan([keeper, dead], [], False, require_keeper_synced=True, include_dead_copies=True)
    assert out["remove"] == [] and "not Synced" in out["held"][0][1]
    keeper["sync"] = "Synced"
    assert removal_plan.plan([keeper, dead], [], False, exclude_skus={"222"}, include_dead_copies=True)["remove"] == []
    assert removal_plan.plan([keeper, dead], [], False, approved_dup_skus=["999"], include_dead_copies=True)["remove"] == []
    twin = row("Amazon", 3, "222", "amazon:B0Q", created=False)
    assert removal_plan.plan([keeper, dead, twin], [], False, include_dead_copies=True)["remove"] == []
