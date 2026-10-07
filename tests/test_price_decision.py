"""decide_price (the pricing decision of the sync loop), the Fee % reading and
the eBay delivery cost helpers. Extracted from generate_xml.py's source - the
module itself needs gspread and runs on import.

2026-10-02: the Fee % cell shows the EFFECTIVE fee while the Supabase mirror
holds whole numbers, so every cell was read as a manual fee override and priced
as a flat fee plus the uplift a second time (~1.8% too high). These tests pin
the fix, that such prices follow the corrected formula DOWN, that real
overrides still win, and that every other path prices exactly as before (fuzz
against the old block).

2026-10-07: OnBuy's real deduction is the listed commission PLUS 20% VAT on it
(15% -> 18.00, not the 16.50 of the 1.5-point uplift). The misread era's own
numbers are reproduced with the legacy flag, on a 7% rule: there the old
misread (8.5 + 1.5 = 10%) still overshoots the true 8.4%, while on a 15% rule
it happens to equal the true 18%. And an Amazon price a person raised above
the formula is kept (user: "the manual increase is overwritten again and
again"), while the automation's own prices still follow the cost both ways.
"""
import ast
import random
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pricing  # noqa: E402

SRC = ROOT / "generate_xml.py"
NAMES = ("_to_float", "_formula_priced", "_pct_value", "resolve_pct_cell", "_fee_cell_auto_values",
         "_misread_fee_priced", "decide_price", "_shipping_value", "ebay_shipping_cost", "_quantile",
         "shipping_cell_update", "_cell_number", "_cells_differ", "drop_edited_cells",
         "_pct_text", "_shown_profit")


def _load():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    ns = {"pricing": pricing, "re": re}
    for name in NAMES:
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
    return ns


NS = _load()
decide_price = NS["decide_price"]
RULE15 = pricing.FeeRule("Test 15%", 15)
RULE7 = pricing.FeeRule("Test 7%", 7)
RULE20 = pricing.FeeRule("Test 20%", 20)
TIERED = pricing.FeeRule("Test tiered", 15, upper_pct=8, threshold=100, min_fee=0.25)


def decide(cost, rule, existing, fee_cell, profit_cell="", ship=0.0, supplier="eBay", prev=None, **extra):
    return decide_price(supplier=supplier, cost_price=cost, shipping_cost=ship, fee_rule=rule,
                        existing_price=existing, fee_cell=fee_cell, profit_cell=profit_cell, prev=prev or {}, **extra)


def shown(price, rule):
    """The Fee % cell text the sync writes for a row priced at `price`."""
    return f"{pricing.effective_fee_percent(price, rule):.2f}"


def shown_legacy(price, rule):
    """The Fee % cell text the 2026-09-17 .. 10-07 model wrote (nominal + 1.5 points)."""
    return f"{pricing.effective_fee_percent(price, rule, legacy=True):.2f}"


def misread_price(cost, band, cell):
    """What the sync priced at while it misread the Fee % cell (2026-09-17 .. 10-02): the shown fee taken for a
    nominal rate, with the 1.5 points added to it again."""
    return pricing.price_for_profit(cost, band, platform_fee_percent=float(cell), legacy=True)


def mirror_of(text):
    """What the Supabase mirror stores for it: a whole number."""
    return str(int(round(float(text))))


IS_GTV = hasattr(pricing, "profit_percent_for")        # GTV's band depends on the row's fee rule (its own pricing.py)


def band_of(total, rule=None):
    """The profit % the sync's band logic gives this cost."""
    return pricing.profit_percent_for(total, rule=rule) if IS_GTV else pricing.profit_percent(total)


def legacy_of(total, rule=None):
    return pricing.legacy_profit_percents(total, rule=rule) if IS_GTV else pricing.legacy_profit_percents(total)


# ---------------------------------------------------------------- the misread, and its fix
@pytest.mark.parametrize("mirror", ["18", "16", None])
def test_the_effective_fee_the_automation_shows_is_not_an_override(mirror):
    cost = 20.0
    correct = pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    cell = shown(correct, RULE15)
    assert cell == "18.00"                                    # 15% commission + the 20% VAT OnBuy adds to it
    d = decide(cost, RULE15, correct, cell, "40.00",
               prev={"Cost Price (£)": cost, "Profit %": "40", "Fee %": mirror})
    assert d["fee_override"] is None and d["misread"] is False
    assert d["formula_price"] == correct and d["selling_price"] == correct and d["how"] == "kept"


def test_a_price_set_under_the_misread_follows_down_to_the_category_rule():
    cost = 20.0
    band = band_of(cost, RULE7)
    correct = pricing.price_for_profit(cost, band, rule=RULE7)
    first = pricing.price_for_profit(cost, band, rule=RULE7, legacy=True)         # the 09-17 model's own price
    cell = shown_legacy(first, RULE7)
    assert cell == "8.50"
    inflated = misread_price(cost, band, cell)                                     # what the sync set: fee 8.5 + 1.5 again
    assert inflated > correct
    d = decide(cost, RULE7, inflated, cell, f"{band:.2f}", prev={"Cost Price (£)": cost, "Fee %": "8"})
    assert d["how"] == "follows" and d["misread"] is True
    assert d["selling_price"] == correct < inflated


def test_the_misread_price_is_recognised_at_the_cost_the_mirror_last_saw():
    # the cost moved since the inflated price was set: only the mirror's cost explains the price
    old_cost, cost = 20.0, 21.0
    band = band_of(old_cost, RULE7)
    cell = shown_legacy(pricing.price_for_profit(old_cost, band, rule=RULE7, legacy=True), RULE7)
    inflated_then = misread_price(old_cost, band, cell)
    d = decide(cost, RULE7, inflated_then, cell, prev={"Cost Price (£)": old_cost, "Fee %": "8"})
    correct_now = pricing.price_for_profit(cost, band_of(cost, RULE7), rule=RULE7)
    # the new cost's formula is higher than the old inflated price, so it is simply raised - never lowered below it
    assert d["selling_price"] == max(inflated_then, correct_now)


