"""held_skus: wrong-content listings are held at stock 0 by the nightly price/stock audit (2026-10-05)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import held_skus


def test_load_skus_strips_comments_blanks_and_keeps_both_zero_forms(tmp_path):
    f = tmp_path / "hold_at_zero_skus.txt"
    f.write_text("# header comment\n993578879973  # XGODY Mini 4K\n\n0134203578940\n   \n", encoding="utf-8")
    got = held_skus.load_skus(str(f))
    assert got == {"993578879973", "0134203578940", "134203578940"}


def test_a_missing_file_is_empty_not_an_error(tmp_path):
    assert held_skus.load_skus(str(tmp_path / "nope.txt")) == set()
    assert held_skus.held_set(str(tmp_path)) == set()


def test_held_set_merges_the_hand_kept_list_and_the_nights_zero_step(tmp_path):
    (tmp_path / "hold_at_zero_skus.txt").write_text("111\n", encoding="utf-8")
    (tmp_path / "held_at_zero.txt").write_text("222\n222\n", encoding="utf-8")
    assert held_skus.held_set(str(tmp_path)) == {"111", "222"}


def test_remember_appends_deduplicated_and_the_next_held_set_sees_it(tmp_path):
    assert held_skus.remember(["555", "555", " 666 ", "", None], str(tmp_path)) == 2
    assert held_skus.remember([], str(tmp_path)) == 0
    assert held_skus.remember(["777"], str(tmp_path)) == 1
    assert held_skus.held_set(str(tmp_path)) == {"555", "666", "777"}


def test_is_held_ignores_leading_zeros_both_ways():
    held = {"134203578940", "0134203578940"}
    assert held_skus.is_held("134203578940", held) and held_skus.is_held("0134203578940", held)
    assert not held_skus.is_held("134203578941", held)
    assert not held_skus.is_held("", held) and not held_skus.is_held(None, held)


def test_split_held_zeroes_only_held_skus_that_still_show_stock():
    sheet = {"111": (19.99, 5, "Sheet1", 2), "222": (9.5, 10, "Amazon", 7), "333": (4.0, 3, "Sheet1", 9), "444": (6.0, 2, "Sheet1", 11)}
    live = {"111": (17.50, 4, "t"), "222": (9.5, 0, "t"), "333": (4.0, 1, "t"), "555": (1.0, 9, "t")}
    to_zero, rest = held_skus.split_held(live, sheet, {"111", "222"})
    assert to_zero == [("111", 17.50, 4, "Sheet1", 2)]      # the listing's own price, never the sheet's 19.99
    assert rest == {"333": (4.0, 1, "t"), "555": (1.0, 9, "t")}  # held SKUs (even at stock 0) leave the drift check


def test_split_held_price_fallbacks_and_skips():
    sheet = {"111": (19.99, 5, "Sheet1", 2), "222": (0.0, 5, "Sheet1", 3), "333": (7.0, 5, "Sheet1", 4)}
    live = {"111": (0.0, 3, "t"), "222": (0.0, 3, "t"), "333": (7.0, 2, "t"), "999": (5.0, 5, "t")}
    to_zero, rest = held_skus.split_held(live, sheet, {"111", "222", "333", "999"})
    assert ("111", 19.99, 3, "Sheet1", 2) in to_zero           # no live price -> the sheet's
    assert all(t[0] != "222" for t in to_zero)                  # no usable price anywhere -> left alone
    assert all(t[0] != "999" for t in to_zero)                  # not on the sheet -> left alone
    assert rest == {}                                           # but none of them is price-checked either
