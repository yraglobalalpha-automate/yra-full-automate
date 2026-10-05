"""link_owners.build: the deterministic owner of a supplier product (2026-10-05)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import link_owners


def test_first_row_in_sheet_order_owns_when_nothing_is_live():
    counts, owner = link_owners.build([("Sheet1", 5, "ebay:1", False), ("Sheet1", 9, "ebay:1", False)])
    assert counts == {"ebay:1": 2} and owner == {"ebay:1": ("Sheet1", 5)}


def test_a_live_later_row_takes_the_product_from_a_never_listed_first_row():
    # the Arden bug: row 1968 (never listed) used to freeze the live original at row 95
    counts, owner = link_owners.build([("Amazon", 95, "amazon:B0X", True), ("Amazon", 1968, "amazon:B0X", False)])
    assert owner["amazon:B0X"] == ("Amazon", 95)
    counts, owner = link_owners.build([("Amazon", 40, "amazon:B0X", False), ("Amazon", 95, "amazon:B0X", True)])
    assert owner["amazon:B0X"] == ("Amazon", 95)


def test_among_several_live_rows_the_first_in_sheet_order_owns():
    _, owner = link_owners.build([("Sheet1", 3, "ebay:7", False), ("Sheet1", 8, "ebay:7", True), ("Sheet1", 12, "ebay:7", True)])
    assert owner["ebay:7"] == ("Sheet1", 8)


def test_tab_order_is_the_order_the_entries_come_in():
    _, owner = link_owners.build([("Sheet1", 700, "amazon:B0Y", True), ("Amazon", 2, "amazon:B0Y", True)])
    assert owner["amazon:B0Y"] == ("Sheet1", 700)


def test_a_product_on_one_row_is_counted_once_and_rows_without_identity_are_ignored():
    counts, owner = link_owners.build([("Sheet1", 2, "ebay:1", True), ("Sheet1", 3, "", True), ("Sheet1", 4, None, False)])
    assert counts == {"ebay:1": 1} and owner == {"ebay:1": ("Sheet1", 2)}


def test_the_owner_never_changes_with_the_order_rows_are_processed():
    entries = [("Amazon", 10, "amazon:B0Z", False), ("Amazon", 20, "amazon:B0Z", True), ("Amazon", 30, "amazon:B0Z", False)]
    first = link_owners.build(entries)
    assert link_owners.build(list(entries)) == first
    assert first[1]["amazon:B0Z"] == ("Amazon", 20)