def test_a_misread_price_set_at_a_higher_cost_follows_the_cost_down():
    old_cost, cost = 22.0, 20.0
    band_old = band_of(old_cost, RULE7)
    cell = shown_legacy(pricing.price_for_profit(old_cost, band_old, rule=RULE7, legacy=True), RULE7)
    inflated_then = misread_price(old_cost, band_old, cell)
    d = decide(cost, RULE7, inflated_then, cell, prev={"Cost Price (£)": old_cost, "Fee %": "8"})
    assert d["how"] == "follows" and d["misread"] is True
    assert d["selling_price"] == pricing.price_for_profit(cost, band_of(cost, RULE7), rule=RULE7)


def test_a_price_a_person_raised_is_never_lowered_by_the_fix():
    cost = 20.0
    band = band_of(cost, RULE7)
    cell = shown_legacy(pricing.price_for_profit(cost, band, rule=RULE7, legacy=True), RULE7)
    inflated = misread_price(cost, band, cell)
    hand = round(inflated + 3.0, 2)
    d = decide(cost, RULE7, hand, cell, prev={"Cost Price (£)": cost, "Fee %": "8"})
    assert d["how"] == "kept" and d["misread"] is False and d["selling_price"] == hand


def test_rows_without_a_category_rule_are_priced_flat_at_the_vat_inclusive_rate():
    # the flat fallback divides by 24% now (20% commission + the 20% VAT on it); the display it wrote before (21.50) is
    # still the automation's own, and the price set under it is raised to the real deduction
    cost = 20.0
    band = band_of(cost, None)
    correct = pricing.price_for_profit(cost, band)
    old = pricing.price_for_profit(cost, band, legacy=True)
    assert shown(correct, None) == "24.00" and shown_legacy(old, None) == "21.50" and correct > old
    d = decide(cost, None, old, "21.50", prev={"Cost Price (£)": cost, "Fee %": "22"})
    assert d["fee_override"] is None and d["how"] == "kept" and d["selling_price"] == correct


def test_tiered_category_blend_is_recognised_through_the_mirror():
    cost = 150.0
    band = band_of(cost, TIERED)
    first = pricing.price_for_profit(cost, band, rule=TIERED, legacy=True)       # the price before any Fee % cell existed
    cell = shown_legacy(first, TIERED)                                              # a blend of 16.50 and 9.50, e.g. 12.9x
    assert 9.5 < float(cell) < 16.5
    inflated = misread_price(cost, band, cell)
    d = decide(cost, TIERED, inflated, cell, prev={"Cost Price (£)": cost, "Fee %": mirror_of(cell)})
    assert d["fee_override"] is None
    assert d["selling_price"] == pricing.price_for_profit(cost, band, rule=TIERED)
    # and the blend the automation shows NOW is its own too
    now = pricing.price_for_profit(cost, band, rule=TIERED)
    d2 = decide(cost, TIERED, now, shown(now, TIERED), prev={"Cost Price (£)": cost, "Fee %": mirror_of(shown(now, TIERED))})
    assert d2["fee_override"] is None and d2["selling_price"] == now


def test_a_cell_the_automation_wrote_before_the_uplift_is_still_its_own():
    cost = 20.0
    correct = pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    d = decide(cost, RULE15, correct, "15.00", prev={})                 # the nominal rate, no mirror row
    assert d["fee_override"] is None and d["selling_price"] == correct


def test_a_second_run_changes_nothing():
    cost = 35.0
    band = band_of(cost, RULE15)
    old = pricing.price_for_profit(cost, band, rule=RULE15, legacy=True)         # what the last run wrote (16.50 shown)
    first = decide(cost, RULE15, old, shown_legacy(old, RULE15), prev={"Cost Price (£)": cost, "Fee %": "16"})
    assert first["selling_price"] > old                                           # raised to the real 18% deduction
    cell = shown(first["selling_price"], RULE15)                                  # the cell the sync now writes
    second = decide(cost, RULE15, first["selling_price"], cell, prev={"Cost Price (£)": cost, "Fee %": mirror_of(cell)})
    assert second["selling_price"] == first["selling_price"] and second["how"] == "kept"


# ---------------------------------------------------------------- the 2026-10-03 hotfix: stale 0.00 Profit % cells
def test_a_stale_zero_profit_cell_is_the_automations_own_not_a_zero_profit_price():
    # the sync writes 0.00 into Profit % for a row with no cost; a restocked row still shows it. Read as a 0%
    # override it dragged misread prices down to the no-profit price (-18% .. -30%).
    cost = 20.0
    band = band_of(cost, RULE7)
    cell = shown_legacy(pricing.price_for_profit(cost, band, rule=RULE7, legacy=True), RULE7)
    inflated = misread_price(cost, band, cell)
    d = decide(cost, RULE7, inflated, cell, "0.00", prev={"Cost Price (£)": cost, "Profit %": "40", "Fee %": "8"})
    assert d["profit_override"] is None
    assert d["how"] == "follows" and d["selling_price"] == pricing.price_for_profit(cost, band, rule=RULE7)
    assert d["selling_price"] > 0.95 * inflated                              # only the fee correction


def test_a_zero_profit_cell_never_prices_a_row_at_no_profit_on_the_amazon_tab_either():
    cost = 20.0
    d = decide(cost, RULE15, 21.0, shown(21.0, RULE15), "0", supplier="Amazon", prev={"Fee %": "16"})
    assert d["profit_override"] is None
    assert d["selling_price"] == pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)


def test_a_price_is_only_released_by_the_profit_that_explains_it():
    # a row with a real profit override (20, below the band's 40): a price set at the BAND's profit is not explained
    # by it, so it is not released down to the override's lower price - it stays as it was
    cost = 20.0
    band = band_of(cost, RULE7)
    assert band > 20
    cell = shown_legacy(pricing.price_for_profit(cost, band, rule=RULE7, legacy=True), RULE7)
    at_band = misread_price(cost, band, cell)
    kept = decide(cost, RULE7, at_band, cell, "20", prev={"Cost Price (£)": cost, "Profit %": "40", "Fee %": "8"})
    assert kept["profit_override"] == 20.0 and kept["misread"] is False
    assert kept["how"] == "kept" and kept["selling_price"] == at_band
    # ...while a price set at the override's own profit follows the corrected fee down (no more than the fee effect)
    at_override = pricing.price_for_profit(cost, 20, platform_fee_percent=float(cell), legacy=True)
    moved = decide(cost, RULE7, at_override, cell, "20", prev={"Cost Price (£)": cost, "Profit %": "40", "Fee %": "8"})
    assert moved["misread"] is True and moved["how"] == "follows"
    assert moved["selling_price"] == pricing.price_for_profit(cost, 20, rule=RULE7)
    assert moved["selling_price"] > 0.97 * at_override


