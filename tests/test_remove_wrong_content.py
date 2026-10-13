"""remove_wrong_content: a listing whose OnBuy page is ANOTHER product goes (sheet row backed up first, then the listing) - and only
while it still is another product, is on exactly one row, and its OnBuy read worked."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import remove_wrong_content as rwc

ROOT = Path(__file__).resolve().parents[1]

ROXEL = "Roxel Portable Bluetooth Speaker Waterproof Wireless Party Speaker Black"
SHARK = "Shark WandVac Cordless Handheld Vacuum Cleaner WV200UK Lightweight Powerful"
KETTLE = "Russell Hobbs Stainless Steel Cordless Electric Kettle 1.7 Litre Silver"


def row(sku, title, tab="Amazon", n=2, stock=3):
    return {"tab": tab, "row": n, "sku": sku, "title": title, "stock": stock, "cells": [sku, title], "ident": "", "created": True,
            "sync": "", "price": "10"}


def lst(name, stock=4, opc="PX1"):
    return {"name": name, "stock": stock, "product_encoded_id": opc}


def test_removes_a_row_whose_onbuy_page_is_another_product():
    out = rwc.plan_wrong_content(["A-1"], [row("A-1", ROXEL)], {"A-1": lst(SHARK)})
    assert [r["sku"] for r in out["remove"]] == ["A-1"]
    assert out["held"] == []
    got = out["remove"][0]
    assert got["onbuy_name"] == SHARK and got["onbuy_opc"] == "PX1" and got["overlap"] < 0.5
    assert "wrong content" in got["reason"] and got["cells"] == ["A-1", ROXEL]    # the whole row travels to the backup


def test_holds_back_a_page_that_matches_the_row_again():
    out = rwc.plan_wrong_content(["A-1"], [row("A-1", KETTLE)], {"A-1": lst(KETTLE + " Cordless")})
    assert out["remove"] == []
    assert out["held"][0][0] == "A-1" and "matches the row" in out["held"][0][1]


@pytest.mark.parametrize("rows,listings,errors,why", [
    ([], {"A-1": lst(SHARK)}, None, "not on the sheet"),
    ([row("A-1", ROXEL, n=2), row("A-1", ROXEL, n=9)], {"A-1": lst(SHARK)}, None, "2 sheet rows"),
    ([row("A-1", ROXEL)], {"A-1": None}, None, "no live OnBuy listing"),
    ([row("A-1", ROXEL)], {}, {"A-1": "timeout"}, "read failed"),
    ([row("A-1", "")], {"A-1": lst(SHARK)}, None, "empty"),
    ([row("A-1", ROXEL)], {"A-1": lst("")}, None, "empty"),
])
def test_holds_back_what_it_cannot_prove(rows, listings, errors, why):
    out = rwc.plan_wrong_content(["A-1"], rows, listings, errors=errors)
    assert out["remove"] == []
    assert why in out["held"][0][1]


def test_only_the_asked_skus_are_ever_considered():
    rows = [row("A-1", ROXEL), row("B-2", KETTLE, n=3)]
    out = rwc.plan_wrong_content(["A-1"], rows, {"A-1": lst(SHARK), "B-2": lst(SHARK)})
    assert [r["sku"] for r in out["remove"]] == ["A-1"]


def test_read_listings_turns_a_failed_read_into_a_held_sku(monkeypatch):
    monkeypatch.setattr(rwc.time, "sleep", lambda s: None)

    class Client:
        def get_listing(self, sku):
            if sku == "BAD":
                raise RuntimeError("503 maintenance")
            return lst(SHARK) if sku == "A-1" else None

    listings, errors = rwc.read_listings(Client(), ["A-1", "BAD", "NONE"])
    assert listings == {"A-1": lst(SHARK), "NONE": None}
    assert "503" in errors["BAD"]


# ---- whole run, with the sheet and OnBuy faked ----------------------------------------------------------------------------------------

class FakeWs:
    def __init__(self, title, skus):
        self.title = title
        self.col = ["SKU"] + list(skus)

    def col_values(self, n):
        return list(self.col)


class FakeOnBuy:
    def __init__(self, listings):
        self.listings = listings

    def authenticate(self):
        return True

    def get_listing(self, sku):
        return self.listings.get(sku)


def _setup(monkeypatch, tmp_path, rows, listings, want, dry_run, max_remove=40, stuck=()):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rwc.time, "sleep", lambda s: None)
    monkeypatch.setattr(rwc, "WANT", want)
    monkeypatch.setattr(rwc, "DRY_RUN", dry_run)
    monkeypatch.setattr(rwc, "MAX_REMOVE", max_remove)
    monkeypatch.setattr(rwc, "OnBuyClient", lambda: FakeOnBuy(listings))
    tabs = {"Sheet1": FakeWs("Sheet1", [r["sku"] for r in rows if r["tab"] == "Sheet1"]),
            "Amazon": FakeWs("Amazon", [r["sku"] for r in rows if r["tab"] == "Amazon"])}
    headers = {"Sheet1": ["SKU", "Title"], "Amazon": ["SKU", "Title"]}
    calls = []
    monkeypatch.setattr(rwc, "open_book", lambda: "BOOK")
    monkeypatch.setattr(rwc, "load_tabs", lambda book: (list(tabs.values()), headers, rows))

    def write_backup(book, hbt, removed):
        calls.append(("backup", sorted(r["sku"] for r in removed)))

    def delete_rows(book, tab, skus):
        calls.append(("delete_rows", tab, sorted(skus)))
        gone = [s for s in skus if s not in stuck]
        tabs[tab].col = [v for v in tabs[tab].col if v not in gone]
        return len(gone), [(s, 0) for s in skus if s in stuck]

    monkeypatch.setattr(rwc.grr, "write_backup", write_backup)
    monkeypatch.setattr(rwc.grr, "delete_rows", delete_rows)
    return calls


ROWS = [row("A-1", ROXEL, "Amazon", 5), row("B-2", KETTLE, "Sheet1", 7), row("C-3", ROXEL, "Sheet1", 9)]
LISTINGS = {"A-1": lst(SHARK), "B-2": lst(KETTLE), "C-3": lst(SHARK, opc="PX3")}


def test_dry_run_plans_and_writes_the_files_but_touches_nothing(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, ROWS, LISTINGS, ["A-1", "B-2", "C-3"], dry_run=True)
    rwc.main()
    assert calls == []
    assert (tmp_path / "onbuy_delete_list.txt").read_text().split() == ["A-1", "C-3"]
    plan = (tmp_path / "removal_plan.csv").read_text()
    assert "REMOVE,Amazon,5,A-1" in plan and "HELD,,,B-2" in plan


def test_live_run_backs_up_first_then_deletes_rows_then_lists_the_listings(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, ROWS, LISTINGS, ["A-1", "B-2", "C-3"], dry_run=False)
    rwc.main()
    assert calls[0] == ("backup", ["A-1", "C-3"])
    assert ("delete_rows", "Amazon", ["A-1"]) in calls and ("delete_rows", "Sheet1", ["C-3"]) in calls
    assert (tmp_path / "onbuy_delete_list.txt").read_text().split() == ["A-1", "C-3"]


def test_a_row_that_is_still_on_the_sheet_keeps_its_listing_out_of_the_delete_list(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, ROWS, LISTINGS, ["A-1", "C-3"], dry_run=False, stuck=("C-3",))
    rwc.main()
    assert (tmp_path / "onbuy_delete_list.txt").read_text().split() == ["A-1"]


def test_too_many_rows_refuses_before_anything_changes(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, ROWS, LISTINGS, ["A-1", "C-3"], dry_run=False, max_remove=1)
    with pytest.raises(SystemExit):
        rwc.main()
    assert calls == []


def test_nothing_approved_nothing_done(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, ROWS, LISTINGS, ["B-2"], dry_run=False)
    rwc.main()
    assert calls == []
    assert (tmp_path / "onbuy_delete_list.txt").read_text().split() == []


# ---- workflow wiring --------------------------------------------------------------------------------------------------------------------

def test_workflow_is_a_dry_run_by_default_and_serialised_with_the_other_onbuy_jobs():
    text = (ROOT / ".github" / "workflows" / "remove_wrong_content.yml").read_text(encoding="utf-8")
    assert "group: onbuy-api" in text and "cancel-in-progress: false" in text
    dry = text.split("dry_run:")[1].split("permissions:")[0]
    assert 'default: "yes"' in dry
    assert "python remove_wrong_content.py" in text
    assert "KEEP_AT: ${{ inputs.keep_at }}" in text
    assert "DELETE_SKUS_FILE: onbuy_delete_list.txt" in text and "python delete_listings_batch.py" in text
    # the OnBuy delete runs only for a real run, and only after the sheet step
    assert text.index("python remove_wrong_content.py") < text.index("python delete_listings_batch.py")
    assert "inputs.dry_run == 'no'" in text


# ---- unattended mode (user 2026-10-13: "Delist every wrong listing from now and in future") -------------------------------------------------

SHARK_FILTER = "Shark Replacement Filter Pack"          # shares one word with the Shark page: borderline, not clearly another product


def test_a_borderline_page_stays_frozen_in_the_nightly_but_goes_in_a_manual_run():
    overlap = rwc.adoption_guard.title_similarity(SHARK, SHARK_FILTER)
    assert 0.25 <= overlap < 0.5
    auto = rwc.plan_wrong_content(["A-1"], [row("A-1", SHARK_FILTER)], {"A-1": lst(SHARK)}, keep_at=0.25)
    assert auto["remove"] == [] and "borderline" in auto["held"][0][1] and "frozen" in auto["held"][0][1]
    manual = rwc.plan_wrong_content(["A-1"], [row("A-1", SHARK_FILTER)], {"A-1": lst(SHARK)}, keep_at=0.5)
    assert [r["sku"] for r in manual["remove"]] == ["A-1"]


def test_a_clearly_different_product_goes_in_the_nightly():
    out = rwc.plan_wrong_content(["A-1"], [row("A-1", ROXEL)], {"A-1": lst(SHARK)}, keep_at=0.25)
    assert [r["sku"] for r in out["remove"]] == ["A-1"] and "(shift)" not in out["remove"][0]["reason"]


def test_a_page_that_belongs_to_the_neighbouring_row_goes_even_when_it_shares_words_with_its_own_row():
    rows = [row("A-1", SHARK_FILTER, "Sheet1", 2), row("B-2", SHARK, "Sheet1", 3)]
    out = rwc.plan_wrong_content(["A-1"], rows, {"A-1": lst(SHARK)}, keep_at=0.25)
    assert [r["sku"] for r in out["remove"]] == ["A-1"]
    assert "(shift)" in out["remove"][0]["reason"] and "row 3" in out["remove"][0]["reason"]


def test_a_neighbour_too_far_away_or_on_another_tab_is_no_shift():
    far = [row("A-1", SHARK_FILTER, "Sheet1", 2), row("B-2", SHARK, "Sheet1", 9)]
    assert rwc.plan_wrong_content(["A-1"], far, {"A-1": lst(SHARK)}, keep_at=0.25)["remove"] == []
    other_tab = [row("A-1", SHARK_FILTER, "Sheet1", 2), row("B-2", SHARK, "Amazon", 3)]
    assert rwc.plan_wrong_content(["A-1"], other_tab, {"A-1": lst(SHARK)}, keep_at=0.25)["remove"] == []


def test_a_page_that_matches_its_own_row_is_never_a_shift():
    rows = [row("A-1", SHARK, "Sheet1", 2), row("B-2", SHARK, "Sheet1", 3)]
    out = rwc.plan_wrong_content(["A-1"], rows, {"A-1": lst(SHARK)}, keep_at=0.25)
    assert out["remove"] == [] and "matches the row" in out["held"][0][1]


def test_a_same_product_listing_is_never_removed_whatever_the_overlap():
    out = rwc.plan_wrong_content(["A-1"], [row("A-1", ROXEL)], {"A-1": lst(SHARK)}, keep_at=0.25, same_product={"A-1"})
    assert out["remove"] == [] and "same_product_skus.txt" in out["held"][0][1]
    out = rwc.plan_wrong_content(["0123"], [row("0123", ROXEL)], {"0123": lst(SHARK)}, keep_at=0.25, same_product={"123"})
    assert out["remove"] == []                                                        # either zero spelling counts


def test_wanted_skus_come_from_the_env_or_else_from_the_hand_off_file(tmp_path):
    f = tmp_path / "held_at_zero.txt"
    f.write_text("111\n# a comment\n222  # note\n111\n", encoding="utf-8")
    assert rwc.wanted_skus("A, B,A", str(f)) == ["A", "B"]                             # the env wins, de-duplicated
    assert rwc.wanted_skus("", str(f)) == ["111", "222"]
    assert rwc.wanted_skus("", str(tmp_path / "missing.txt")) == []


def test_an_empty_hand_off_file_is_a_quiet_run(monkeypatch, tmp_path, capsys):
    calls = _setup(monkeypatch, tmp_path, ROWS, LISTINGS, [], dry_run=False)
    monkeypatch.setattr(rwc, "SKUS_FILE", "held_at_zero.txt")
    rwc.main()
    assert calls == [] and (tmp_path / "onbuy_delete_list.txt").read_text() == ""
    assert "nothing to remove" in capsys.readouterr().out


def test_without_a_hand_off_file_an_empty_sku_list_is_still_an_error(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, ROWS, LISTINGS, [], dry_run=False)
    monkeypatch.setattr(rwc, "SKUS_FILE", "")
    with pytest.raises(SystemExit):
        rwc.main()


def test_clean_holds_drops_only_the_skus_whose_rows_really_went(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, ROWS, LISTINGS, ["A-1", "C-3"], dry_run=False, stuck=("C-3",))
    forgotten = []
    monkeypatch.setattr(rwc.held_skus, "forget", lambda skus: forgotten.append(sorted(skus)) or len(skus))
    monkeypatch.setattr(rwc, "CLEAN_HOLDS", True)
    rwc.main()
    assert forgotten == [["A-1"]]                                                     # C-3 is still on the sheet: it keeps its hold


def test_the_nightly_deletes_between_the_zero_passes_and_the_audit():
    for name in ("nightly_reconcile.yml", "backfill_onbuy_status.yml"):
        path = ROOT / ".github" / "workflows" / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if "remove_wrong_content.py" not in text:
            continue                                                                    # a store whose nightly lives in the other file
        step = text.index("python remove_wrong_content.py")
        assert text.rindex("python zero_stock_mismatched.py") < step < text.index("python audit_price_drift.py")
        block = text[text.rindex("- name:", 0, step):step]
        assert 'KEEP_AT: "0.25"' in block and 'CLEAN_HOLDS: "1"' in block and "SKUS_FILE: held_at_zero.txt" in block and 'DRY_RUN: "0"' in block
        assert "continue-on-error: true" in block
        assert "python delete_listings_batch.py" in text[step:text.index("python audit_price_drift.py")]
        return
    raise AssertionError("no nightly workflow carries the delete step")
