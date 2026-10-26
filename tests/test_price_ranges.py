"""Price ranges with their own profit % - the mechanism behind the dashboard's "Profit % by price range" (2026-10-26; GTV's GBP 10-20 -> 15% of 2026-09-26 is the
same thing, set in its code). EMPTY on this store by default: every price stays exactly as it was until a range is set (the PROFIT_SCHEDULE setting's
sell_price_overrides, or SELL_PRICE_OVERRIDES). A row whose BASE-schedule price lands inside a range sells at the range's profit; prices set under the cost band's
own profit follow the range down, and a Profit % cell the automation wrote under the band is never read as a manual override."""
import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pricing  # noqa: E402

SRC = ROOT / "generate_xml.py"
NAMES = ("_to_float", "_formula_priced", "_pct_value", "resolve_pct_cell", "_fee_cell_auto_values", "_misread_fee_priced", "_pct_text", "_shown_profit",
         "decide_price")


def _load():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    ns = {"pricing": pricing, "re": re}
    for name in NAMES:
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
    return ns


decide_price = _load()["decide_price"]
ANCHOR = 1 - pricing.effective_rate(pricing.PLATFORM_FEE_PERCENT, legacy=True) / 100.0
NOW = 1 - pricing.effective_rate(pricing.PLATFORM_FEE_PERCENT) / 100.0
COSTS = [0.5, 1, 3, 4.4, 8, 12, 15, 25, 40, 80, 150, 400]
RANGE_PROFIT = 7                        # below every band of every store, so a range always changes the profit


def base_price(total):
    """The price a range is judged on: the base schedule under the fee model of 2026-09-26."""
    return total * (1 + pricing.profit_percent(total) / 100.0) / ANCHOR


def window_around(total, half=1.0, profit=RANGE_PROFIT):
    b = base_price(total)
    return ((round(b - half, 2), round(b + half, 2), profit),)


def decide(cost, existing, profit_cell, supplier="eBay"):
    return decide_price(supplier=supplier, cost_price=cost, shipping_cost=0.0, fee_rule=None, existing_price=existing, fee_cell="",
                        profit_cell=profit_cell, prev={})


def test_no_range_by_default_and_every_function_agrees_with_the_cost_bands():
    assert pricing.SELL_PRICE_OVERRIDES == ()
    for c in COSTS:
        assert pricing.profit_percent_for(c) == pricing.profit_percent(c)
        assert pricing.profit_percent_for(c, rule=pricing.FeeRule("t", 15)) == pricing.profit_percent(c)
        assert pricing.legacy_profit_percents(c, rule=None) == pricing.legacy_profit_percents(c)
        assert pricing.calculate_selling_price(c, 0.0) == round(c * (1 + pricing.profit_percent(c) / 100) / NOW, 2)
    assert pricing.profit_percent_for(0) == pricing.profit_percent(0) and pricing.legacy_profit_percents(0) == []


def test_a_range_puts_the_rows_whose_base_price_lands_inside_it_on_its_profit(monkeypatch):
    c0 = 12.0
    monkeypatch.setattr(pricing, "SELL_PRICE_OVERRIDES", window_around(c0))
    inside = [c for c in COSTS if window_around(c0)[0][0] <= base_price(c) <= window_around(c0)[0][1]]
    assert c0 in inside and len(inside) < len(COSTS)
    for c in COSTS:
        assert pricing.profit_percent_for(c) == (RANGE_PROFIT if c in inside else pricing.profit_percent(c))
    assert pricing.calculate_selling_price(c0, 0.0) == round(c0 * (1 + RANGE_PROFIT / 100) / NOW, 2)
    # the cost band's own value is now a superseded profit: prices written under it are the automation's own
    assert pricing.profit_percent(c0) in pricing.legacy_profit_percents(c0)


def test_both_range_edges_are_inclusive(monkeypatch):
    c0 = 12.0
    b = round(base_price(c0), 2)                    # the anchor price is rounded to the penny, like every price
    monkeypatch.setattr(pricing, "SELL_PRICE_OVERRIDES", ((0.0, b, RANGE_PROFIT),))
    assert pricing.profit_percent_for(c0) == RANGE_PROFIT                         # the upper edge itself is inside
    monkeypatch.setattr(pricing, "SELL_PRICE_OVERRIDES", ((b, 1e9, RANGE_PROFIT),))
    assert pricing.profit_percent_for(c0) == RANGE_PROFIT                         # so is the lower edge
    monkeypatch.setattr(pricing, "SELL_PRICE_OVERRIDES", ((b + 0.01, 1e9, RANGE_PROFIT),))
    assert pricing.profit_percent_for(c0) == pricing.profit_percent(c0)           # a penny above is outside


def test_the_schedule_setting_fills_the_range_and_clearing_it_restores_the_default():
    c0 = 12.0
    lo, hi, pct = window_around(c0)[0]
    raw = json.dumps({"bands": [list(b) for b in pricing.PROFIT_BANDS], "sell_price_overrides": [[lo, hi, pct]]})
    try:
        installed = pricing._apply_schedule_setting(pricing._schedule_setting(raw))
        assert installed and pricing.profit_percent_for(c0) == RANGE_PROFIT
        assert [list(r) for r in pricing.SELL_PRICE_OVERRIDES] == [[lo, hi, pct]]
    finally:
        pricing._apply_schedule_setting(None)
    assert pricing.SELL_PRICE_OVERRIDES == () and pricing.profit_percent_for(c0) == pricing.profit_percent(c0)


def test_a_price_set_under_the_cost_band_follows_the_range_down_and_its_profit_cell_is_not_an_override(monkeypatch):
    c0 = 12.0
    band = pricing.profit_percent(c0)
    old_price = pricing.calculate_selling_price(c0, 0.0)
    before = decide(c0, old_price, f"{band:.2f}")
    assert before["selling_price"] == old_price and before["profit_override"] is None          # no range: nothing moves
    monkeypatch.setattr(pricing, "SELL_PRICE_OVERRIDES", window_around(c0))
    new_price = pricing.calculate_selling_price(c0, 0.0)
    assert new_price < old_price
    after = decide(c0, old_price, f"{band:.2f}")
    assert after["profit_override"] is None, "the cell the automation wrote under the band must not turn into a manual override"
    assert after["how"] == "follows" and after["selling_price"] == new_price and after["band_now"] == RANGE_PROFIT
    amazon = decide(c0, old_price, f"{band:.2f}", supplier="Amazon")
    assert amazon["selling_price"] == new_price and amazon["how"] == "amazon"
    # a price a person raised stays above the formula, and a number nobody's schedule ever used is still a typed override
    raised = decide(c0, round(old_price * 1.4, 2), f"{band:.2f}")
    assert raised["how"] == "kept" and raised["selling_price"] == round(old_price * 1.4, 2)
    typed = decide(c0, old_price, "33")
    assert typed["profit_override"] == 33.0


def test_a_range_never_touches_a_row_outside_it(monkeypatch):
    c0, other = 12.0, 400.0
    monkeypatch.setattr(pricing, "SELL_PRICE_OVERRIDES", window_around(c0))
    assert base_price(other) > window_around(c0)[0][1]
    price = pricing.calculate_selling_price(other, 0.0)
    d = decide(other, price, f"{pricing.profit_percent(other):.2f}")
    assert d["selling_price"] == price and d["how"] == "kept" and d["band_now"] == pricing.profit_percent(other)