def test_the_flat_fallback_display_on_a_categorised_row_is_not_an_override():
    # priced flat (21.50 shown), categorised later: the cell keeps showing the flat display
    cost = 20.0
    band = band_of(cost, RULE15)
    flat_price = pricing.price_for_profit(cost, band)
    d = decide(cost, RULE15, flat_price, "21.50", prev={"Cost Price (£)": cost, "Fee %": "22"})
    assert d["fee_override"] is None
    d2 = decide(cost, RULE15, flat_price, "21.50", prev={})
    assert d2["fee_override"] is None and d2["selling_price"] == pricing.price_for_profit(cost, band, rule=RULE15)


def test_the_sync_no_longer_writes_a_zero_profit_for_a_row_without_a_cost():
    text = SRC.read_text(encoding="utf-8")
    # a row without a cost has no profit to show: decide_price answers None and the loop writes nothing
    assert 'if "Profit %" in col_map and profit_override is None and _profit_shown is not None:' in text
    d = decide(0.0, RULE15, 42.0, "")
    assert d["profit_shown"] is None


# ---------------------------------------------------------------- 2026-10-03: superseded profits are the automation's own
def test_a_profit_cell_written_under_a_superseded_schedule_is_not_an_override():
    # With no mirror to vouch for it (the prefetch used to fail on big batches) a cell holding an OLD band's profit
    # for this cost - the 20 written before the temporary 15, the 40 before the 09-11 schedule - is the automation's
    # own: ~1,000 Amazon rows per store sat stuck at 20.00 while the band was 15.
    cost = 80.0
    legacy = legacy_of(cost, RULE15)
    assert legacy, "an 80-pound cost has a superseded profit"
    stale = legacy[0]
    existing = pricing.price_for_profit(cost, stale, rule=RULE15)
    d = decide(cost, RULE15, existing, shown(existing, RULE15), f"{stale:.2f}", prev={})
    live = pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    assert d["profit_override"] is None
    assert d["how"] == "follows" and d["selling_price"] == live < existing
    amazon = decide(cost, RULE15, existing, shown(existing, RULE15), f"{stale:.2f}", supplier="Amazon", prev={})
    assert amazon["profit_override"] is None and amazon["selling_price"] == live


def test_a_number_no_schedule_ever_used_for_this_cost_is_still_an_override():
    top = decide(80.0, RULE15, 0.0, "", "55", prev={})
    assert top["profit_override"] == 55.0
    mid = decide(20.0, RULE15, 0.0, "", "30", prev={})                 # the 10-50 range never moved
    assert mid["profit_override"] == 30.0


# ---------------------------------------------------------------- real overrides still win
def test_a_typed_fee_override_still_drives_the_formula_and_is_kept():
    cost = 20.0
    band = band_of(cost, RULE15)
    d = decide(cost, RULE15, 0.0, "10", prev={"Fee %": "16"})
    assert d["fee_override"] == 10.0
    assert d["formula_price"] == pricing.price_for_profit(cost, band, platform_fee_percent=10)
    held = decide(cost, RULE15, d["formula_price"], "10", prev={"Fee %": "16"})
    assert held["fee_override"] == 10.0 and held["selling_price"] == d["formula_price"]


def test_a_typed_profit_override_still_drives_the_formula():
    cost = 20.0
    d = decide(cost, RULE15, 0.0, shown(30.0, RULE15), "55", prev={"Profit %": "40", "Fee %": "16"})
    assert d["profit_override"] == 55.0 and d["fee_override"] is None
    assert d["formula_price"] == pricing.price_for_profit(cost, 55, rule=RULE15)


# ---------------------------------------------------------------- Amazon and the floor
def test_amazon_rows_re_derive_both_ways():
    # a price the automation set at an older cost follows Amazon's price down AND up
    old_cost, cost = 70.0, 60.0
    set_then = pricing.price_for_profit(old_cost, band_of(old_cost, RULE15), rule=RULE15)
    now = pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    assert set_then > now
    down = decide(cost, RULE15, set_then, shown(set_then, RULE15), supplier="Amazon", prev={"Cost Price (£)": old_cost, "Fee %": "18"})
    assert down["how"] == "amazon" and down["selling_price"] == now
    old_cost, cost = 60.0, 70.0
    set_then = pricing.price_for_profit(old_cost, band_of(old_cost, RULE15), rule=RULE15)
    up = decide(cost, RULE15, set_then, shown(set_then, RULE15), supplier="Amazon", prev={"Cost Price (£)": old_cost, "Fee %": "18"})
    assert up["how"] == "amazon" and up["selling_price"] == pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)


AMAZON_COST = 54.99                                                               # the SKU of 2026-10-07: 20% band, 15% category
AMAZON_FORMULA = pricing.price_for_profit(AMAZON_COST, band_of(AMAZON_COST, RULE15), rule=RULE15)


def test_the_real_amazon_row_prices_at_the_vat_inclusive_fee():
    assert band_of(AMAZON_COST, RULE15) == 20 and AMAZON_FORMULA == 80.47        # was 79.03 with the 1.5-point uplift


