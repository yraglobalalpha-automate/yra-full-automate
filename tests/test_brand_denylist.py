"""Hisense and Homcom are never listed on YRA (user order 2026-11-04): the matcher, the list loading and where the sync applies it.
generate_xml.py runs a whole sync from main(), so the loop's decision lives in brand_denylist.guard() (tested here) and the placement of
its call is checked against the source."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import brand_denylist as bd

LISTED = bd.compile_names(bd.parse_names("Hisense\nHomcom"))


# ---------------------------------------------------------------- the list
def test_parse_names_notes_commas_and_spellings():
    parsed = bd.parse_names("# a note\nHisense  # trailing note\nHomcom | Home Com ; Aosom Foo\n\n,\n")
    assert parsed == [("Hisense", ["Hisense"]), ("Homcom", ["Homcom", "Home Com"]), ("Aosom Foo", ["Aosom Foo"])]


def test_variable_replaces_the_file_and_the_file_replaces_the_default(tmp_path):
    f = tmp_path / "list.txt"
    f.write_text("Acme\n", encoding="utf-8")
    assert [n for n, _ in bd.load(path=str(f), env_value="")] == ["Acme"]
    assert [n for n, _ in bd.load(path=str(f), env_value="Hisense, Homcom")] == ["Hisense", "Homcom"]


def test_a_missing_file_falls_back_to_the_default_but_an_empty_file_blocks_nothing(tmp_path):
    assert [n for n, _ in bd.load(path=str(tmp_path / "gone.txt"), env_value="")] == ["Hisense", "Homcom"]
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing blocked\n", encoding="utf-8")
    assert bd.load(path=str(empty), env_value="") == []
    assert bd.blocked_name("Hisense", "", listed=[]) == ""


def test_the_repo_file_blocks_hisense_and_homcom():
    listed = bd.load(env_value="")
    assert {"Hisense", "Homcom"} <= {n for n, _ in listed}


def test_the_process_list_is_loaded_once(monkeypatch):
    bd.reset_cache()
    monkeypatch.setenv("BLOCKED_BRANDS", "Acme")
    assert [n for n, _ in bd.names()] == ["Acme"]
    monkeypatch.setenv("BLOCKED_BRANDS", "Other")
    assert [n for n, _ in bd.names()] == ["Acme"]        # cached
    bd.reset_cache()
    assert [n for n, _ in bd.names()] == ["Other"]
    bd.reset_cache()


# ---------------------------------------------------------------- who is blocked
@pytest.mark.parametrize("brand,title,want", [
    ("Hisense", "Hisense 55E6STUK 55 Inch LED 4K Ultra HD Smart TV", "Hisense"),
    ("HISENSE", "anything", "Hisense"),
    ("hisense uk", "anything", "Hisense"),
    ("Hisense Gorenje", "anything", "Hisense"),
    ("HOMCOM", "L Shaped Desk with LED Lights", "Homcom"),
    ("Homcom®", "x", "Homcom"),
    ("", "HOMCOM 3 Seater Sofa Grey", "Homcom"),
    ("", "NEW HOMCOM Wooden Shelf", "Homcom"),
    ("Unbranded", "Hisense 65 inch QLED TV", "Hisense"),
    ("Generic", "HOMCOM® Garden Table", "Homcom"),
    ("none", "Hisense Party Rocket Speaker", "Hisense"),
])
def test_rows_of_a_blocked_brand_are_caught(brand, title, want):
    assert bd.blocked_name(brand, title, LISTED) == want


@pytest.mark.parametrize("brand,title", [
    ("Samsung", "Samsung 55 inch TV"),
    ("Vonhaus", "Remote control for Hisense TV"),            # accessory under another brand
    ("Vonhaus", "Hisense compatible wall bracket"),          # another brand named in the Brand cell wins over the title
    ("", "Wall bracket for HOMCOM cabinet"),                 # brand not in the first three words
    ("", "Replacement Hisense fridge shelf"),
    ("Generic", "Fits Hisense dishwashers filter"),
    ("", "Homcomfort cushion"),                              # whole words only
    ("Hisensei", "x"),
    ("", ""),
    (None, None),
])
def test_other_rows_are_left_alone(brand, title):
    assert bd.blocked_name(brand, title, LISTED) == ""


def test_a_title_that_only_mentions_the_brand_is_reported_but_not_blocked():
    assert bd.mentions("Remote control for Hisense TV", LISTED) == "Hisense"
    assert bd.mentions("Samsung TV", LISTED) == ""


def test_other_spellings_of_one_brand_count():
    listed = bd.compile_names(bd.parse_names("Homcom | Home Com"))
    assert bd.blocked_name("Home Com", "", listed) == "Homcom"
    assert bd.blocked_name("", "Home Com Wooden Chair", listed) == "Homcom"


# ---------------------------------------------------------------- what the loop does with it
def test_guard_flags_blocked_rows_and_forces_stock_zero_over_any_other_flag():
    flag, stock, name = bd.guard("Hisense", "Hisense TV", "", 5, LISTED)
    assert (flag, stock, name) == (bd.status_text("Hisense"), 0, "Hisense")
    assert bd.is_blocked_status(flag) and "Hisense" in flag and len(flag) < 140
    flag, stock, name = bd.guard("HOMCOM", "x", "Failed: supplier link already used on row 9", 3, LISTED)
    assert flag.startswith("Blocked brand: Homcom") and stock == 0


def test_guard_passes_every_other_row_through_unchanged():
    assert bd.guard("Samsung", "Samsung TV", "", 5, LISTED) == ("", 5, "")
    assert bd.guard("Samsung", "x", "Failed: dup", 2, LISTED) == ("Failed: dup", 2, "")
    assert not bd.is_blocked_status("Failed: something")
    assert not bd.is_blocked_status(None)


# ---------------------------------------------------------------- where the sync applies it
SRC = (ROOT / "generate_xml.py").read_text(encoding="utf-8")


def test_the_sync_loop_applies_the_block_before_any_push_decision():
    guard_at = SRC.index("brand_denylist.guard(")
    assert SRC.index("supplier link already used on row") < guard_at < SRC.index("sync_status = amazon_flag or None")
    assert "import brand_denylist" in SRC


def test_the_catalogue_mirror_leaves_blocked_brands_out():
    mirror = SRC[SRC.index("GENERATE XML (kept as fallback)"):]
    assert "brand_denylist.blocked_name(" in mirror.split("ET.SubElement(product")[0]


def test_the_germany_populate_skips_blocked_brands():
    src = (ROOT / "germany_sync.py").read_text(encoding="utf-8")
    assert "brand_denylist.blocked_name(" in src.split("def mode_prune")[0]
    assert re.search(r'"blocked brand"', src)
