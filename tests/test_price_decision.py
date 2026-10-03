"""decide_price (the pricing decision of the sync loop), the Fee % reading and
the eBay delivery cost helpers. Extracted from generate_xml.py's source - the
module itself needs gspread and runs on import.

2026-10-02: the Fee % cell shows the EFFECTIVE fee (nominal + the 1.5-point
uplift, e.g. 16.50) while the Supabase mirror holds whole numbers, so every
cell was read as a manual fee override and priced as a flat fee plus the
uplift a second time (~1.8% too high). These tests pin the fix, that such
prices follow the corrected formula DOWN, that real overrides still win, and
that every other path prices exactly as before (fuzz against the old block).
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
         "_misread_fee_priced", "decide_price", "_shipping_value", "ebay_shipping_cost", "_quantile")


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


def decide(cost, rule, existing, fee_cell, profit_cell="", ship=0.0, supplier="eBay", prev=None):
    return decide_price(supplier=supplier, cost_price=cost, shipping_cost=ship, fee_rule=rule,
                        existing_price=existing, fee_cell=fee_cell, profit_cell=profit_cell, prev=prev or {})


def shown(price, rule):
    """The Fee % cell text the sync writes for a row priced at `price`."""
    return f"{pricing.effective_fee_percent(price, rule):.2f}"


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
@pytest.mark.parametrize("mirror", ["16", "17", None])
def test_the_effective_fee_the_automation_shows_is_not_an_override(mirror):
    cost = 20.0
    correct = pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    cell = shown(correct, RULE15)
    assert cell == "16.50"
    d = decide(cost, RULE15, correct, cell, "40.00",
               prev={"Cost Price (£)": cost, "Profit %": "40", "Fee %": mirror})
    assert d["fee_override"] is None and d["misread"] is False
    assert d["formula_price"] == correct and d["selling_price"] == correct and d["how"] == "kept"


def test_a_price_set_under_the_misread_follows_down_to_the_category_rule():
    cost = 20.0
    band = band_of(cost, RULE15)
    correct = pricing.price_for_profit(cost, band, rule=RULE15)
    inflated = pricing.price_for_profit(cost, band, platform_fee_percent=16.5)      # what the sync set: fee 16.5 + 1.5 again
    assert inflated > correct
    d = decide(cost, RULE15, inflated, "16.50", f"{band:.2f}", prev={"Cost Price (£)": cost, "Fee %": "16"})
    assert d["how"] == "follows" and d["misread"] is True
    assert d["selling_price"] == correct < inflated


def test_the_misread_price_is_recognised_at_the_cost_the_mirror_last_saw():
    # the cost moved since the inflated price was set: only the mirror's cost explains the price
    old_cost, cost = 20.0, 21.0
    band = band_of(old_cost, RULE15)
    inflated_then = pricing.price_for_profit(old_cost, band, platform_fee_percent=16.5)
    d = decide(cost, RULE15, inflated_then, "16.50", prev={"Cost Price (£)": old_cost, "Fee %": "16"})
    correct_now = pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    # the new cost's formula is higher than the old inflated price, so it is simply raised - never lowered below it
    assert d["selling_price"] == max(inflated_then, correct_now)


def test_a_misread_price_set_at_a_higher_cost_follows_the_cost_down():
    old_cost, cost = 22.0, 20.0
    inflated_then = pricing.price_for_profit(old_cost, band_of(old_cost, RULE15), platform_fee_percent=16.5)
    d = decide(cost, RULE15, inflated_then, "16.50", prev={"Cost Price (£)": old_cost, "Fee %": "16"})
    assert d["how"] == "follows" and d["misread"] is True
    assert d["selling_price"] == pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)


def test_a_price_a_person_raised_is_never_lowered_by_the_fix():
    cost = 20.0
    band = band_of(cost, RULE15)
    inflated = pricing.price_for_profit(cost, band, platform_fee_percent=16.5)
    hand = round(inflated + 3.0, 2)
    d = decide(cost, RULE15, hand, "16.50", prev={"Cost Price (£)": cost, "Fee %": "16"})
    assert d["how"] == "kept" and d["misread"] is False and d["selling_price"] == hand


def test_rows_without_a_category_rule_follow_down_too():
    # flat fallback: the cell shows 21.50 (20 + 1.5); the misread priced it at 21.5 + 1.5 = 23%
    cost = 20.0
    band = band_of(cost, None)
    correct = pricing.price_for_profit(cost, band)
    inflated = pricing.price_for_profit(cost, band, platform_fee_percent=21.5)
    assert shown(correct, None) == "21.50" and inflated > correct
    d = decide(cost, None, inflated, "21.50", prev={"Cost Price (£)": cost, "Fee %": "22"})
    assert d["how"] == "follows" and d["selling_price"] == correct


def test_tiered_category_blend_is_recognised_through_the_mirror():
    cost = 150.0
    band = band_of(cost, TIERED)
    first = pricing.price_for_profit(cost, band, rule=TIERED)            # the price before any Fee % cell existed
    cell = shown(first, TIERED)                                           # a blend of 16.50 and 9.50, e.g. 12.9x
    assert 9.5 < float(cell) < 16.5
    inflated = pricing.price_for_profit(cost, band, platform_fee_percent=float(cell))
    d = decide(cost, TIERED, inflated, cell, prev={"Cost Price (£)": cost, "Fee %": mirror_of(cell)})
    assert d["fee_override"] is None and d["how"] == "follows"
    assert d["selling_price"] == pricing.price_for_profit(cost, band, rule=TIERED)


def test_a_cell_the_automation_wrote_before_the_uplift_is_still_its_own():
    cost = 20.0
    correct = pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    d = decide(cost, RULE15, correct, "15.00", prev={})                 # the nominal rate, no mirror row
    assert d["fee_override"] is None and d["selling_price"] == correct


def test_a_second_run_changes_nothing():
    cost = 35.0
    band = band_of(cost, RULE15)
    inflated = pricing.price_for_profit(cost, band, platform_fee_percent=16.5)
    first = decide(cost, RULE15, inflated, "16.50", prev={"Cost Price (£)": cost, "Fee %": "16"})
    cell = shown(first["selling_price"], RULE15)                          # the cell the sync now writes
    second = decide(cost, RULE15, first["selling_price"], cell, prev={"Cost Price (£)": cost, "Fee %": mirror_of(cell)})
    assert second["selling_price"] == first["selling_price"] and second["how"] == "kept"


# ---------------------------------------------------------------- the 2026-10-03 hotfix: stale 0.00 Profit % cells
def test_a_stale_zero_profit_cell_is_the_automations_own_not_a_zero_profit_price():
    # the sync writes 0.00 into Profit % for a row with no cost; a restocked row still shows it. Read as a 0%
    # override it dragged misread prices down to the no-profit price (-18% .. -30%).
    cost = 20.0
    band = band_of(cost, RULE15)
    inflated = pricing.price_for_profit(cost, band, platform_fee_percent=16.5)
    d = decide(cost, RULE15, inflated, "16.50", "0.00", prev={"Cost Price (£)": cost, "Profit %": "40", "Fee %": "16"})
    assert d["profit_override"] is None
    assert d["how"] == "follows" and d["selling_price"] == pricing.price_for_profit(cost, band, rule=RULE15)
    assert d["selling_price"] > 0.95 * inflated                              # only the ~1.8% fee correction


def test_a_zero_profit_cell_never_prices_a_row_at_no_profit_on_the_amazon_tab_either():
    cost = 20.0
    d = decide(cost, RULE15, 21.0, shown(21.0, RULE15), "0", supplier="Amazon", prev={"Fee %": "16"})
    assert d["profit_override"] is None
    assert d["selling_price"] == pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)


def test_a_price_is_only_released_by_the_profit_that_explains_it():
    # a row with a real profit override (20, below the band's 40): a price set at the BAND's profit is not explained
    # by it, so it is not released down to the override's lower price - it stays as it was
    cost = 20.0
    band = band_of(cost, RULE15)
    assert band > 20
    at_band = pricing.price_for_profit(cost, band, platform_fee_percent=16.5)
    kept = decide(cost, RULE15, at_band, "16.50", "20", prev={"Cost Price (£)": cost, "Profit %": "40", "Fee %": "16"})
    assert kept["profit_override"] == 20.0 and kept["misread"] is False
    assert kept["how"] == "kept" and kept["selling_price"] == at_band
    # ...while a price set at the override's own profit follows the corrected fee down (no more than the fee effect)
    at_override = pricing.price_for_profit(cost, 20, platform_fee_percent=16.5)
    moved = decide(cost, RULE15, at_override, "16.50", "20", prev={"Cost Price (£)": cost, "Profit %": "40", "Fee %": "16"})
    assert moved["misread"] is True and moved["how"] == "follows"
    assert moved["selling_price"] == pricing.price_for_profit(cost, 20, rule=RULE15)
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
    assert 'if "Profit %" in col_map and profit_override is None and _band_now is not None:' in text


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
    cost = 20.0
    correct = pricing.price_for_profit(cost, band_of(cost, RULE15), rule=RULE15)
    down = decide(cost, RULE15, correct + 9.0, shown(correct, RULE15), supplier="Amazon", prev={"Fee %": "16"})
    up = decide(cost, RULE15, correct - 5.0, shown(correct, RULE15), supplier="Amazon", prev={"Fee %": "16"})
    assert down["how"] == up["how"] == "amazon" and down["selling_price"] == up["selling_price"] == correct


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