def test_an_amazon_price_a_person_raised_above_the_formula_is_kept():
    hand = 89.99
    mirror = {"Cost Price (£)": AMAZON_COST, "Fee %": "18", "Profit %": "20"}
    d = decide(AMAZON_COST, RULE15, hand, shown(hand, RULE15), "20.00", supplier="Amazon", prev=mirror)
    assert d["how"] == "manual" and d["selling_price"] == hand and d["formula_price"] == AMAZON_FORMULA
    # stable: the next run sees the same cell and decides the same
    again = decide(AMAZON_COST, RULE15, d["selling_price"], shown(d["selling_price"], RULE15), "20.00", supplier="Amazon", prev=mirror)
    assert again["how"] == "manual" and again["selling_price"] == hand
    # ...with no mirror row at all (a failed prefetch, a new SKU) it is still a person's price
    bare = decide(AMAZON_COST, RULE15, hand, shown(hand, RULE15), "20.00", supplier="Amazon", prev={})
    assert bare["how"] == "manual" and bare["selling_price"] == hand


def test_a_manual_amazon_price_stays_when_the_cost_falls_and_gives_way_when_the_formula_overtakes_it():
    hand = 89.99
    cheaper = 49.0                                                                # formula falls well below the manual price
    assert pricing.price_for_profit(cheaper, band_of(cheaper, RULE15), rule=RULE15) < hand
    d = decide(cheaper, RULE15, hand, shown(hand, RULE15), supplier="Amazon", prev={"Cost Price (£)": AMAZON_COST})
    assert d["how"] == "manual" and d["selling_price"] == hand
    dearer = 80.0                                                                 # formula climbs past it
    up = pricing.price_for_profit(dearer, band_of(dearer, RULE15), rule=RULE15)
    assert up > hand
    d2 = decide(dearer, RULE15, hand, shown(hand, RULE15), supplier="Amazon", prev={"Cost Price (£)": AMAZON_COST})
    assert d2["how"] == "amazon" and d2["selling_price"] == up


def test_an_amazon_price_below_the_formula_is_raised_to_it():
    d = decide(AMAZON_COST, RULE15, AMAZON_FORMULA - 5.0, "18.00", supplier="Amazon", prev={"Cost Price (£)": AMAZON_COST})
    assert d["how"] == "amazon" and d["selling_price"] == AMAZON_FORMULA


@pytest.mark.parametrize("where", ["mirror", "sheet cell", "both"])
def test_an_amazon_price_the_automation_set_at_a_higher_cost_follows_the_cost_down(where):
    old_cost, cost = 70.0, 60.0
    set_then = pricing.price_for_profit(old_cost, band_of(old_cost, RULE15), rule=RULE15)
    now = pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    extra = {}
    if where in ("sheet cell", "both"):
        extra["sheet_cost"] = old_cost                                            # the Cost cell written with the price
    prev = {"Cost Price (£)": old_cost} if where in ("mirror", "both") else {}
    d = decide(cost, RULE15, set_then, shown(set_then, RULE15), supplier="Amazon", prev=prev, **extra)
    assert d["how"] == "amazon" and d["selling_price"] == now < set_then


def test_an_amazon_price_set_under_the_old_fee_model_is_still_the_automations_own():
    # deployed 2026-10-07: prices written the day before used nominal + 1.5 points; if the cost has fallen since, such a
    # price is above the new formula and must not be mistaken for a person's
    old_cost, cost = 100.0, 90.0
    legacy_price = pricing.price_for_profit(old_cost, band_of(old_cost, RULE15), rule=RULE15, legacy=True)
    now = pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    assert legacy_price > now
    d = decide(cost, RULE15, legacy_price, shown_legacy(legacy_price, RULE15), supplier="Amazon",
               prev={"Cost Price (£)": old_cost, "Fee %": "16"}, sheet_cost=old_cost)
    assert d["how"] == "amazon" and d["selling_price"] == now


def test_an_amazon_price_set_before_a_recategorisation_is_still_the_automations_own():
    cost = 60.0
    dear = pricing.price_for_profit(cost, band_of(cost, RULE20), rule=RULE20)       # set while the row sat in a 20% category
    now = pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    assert dear > now
    d = decide(cost, RULE15, dear, shown(dear, RULE20), supplier="Amazon", prev={"Cost Price (£)": cost, "Fee %": "24"},
               sheet_cost=cost, prev_fee_rule=RULE20)
    assert d["how"] == "amazon" and d["selling_price"] == now


def test_an_amazon_price_set_under_a_typed_profit_override_that_was_cleared_is_taken_for_a_persons():
    # the known limit, stated: nothing records that an override once explained the price
    cost = 20.0
    at_30 = pricing.price_for_profit(cost, 30, rule=RULE15)
    assert at_30 < pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    d = decide(cost, RULE15, at_30 + 10.0, shown(at_30, RULE15), "", supplier="Amazon", prev={"Cost Price (£)": cost})
    assert d["how"] == "manual"


@pytest.mark.parametrize("cell,expected", [
    ("89.99", 89.99), (89.99, 89.99), (90, 90.0), ("£89.99", 89.99), (" 89.99 ", 89.99), ("1,250.50", 1250.5),
    ("89,99", 89.99), ("16.5%", 16.5), ("", None), (None, None), ("abc", None), ("n/a", None), (True, None),
])
def test_cell_number_reads_what_people_type(cell, expected):
    got = NS["_cell_number"](cell)
    assert got == expected if expected is None else got == pytest.approx(expected)


@pytest.mark.parametrize("a,b,differ", [
    (79.03, "79.03", False), ("£79.03", 79.03, False), (79.03, 89.99, True), ("", None, False), ("", 0, True),
    ("16.50", 16.5, False), ("20", "30", True), ("note", "note", False), ("note", "other", True), (79.03, 79.034, False), (79.03, 79.04, True),
])
def test_cells_differ(a, b, differ):
    assert NS["_cells_differ"](a, b) is differ


def _guard(updates, snap, fresh, *, snap_skus=("SKU", "A", "B", "C"), fresh_skus=("SKU", "A", "B", "C")):
    """Run drop_edited_cells over a tiny sheet: columns B (Selling Price (£)) and C (Profit %) are guarded, D is not."""
    guarded = {"B": "Selling Price (£)", "C": "Profit %"}

    def value(table, name, row):
        return table.get((name, row), "")
    return NS["drop_edited_cells"](updates, guarded, list(snap_skus), lambda n, r: value(snap, n, r),
                                   list(fresh_skus), lambda n, r: value(fresh, n, r))


