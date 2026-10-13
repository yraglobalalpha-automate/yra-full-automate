"""The nightly's zeroes are permanent holds (user 2026-10-12: "next time never restock the nightly zeroes").

Before: the zero pass took the stock off a wrong-content listing, the same-night audit left it alone, and the next hourly sync restocked it from the sheet - the
listing flapped between zero and sellable. Now the nightly (PERSIST_HOLDS=1) writes what it holds to protected_skus.txt (the sync freezes the row) and
hold_at_zero_skus.txt (the audit keeps the stock off); a workflow step commits both files."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import held_skus
import test_held_flows as flows

PROTECTED, HOLD = "protected_skus.txt", "hold_at_zero_skus.txt"


def lines(path):
    return [ln for ln in Path(path).read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")]


def skus_of(path):
    return held_skus.load_skus(str(path))


# ---------------------------------------------------------------- held_skus.persist_holds
def test_new_holds_go_to_both_files_with_a_dated_comment_and_the_note(tmp_path):
    added = held_skus.persist_holds([("111", "Sheet1 row 5: row = A | page = B"), ("0222", "")], str(tmp_path), today="2026-10-12")
    assert added == ["0222", "111"]
    for name in (PROTECTED, HOLD):
        text = (tmp_path / name).read_text(encoding="utf-8")
        assert "# 2026-10-12 the nightly found these listings showing ANOTHER product" in text
        assert "111  # Sheet1 row 5: row = A | page = B" in text and "\n0222\n" in text
        assert skus_of(tmp_path / name) == {"111", "0222", "222"}


def test_a_missing_file_is_created_with_its_header(tmp_path):
    held_skus.persist_holds([("111", "")], str(tmp_path), today="2026-10-12")
    assert (tmp_path / PROTECTED).read_text(encoding="utf-8").startswith("# OnBuy content mismatch")
    assert (tmp_path / HOLD).read_text(encoding="utf-8").startswith("# SKUs whose OnBuy listing shows ANOTHER product")


def test_what_is_already_held_is_not_written_twice_and_nothing_is_touched_when_nothing_is_new(tmp_path):
    (tmp_path / PROTECTED).write_text("# header\n0134203578940\n", encoding="utf-8")
    (tmp_path / HOLD).write_text("# header\n134203578940  # old\n", encoding="utf-8")
    before = {n: (tmp_path / n).read_bytes() for n in (PROTECTED, HOLD)}
    assert held_skus.persist_holds([("134203578940", "x"), ("0134203578940", "y"), ("", "z")], str(tmp_path)) == []   # either zero spelling counts
    assert {n: (tmp_path / n).read_bytes() for n in (PROTECTED, HOLD)} == before


def test_a_sku_new_to_only_one_file_is_added_there_only(tmp_path):
    (tmp_path / PROTECTED).write_text("# header\n111\n", encoding="utf-8")
    (tmp_path / HOLD).write_text("# header\n", encoding="utf-8")
    assert held_skus.persist_holds([("111", "n")], str(tmp_path), today="2026-10-12") == ["111"]
    assert skus_of(tmp_path / PROTECTED) == {"111"} and skus_of(tmp_path / HOLD) == {"111"}
    assert (tmp_path / PROTECTED).read_text(encoding="utf-8").count("111") == 1            # not duplicated


def test_line_endings_of_an_existing_file_are_kept(tmp_path):
    (tmp_path / PROTECTED).write_bytes(b"# header\r\n111\r\n")
    held_skus.persist_holds([("222", "n")], str(tmp_path), today="2026-10-12")
    raw = (tmp_path / PROTECTED).read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_the_same_sku_twice_in_one_call_is_written_once_and_notes_stay_on_one_line(tmp_path):
    held_skus.persist_holds([("333", "multi\nline  note"), ("333", "again")], str(tmp_path), today="2026-10-12")
    text = (tmp_path / HOLD).read_text(encoding="utf-8")
    assert text.count("333") == 1 and "333  # multi line note" in text


def test_held_set_sees_what_was_persisted_so_the_audit_keeps_the_stock_off(tmp_path):
    held_skus.persist_holds([("444", "")], str(tmp_path))
    assert held_skus.is_held("444", held_skus.held_set(str(tmp_path)))


# ---------------------------------------------------------------- the zero pass
def run_zero(monkeypatch, persist, dry_run=False, same_product=()):
    calls = []
    monkeypatch.setattr(flows.zero.held_skus, "persist_holds", lambda items, *a, **k: calls.append(list(items)) or [s for s, _ in items])
    monkeypatch.setattr(flows.zero, "PERSIST_HOLDS", persist)
    monkeypatch.setattr(flows.zero, "SAME_PRODUCT", set(same_product))
    flows._run_zero(monkeypatch, zero_skus=[], only_listed=False, dry_run=dry_run)
    return calls


def test_the_nightly_persists_every_listing_it_holds_including_the_ones_already_at_zero(monkeypatch):
    calls = run_zero(monkeypatch, persist=True)
    assert len(calls) == 1
    got = dict(calls[0])
    assert sorted(got) == ["200", "400"]                              # 200 zeroed now, 400 was already at 0 - both stay frozen
    assert "row 3" in got["200"] and "Cordless Leaf Blower" in got["200"] and "Stainless Steel Electric Kettle" in got["200"]


def test_a_verified_same_product_listing_is_never_persisted(monkeypatch):
    calls = run_zero(monkeypatch, persist=True, same_product={"200"})
    assert sorted(dict(calls[0])) == ["400"]


def test_nothing_is_written_in_a_dry_run_or_when_the_switch_is_off(monkeypatch):
    assert run_zero(monkeypatch, persist=True, dry_run=True) == []
    assert run_zero(monkeypatch, persist=False) == []


def test_the_switch_is_off_by_default_so_tests_and_one_off_runs_never_write_files():
    assert flows.zero.PERSIST_HOLDS is False



# ---------------------------------------------------------------- held_skus.forget (a deleted listing needs no hold)
def test_forget_drops_the_skus_lines_from_both_files_and_keeps_the_rest(tmp_path):
    (tmp_path / PROTECTED).write_bytes(b"# header\r\n111  # a\r\n0222  # b\r\n333\r\n")
    (tmp_path / HOLD).write_text("# header\n222\n444  # keep\n", encoding="utf-8")
    assert held_skus.forget(["222", "333", "999"], str(tmp_path)) == 3
    assert (tmp_path / PROTECTED).read_bytes() == b"# header\r\n111  # a\r\n"                  # both zero spellings go, CRLF kept, comments stay
    assert (tmp_path / HOLD).read_text(encoding="utf-8") == "# header\n444  # keep\n"


def test_forget_touches_nothing_when_nothing_matches_and_tolerates_a_missing_file(tmp_path):
    (tmp_path / PROTECTED).write_text("# header\n111\n", encoding="utf-8")
    before = (tmp_path / PROTECTED).read_bytes()
    assert held_skus.forget(["555", ""], str(tmp_path)) == 0
    assert (tmp_path / PROTECTED).read_bytes() == before and not (tmp_path / HOLD).exists()
