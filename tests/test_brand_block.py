"""Brand blocks must expire. Rows have sat skipped for weeks on
brands the platform was accepting on neighbouring rows, because the
refusal was re-read from the mirror every run and the row was never
pushed again to test it. generate_xml.py runs on import, so the decision
function is extracted from source rather than imported."""
import ast
import re
from datetime import date, datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "generate_xml.py"


def _brand_block_state():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "brand_block_state")
    consts = [n for n in tree.body
              if isinstance(n, ast.Assign)
              and getattr(n.targets[0], "id", "") in
              ("BRAND_BLOCK_RE", "BRAND_BLOCK_PHRASE", "BRAND_BLOCK_RETRY_DAYS")]
    ns = {"re": re, "datetime": datetime, "os": __import__("os")}
    exec(compile(ast.Module(body=consts + [fn], type_ignores=[]), str(SRC), "exec"), ns)
    return ns["brand_block_state"]


brand_block_state = _brand_block_state()
TODAY = date(2026, 9, 3)


def flag(stamp, brand):
    stamped = f" ({stamp})" if stamp else ""
    return (f"BRAND BLOCKED{stamped} - OnBuy says the brand '{brand}' is owned by another "
            "seller, so this product cannot be listed under it.")


RAW = "Failed: An error occurred: The supplied brand is owned by another seller."


def test_first_sighting_of_onbuys_wording_blocks_and_dates_it():
    skip, stamp = brand_block_state((RAW, RAW), "Metatell", TODAY)
    assert (skip, stamp) == (True, TODAY)


def test_a_fresh_block_on_the_same_brand_keeps_skipping():
    skip, _ = brand_block_state((flag("2026-09-01", "Metatell"), RAW), "Metatell", TODAY)
    assert skip is True


def test_the_clock_is_not_reset_by_re_flagging():
    # Re-stamping today's date every run is what made these permanent.
    _, stamp = brand_block_state((flag("2026-09-01", "Metatell"), RAW), "Metatell", TODAY)
    assert stamp == date(2026, 9, 1)


def test_a_corrected_brand_lifts_the_block_immediately():
    skip, _ = brand_block_state((flag("2026-09-02", "Metatell"), RAW), "Unbranded", TODAY)
    assert skip is False


def test_brand_comparison_ignores_case_and_padding():
    skip, _ = brand_block_state((flag("2026-09-02", "Metatell"), RAW), "  metatell ", TODAY)
    assert skip is True


def test_a_stale_verdict_is_re_tested():
    skip, _ = brand_block_state((flag("2026-08-20", "Metatell"), RAW), "Metatell", TODAY)
    assert skip is False


def test_expiry_boundary_is_exactly_the_retry_window():
    assert brand_block_state((flag("2026-08-28", "Metatell"), RAW), "Metatell", TODAY)[0] is True
    assert brand_block_state((flag("2026-08-27", "Metatell"), RAW), "Metatell", TODAY)[0] is False


def test_the_sheets_dated_flag_beats_the_mirrors_undated_copy():
    """The mirror is never rewritten for a blocked row, so its copy of
    OnBuy's raw wording lives forever - it must not win."""
    skip, _ = brand_block_state((flag("2026-08-01", "Metatell"), RAW), "Metatell", TODAY)
    assert skip is False


def test_a_legacy_undated_flag_is_dated_rather_than_obeyed_forever():
    skip, stamp = brand_block_state((flag(None, "Metatell"), RAW), "Metatell", TODAY)
    assert (skip, stamp) == (True, TODAY)


def test_a_row_with_no_refusal_anywhere_is_never_skipped():
    assert brand_block_state(("Synced", "Synced"), "Metatell", TODAY)[0] is False
    assert brand_block_state(("", ""), "", TODAY)[0] is False