def test_a_price_typed_while_a_run_was_in_progress_is_not_written_over():
    snap = {("Selling Price (£)", 2): 79.03, ("Selling Price (£)", 3): 50.0, ("Profit %", 2): "20.00"}
    fresh = {("Selling Price (£)", 2): "89.99", ("Selling Price (£)", 3): "50", ("Profit %", 2): "20.00"}   # row 2 edited by a person
    updates = [{"range": "B2", "values": [[80.47]]}, {"range": "B3", "values": [[51.0]]}, {"range": "C2", "values": [["20.00"]]},
               {"range": "D2", "values": [["x"]]}]
    kept, dropped = _guard(updates, snap, fresh)
    assert [u["range"] for u in kept] == ["B3", "C2", "D2"]                        # the run's other writes and unguarded columns stand
    assert dropped == [("A", "Selling Price (£)", "89.99")]


def test_an_override_typed_during_a_run_survives_the_runs_own_percentage_write():
    snap = {("Profit %", 3): "20.00"}
    fresh = {("Profit %", 3): "30"}
    kept, dropped = _guard([{"range": "C3", "values": [["20.00"]]}], snap, fresh)
    assert kept == [] and dropped == [("B", "Profit %", "30")]


def test_nothing_is_dropped_when_nobody_edited():
    snap = {("Selling Price (£)", 2): 79.03, ("Profit %", 2): "20.00"}
    fresh = {("Selling Price (£)", 2): "79.03", ("Profit %", 2): "20.00"}
    updates = [{"range": "B2", "values": [[80.47]]}, {"range": "C2", "values": [["20.00"]]}]
    kept, dropped = _guard(updates, snap, fresh)
    assert kept == updates and dropped == []


def test_the_guard_follows_the_sku_when_rows_moved_and_leaves_ambiguous_writes_to_the_remap():
    snap = {("Selling Price (£)", 2): 79.03}
    # a row was inserted above: SKU A now sits on row 3 and was edited there
    fresh = {("Selling Price (£)", 3): "89.99"}
    kept, dropped = _guard([{"range": "B2", "values": [[80.47]]}], snap, fresh, fresh_skus=("SKU", "NEW", "A", "B", "C"))
    assert kept == [] and dropped == [("A", "Selling Price (£)", "89.99")]
    # the SKU appears twice (or vanished): not this guard's call - the remap drops it
    dup, none = _guard([{"range": "B2", "values": [[80.47]]}], snap, fresh, fresh_skus=("SKU", "A", "A", "C"))
    assert dup == [{"range": "B2", "values": [[80.47]]}] and none == []
    gone, none2 = _guard([{"range": "B2", "values": [[80.47]]}], snap, fresh, fresh_skus=("SKU", "X", "B", "C"))
    assert gone == [{"range": "B2", "values": [[80.47]]}] and none2 == []


def test_the_flush_runs_the_guard_before_the_row_remap_and_the_price_is_read_tolerantly():
    text = SRC.read_text(encoding="utf-8")
    assert text.index("drop_edited_cells(all_sheet_updates") < text.index('_remap_row_writes(all_sheet_updates, "run sheet writes")')
    assert 'existing_price = _cell_number(row.get("Selling Price (£)")) or 0.0' in text
    assert "sheet_cost=_to_float(row.get(\"Cost Price (£)\"))" in text


def test_a_price_below_the_formula_is_raised_to_it():
    cost = 20.0
    correct = pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    d = decide(cost, RULE15, correct - 4.0, shown(correct, RULE15), prev={"Cost Price (£)": cost, "Fee %": "16"})
    assert d["selling_price"] == correct and d["how"] == "kept"


def test_no_cost_means_no_formula_and_the_price_stays():
    d = decide(0.0, RULE15, 42.0, "16.50")
    assert d["formula_price"] == 0.0 and d["selling_price"] == 42.0


# ---------------------------------------------------------------- delivery cost joins the cost base
def test_delivery_cost_is_part_of_the_cost_base_and_the_band():
    cost, ship = 8.0, 3.0                                   # 11 -> out of the 80% band (GTV: its own 10-20 rule)
    d = decide(cost, RULE15, 0.0, "", ship=ship)
    assert d["band_now"] == band_of(11.0, RULE15)
    assert d["formula_price"] == pricing.price_for_profit(11.0, band_of(11.0, RULE15), rule=RULE15)


@pytest.mark.parametrize("cell,expected", [
    ("", 0.0), (None, 0.0), ("free", 0.0), ("FREE", 0.0), (" Free ", 0.0), ("n/a", 0.0),
    ("3.99", 3.99), (3.99, 3.99), (4, 4.0), ("£3.99", 3.99), ("3,99", 3.99),
    ("0", 0.0), ("-2", 0.0), ("nan", 0.0), ("inf", 0.0), ("1,250", 0.0), ("5000", 0.0),     # absurd amounts count as none
])
def test_shipping_cell_values(cell, expected):
    assert NS["_shipping_value"](cell) == pytest.approx(expected)


def _opt(value, currency="GBP", **kw):
    o = {"shippingCostType": "FIXED", "shippingCost": {"value": value, "currency": currency}}
    o.update(kw)
    return o


@pytest.mark.parametrize("item,expected", [
    ({"shippingOptions": [_opt("0.00")]}, 0.0),
    ({"shippingOptions": [_opt("3.99")]}, 3.99),
    ({"shippingOptions": [_opt("6.99"), _opt("2.49"), _opt("4.00")]}, 2.49),          # the cheapest delivered option
    ({"shippingOptions": [_opt("0.00", type="PICKUP"), _opt("4.50")]}, 4.5),           # free collection is not delivery
    ({"shippingOptions": [_opt("0.00", shippingServiceCode="UK_CollectInPerson"), _opt("3.00")]}, 3.0),
    ({"shippingOptions": [_opt("5.00", "USD"), _opt("3.00")]}, 3.0),                   # another currency is ignored
    ({"shippingOptions": [_opt("5.00", "USD")]}, None),
    ({"shippingOptions": [{"shippingCostType": "CALCULATED"}]}, None),                 # eBay gave no figure
    ({"shippingOptions": [_opt("2.00", shippingCostType="CALCULATED")]}, None),        # a postcode-dependent figure is not "exact"
    ({"shippingOptions": [_opt("2.00", shippingCostType="CALCULATED"), _opt("4.00")]}, 4.0),   # only the written fee counts
    ({"shippingOptions": [{"shippingCost": {"value": "2.50", "currency": "GBP"}}]}, 2.5),      # type not stated: the figure is taken
    ({"shippingOptions": []}, None),
    ({}, None), (None, None),
    ({"shippingOptions": [_opt("-1.00")]}, None),
    ({"shippingOptions": [_opt("abc")]}, None),
    ({"shippingOptions": [_opt("3.999")]}, 4.0),
    ({"shippingOptions": ["x", _opt("2.00")]}, 2.0),
])
def test_ebay_shipping_cost(item, expected):
    got = NS["ebay_shipping_cost"](item)
    assert got == expected if expected is None else got == pytest.approx(expected)


@pytest.mark.parametrize("quote,current,expected", [
    (3.99, None, 3.99), (3.99, "", 3.99), (3.99, "free", 3.99), (3.99, 0, 3.99),            # a stated fee is written
    (3.99, "3.99", None), (3.99, 3.99, None), (3.99, "£3.99", None),                       # ...once: a steady row costs no edit
    (3.99, "2.50", 3.99), (3.99, "abc", 3.99),                                               # the fee changed / text in the cell
    (0.0, None, None), (0.0, "", None), (0.0, "free", None), (0.0, "FREE", None), (0.0, "0", None),   # free: a blank cell stays blank
    (0.0, "3.99", "free"), (0.0, 3.99, "free"),                                              # ...but an earlier fee is cleared to "free"
    (None, "3.99", None), (None, "", None),                                                  # no exact quote never touches the cell
])
def test_shipping_cell_update(quote, current, expected):
    assert NS["shipping_cell_update"](quote, current) == expected


def test_quantile():
    q = NS["_quantile"]
    assert q([], 0.5) == 0.0
    assert q([5.0], 0.9) == 5.0
    assert q([4, 1, 3, 2], 0.5) == 3 and q([4, 1, 3, 2], 0.9) == 4 and q([4, 1, 3, 2], 0.0) == 1


def test_get_ebay_data_carries_the_quote_and_the_loop_uses_the_decision():
    text = SRC.read_text(encoding="utf-8")
    assert '"shipping_cost": ebay_shipping_cost(data)' in text
    assert "_price = decide_price(" in text
    # unset = shadow: the quote is measured and logged, nothing is written or repriced until the switch is 1
    assert '(os.getenv("EBAY_SHIPPING_COST") or "shadow")' in text


# ---------------------------------------------------------------- nothing else moved: fuzz against the old block
def _legacy_resolve(cell, auto_values, mirror, hi=100):
    typed = NS["_pct_value"](cell, hi)
    if typed is None:
        return None
    candidates = [v for v in list(auto_values) + [NS["_pct_value"](mirror, hi)] if v is not None]
    return None if any(abs(typed - v) < 0.05 for v in candidates) else typed


def _legacy_decide(supplier, cost_price, shipping_cost, fee_rule, existing_price, fee_cell, profit_cell, prev):
    """The pricing block exactly as it stood in generate_xml.py before 2026-10-02."""
    to_f, formula_priced = NS["_to_float"], NS["_formula_priced"]
    total = cost_price + shipping_cost
    band_now = band_of(total, fee_rule) if cost_price > 0 else None
    prev_total = to_f(prev.get("Cost Price (£)")) + to_f(prev.get("Shipping Cost (£)"))
    band_prev = band_of(prev_total, fee_rule) if prev_total > 0 else None
    autos = [band_now, band_prev]
    if IS_GTV:                                           # the displaced cost-band values stay in the auto list
        autos += [pricing.profit_percent(total) if cost_price > 0 else None,
                  pricing.profit_percent(prev_total) if prev_total > 0 else None]
    profit_override = _legacy_resolve(profit_cell, autos, prev.get("Profit %"), hi=500)
    fee_auto = ([fee_rule.lower_pct, fee_rule.upper_pct] if fee_rule is not None
                else [float(pricing.PLATFORM_FEE_PERCENT)])
    fee_override = _legacy_resolve(fee_cell, fee_auto, prev.get("Fee %"))
    used = profit_override if profit_override is not None else (band_now or 0)
    if cost_price <= 0:
        formula = 0.0
    elif fee_override is not None:
        formula = pricing.price_for_profit(total, used, platform_fee_percent=fee_override)
    else:
        formula = pricing.price_for_profit(total, used, rule=fee_rule)
    auto = (formula_priced(existing_price, cost_price, shipping_cost, fee_rule, profit_override, fee_override)
            or formula_priced(existing_price, to_f(prev.get("Cost Price (£)")), to_f(prev.get("Shipping Cost (£)")),
                              fee_rule, profit_override, fee_override))
    basis = (fee_rule is not None or profit_override is not None or fee_override is not None
             or bool(legacy_of(total, fee_rule)))
    if supplier == "Amazon" and formula > 0:
        if existing_price > formula + 0.011 and not auto:
            how, selling = "manual", existing_price
        else:
            how, selling = "amazon", formula
    elif basis and auto and 0 < formula < existing_price:
        how, selling = "follows", formula
    else:
        how, selling = "kept", max(existing_price, formula)
    return band_now, profit_override, fee_override, formula, selling, how


def test_every_path_that_never_misread_prices_exactly_as_before():
    rng = random.Random(20261002)
    rules = [None, RULE7, RULE15, RULE20, TIERED]
    # No fee the automation shows lies near these: its rates run 8.5-21.5 and a tiered blend or a minimum-fee
    # effect stays inside that, so a typed 11.3 on a tiered category can BE the displayed blend (that is the fix).
    far_fee = [3.0, 5.5, 30.0, 45.0]
    far_profit = [17.0, 55.0, 130.0]
    compared = 0
    for _ in range(4000):
        rule = rng.choice(rules)
        cost = round(rng.choice([rng.uniform(0.5, 12), rng.uniform(12, 60), rng.uniform(60, 400)]), 2)
        ship = rng.choice([0.0, 0.0, round(rng.uniform(0.5, 9.0), 2)])
        total = cost + ship
        band = band_of(total, rule)
        formula_here = pricing.price_for_profit(total, band, rule=rule)
        existing = rng.choice([0.0, formula_here, round(formula_here * rng.uniform(0.6, 1.6), 2),
                               pricing.price_for_profit(total, band, platform_fee_percent=20.0),
                               pricing.price_for_profit(total, rng.choice(legacy_of(total, rule) or [band]),
                                                        rule=rule)])
        prev_cost = rng.choice([cost, round(cost * rng.uniform(0.7, 1.3), 2)])
        prev = {"Cost Price (£)": prev_cost, "Shipping Cost (£)": str(ship) if ship else None}
        fee_cell = rng.choice([None, "", rng.choice(far_fee), f"{rng.choice(far_fee):.2f}"])
        profit_cell = rng.choice([None, "", f"{band:.2f}", str(rng.choice(far_profit))])
        prev["Fee %"] = rng.choice([None, "20", "16"])          # never within half a point of a typed override above
        prev["Profit %"] = rng.choice([None, str(band)])
        supplier = rng.choice(["eBay", "eBay", "Amazon"])
        new = decide_price(supplier=supplier, cost_price=cost, shipping_cost=ship, fee_rule=rule,
                           existing_price=existing, fee_cell=fee_cell, profit_cell=profit_cell, prev=prev)
        old = _legacy_decide(supplier, cost, ship, rule, existing, fee_cell, profit_cell, prev)
        assert (new["band_now"], new["profit_override"], new["fee_override"], new["formula_price"],
                new["selling_price"], new["how"]) == old, (cost, ship, rule, existing, fee_cell, profit_cell, prev, supplier)
        assert new["misread"] is False
        compared += 1
    assert compared == 4000


# ---------------------------------------------------------------- 2026-10-07: the Profit % cell shows what the price REALLY earns
@pytest.mark.parametrize("value,text", [(20.0, "20.00"), (15, "15.00"), (34.19, "34.19"), (34.1931, "34.1931"), (100.5, "100.50"),
                                        (0.0, "0.00"), (7.123456, "7.123456"), (130.0, "130.00")])
def test_pct_text_keeps_two_decimals_and_only_adds_more_when_needed(value, text):
    assert NS["_pct_text"](value) == text


def _profit_earned(price, cost, rule, ship=0.0):
    """What the price leaves after OnBuy's commission (VAT included), as a share of the cost base."""
    return ((price - pricing.fee_amount(price, rule)) / (cost + ship) - 1) * 100


def test_effective_profit_is_the_inverse_of_the_price_formula():
    rng = random.Random(20261007)
    rules = [None, RULE7, RULE15, RULE20, TIERED]
    for _ in range(2000):
        rule = rng.choice(rules)
        total = round(rng.uniform(0.5, 400), 2)
        profit = rng.choice([15, 20, 40, 80, 100, 34.5, 150])
        price = pricing.price_for_profit(total, profit, rule=rule)
        # the price is rounded to the penny, so the profit it earns is the asked one within that rounding
        assert pricing.effective_profit_percent(price, total, rule) == pytest.approx(profit, abs=0.006 / total * 100 + 1e-6)
    assert pricing.effective_profit_percent(0, 10) == 0.0 and pricing.effective_profit_percent(10, 0) == 0.0


def test_the_real_row_shows_its_real_profit():
    # SKU 294148259721 (GTV Amazon): cost 54.99, a person's 89.99 against the 80.47 formula, the 15% category (18% with VAT)
    hand = 89.99
    d = decide(AMAZON_COST, RULE15, hand, shown(hand, RULE15), "20.00", supplier="Amazon", prev={"Cost Price (£)": AMAZON_COST, "Fee %": "18", "Profit %": "20"})
    assert d["how"] == "manual" and d["selling_price"] == hand
    assert d["profit_shown"] == pytest.approx(34.19, abs=0.005)             # (89.99 - 16.20) / 54.99 - 1
    assert d["profit_shown"] == pytest.approx(_profit_earned(hand, AMAZON_COST, RULE15), abs=0.005)
    assert NS["_pct_text"](d["profit_shown"]) == "34.19"


def test_a_row_priced_by_the_formula_still_shows_its_band():
    cost = 20.0
    price = pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    for supplier in ("eBay", "Amazon"):
        d = decide(cost, RULE15, price, shown(price, RULE15), "40.00", supplier=supplier, prev={"Cost Price (£)": cost, "Profit %": "40", "Fee %": "18"})
        assert d["profit_shown"] == band_of(cost, RULE15) == d["band_now"]
    # a price a cent over the formula (rounding) is not "above" it
    near = decide(cost, RULE15, price + 0.01, "18.00", "40.00", prev={"Cost Price (£)": cost, "Profit %": "40", "Fee %": "18"})
    assert near["profit_shown"] == band_of(cost, RULE15)


def test_the_ebay_tab_shows_the_real_profit_of_a_price_kept_above_the_formula_too():
    cost = 20.0
    formula = pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    hand = round(formula * 1.3, 2)
    d = decide(cost, RULE15, hand, shown(hand, RULE15), "40.00", prev={"Cost Price (£)": cost, "Profit %": "40", "Fee %": "18"})
    assert d["how"] == "kept" and d["selling_price"] == hand
    assert d["profit_shown"] == pytest.approx(_profit_earned(hand, cost, RULE15), abs=0.005) and d["profit_shown"] > band_of(cost, RULE15)


def test_a_typed_profit_override_stays_exactly_as_typed_and_nothing_is_shown_over_it():
    cost = 20.0
    d = decide(cost, RULE15, 0.0, "", "55", prev={"Profit %": "40", "Fee %": "18"})
    assert d["profit_override"] == 55.0 and d["profit_shown"] is None


def test_the_shown_profit_is_the_automations_own_through_the_mirror_and_a_typed_override_that_equals_it_is_still_an_override():
    hand = 89.99
    mirror = {"Cost Price (£)": AMAZON_COST, "Fee %": "18", "Profit %": "34"}              # what the last run stored: the shown profit, whole
    shown_cell = NS["_pct_text"](decide(AMAZON_COST, RULE15, hand, "18.00", "20.00", supplier="Amazon", prev={"Cost Price (£)": AMAZON_COST})["profit_shown"])
    d = decide(AMAZON_COST, RULE15, hand, "18.00", shown_cell, supplier="Amazon", prev=mirror)
    assert d["profit_override"] is None and d["how"] == "manual" and d["selling_price"] == hand       # the cell it wrote is not an override
    # ...but a person typing a profit while the mirror still holds the band (it never stores overrides) IS an override, even
    # when the number equals the profit the price earns - a typed profit drives the price, whatever the cell shows
    cost = 20.0
    typed = 55.0
    at_typed = pricing.price_for_profit(cost, typed, rule=RULE15)
    ov = decide(cost, RULE15, at_typed, "18.00", "55", prev={"Cost Price (£)": cost, "Profit %": str(int(band_of(cost, RULE15))), "Fee %": "18"})
    assert ov["profit_override"] == 55.0 and ov["profit_shown"] is None
    cheaper = decide(cost * 0.9, RULE15, at_typed, "18.00", "55", prev={"Cost Price (£)": cost, "Profit %": str(int(band_of(cost, RULE15))), "Fee %": "18"})
    assert cheaper["profit_override"] == 55.0
    assert cheaper["selling_price"] == pricing.price_for_profit(cost * 0.9, 55, rule=RULE15) < at_typed        # follows the cost down with its profit


def test_the_shown_profit_prices_back_to_the_same_penny_even_if_it_were_read_as_an_override():
    # the mirror failed to store it: the cell then reads as a typed override of that profit - the price must not move by a cent
    rng = random.Random(7)
    for _ in range(1500):
        rule = rng.choice([None, RULE7, RULE15, RULE20, TIERED])
        cost = round(rng.choice([rng.uniform(0.5, 12), rng.uniform(12, 60), rng.uniform(60, 400)]), 2)
        formula = pricing.price_for_profit(cost, band_of(cost, rule), rule=rule)
        hand = round(formula * rng.uniform(1.02, 2.2) + rng.choice([0, 0.01, 0.37]), 2)
        profit = NS["_shown_profit"](hand, cost, rule, None)
        assert pricing.price_for_profit(cost, profit, rule=rule) == hand, (cost, rule, hand, profit)
        assert abs(profit - _profit_earned(hand, cost, rule)) < 0.01 + 6 * 10 ** -6 or abs(profit - _profit_earned(hand, cost, rule)) < 0.0051 / cost * 100


def test_a_fee_override_is_used_for_the_shown_profit_too():
    cost = 20.0
    formula = pricing.price_for_profit(cost, band_of(cost, RULE15), platform_fee_percent=10)
    hand = round(formula * 1.4, 2)
    d = decide(cost, RULE15, hand, "10", "40.00", prev={"Cost Price (£)": cost, "Profit %": "40", "Fee %": "18"})
    assert d["fee_override"] == 10.0 and d["selling_price"] == hand
    assert d["profit_shown"] == pytest.approx(((hand * (1 - pricing.effective_rate(10) / 100)) / cost - 1) * 100, abs=0.01)


def test_the_manual_price_row_is_stable_run_after_run_with_its_shown_profit():
    hand = 89.99
    mirror = {"Cost Price (£)": AMAZON_COST, "Fee %": "18", "Profit %": "20"}
    cell = "20.00"
    for _ in range(4):
        d = decide(AMAZON_COST, RULE15, hand, shown(hand, RULE15), cell, supplier="Amazon", prev=mirror, sheet_cost=AMAZON_COST)
        assert d["how"] == "manual" and d["selling_price"] == hand and d["profit_override"] is None
        cell = NS["_pct_text"](d["profit_shown"])                               # what the sync writes to the sheet...
        mirror = {"Cost Price (£)": AMAZON_COST, "Fee %": "18", "Profit %": str(int(round(d["profit_shown"])))}   # ...and to the mirror
    assert cell == "34.19"


def test_the_shown_profit_follows_the_cost_and_returns_to_the_band_when_the_formula_overtakes_the_price():
    hand = 89.99
    mirror = {"Cost Price (£)": AMAZON_COST, "Fee %": "18", "Profit %": "34"}
    cell = "34.19"
    dearer = 60.0                                                                # the formula is still below the manual price
    d = decide(dearer, RULE15, hand, "18.00", cell, supplier="Amazon", prev=mirror, sheet_cost=AMAZON_COST)
    assert d["how"] == "manual" and d["profit_shown"] == pytest.approx(_profit_earned(hand, dearer, RULE15), abs=0.005)
    assert d["profit_shown"] < 34.19                                              # a dearer cost leaves less profit on the same price
    mirror2 = {"Cost Price (£)": dearer, "Fee %": "18", "Profit %": str(int(round(d["profit_shown"])))}
    much_dearer = 80.0                                                           # now the formula passes the manual price
    d2 = decide(much_dearer, RULE15, hand, "18.00", NS["_pct_text"](d["profit_shown"]), supplier="Amazon", prev=mirror2, sheet_cost=dearer)
    assert d2["how"] == "amazon" and d2["profit_override"] is None
    assert d2["profit_shown"] == band_of(much_dearer, RULE15)                    # back to the band


def test_clearing_the_price_returns_the_row_to_the_formula_and_the_band():
    mirror = {"Cost Price (£)": AMAZON_COST, "Fee %": "18", "Profit %": "34"}
    d = decide(AMAZON_COST, RULE15, 0.0, "", "34.19", supplier="Amazon", prev=mirror, sheet_cost=AMAZON_COST)
    assert d["profit_override"] is None and d["selling_price"] == AMAZON_FORMULA and d["profit_shown"] == band_of(AMAZON_COST, RULE15)
