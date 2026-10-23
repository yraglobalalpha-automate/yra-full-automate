"""even_margins.py as ported from Makstore (2026-10-23, for the dashboard's pricing card): the plan on THIS store's own schedule, pinning no
number - every expectation is computed from pricing.py as it stands."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import even_margins as em  # noqa: E402
import pricing  # noqa: E402
import pricing_probe  # noqa: E402


def cells_of(**over):
    base = {"SKU": "111", "Title": "A product", "Cost Price (£)": "30", "Shipping Cost (£)": "", "Selling Price (£)": "0", "Fee %": "", "Profit %": "",
            "Category": "", "Stock": "5", "Sync Status": "Synced", "Price Check Flag": "Normal"}
    base.update(over)
    return lambda k: str(base.get(k, ""))


def decide(amazon=False, mirror=None, **over):
    return pricing_probe.decision(cells_of(**over), mirror or {}, amazon)


def schedule_price(cost=30.0):
    return pricing.calculate_selling_price(cost, 0.0, platform_fee_percent=pricing.PLATFORM_FEE_PERCENT)


@pytest.mark.parametrize("cost", [3.0, 7.0, 30.0, 80.0])
def test_a_price_on_the_schedule_is_left_alone(cost):
    assert em.plan_row(decide(**{"Cost Price (£)": str(cost), "Selling Price (£)": f"{schedule_price(cost):.2f}"}))[0] == "ok"
    assert em.plan_row(decide(**{"Cost Price (£)": str(cost), "Selling Price (£)": f"{schedule_price(cost) + 0.01:.2f}"}))[0] == "ok"


def test_a_price_below_the_schedule_is_raised_and_a_legacy_high_one_lowered():
    sched = schedule_price(30)
    action, note = em.plan_row(decide(**{"Selling Price (£)": f"{sched * 0.9:.2f}"}))
    assert action == "raise" and note.startswith("x0.9")
    d = decide(mirror={"Profit %": "74"}, **{"Selling Price (£)": f"{sched * 1.4:.2f}", "Profit %": "74.06"})
    action, note = em.plan_row(d)
    assert action == "lower" and note.startswith("x1.4")
    assert em.new_cells(d, 5)["Selling Price (£)"] == round(d["formula_price"], 2) == sched


def test_typed_overrides_and_frozen_rows_are_never_touched():
    d = decide(**{"Selling Price (£)": f"{schedule_price(30) * 1.5:.2f}", "Profit %": "150"})
    assert d["profit_override"] == 150 and em.plan_row(d) == ("skip", "typed Profit % / Fee % override")
    d = decide(**{"Selling Price (£)": f"{schedule_price(30) * 1.5:.2f}", "Fee %": "12"})
    assert d["fee_override"] == 12 and em.plan_row(d)[0] == "skip"
    d = decide(**{"Selling Price (£)": f"{schedule_price(30) * 1.4:.2f}"})
    assert em.plan_row(d, frozen=True) == ("skip", "frozen wrong-content listing") and em.plan_row(d)[0] == "lower"


def test_an_extreme_outlier_is_held_not_lowered():
    d = decide(**{"Selling Price (£)": f"{schedule_price(30) * 3:.2f}"})
    action, note = em.plan_row(d)
    assert action == "hold" and "review" in note
    assert em.plan_row(d, outlier_ratio=4.0)[0] == "lower"
    assert em.plan_row(decide(**{"Selling Price (£)": f"{schedule_price(30) * 1.99:.2f}"}))[0] == "lower"


def test_a_row_without_a_cost_or_price_is_skipped():
    assert em.plan_row(None) == ("skip", "no cost")
    assert em.plan_row(decide(**{"Selling Price (£)": "0"}))[0] == "skip"


def test_the_band_boundaries_pick_this_stores_scheduled_profit():
    for cost in (3, 7, 30, 35, 80):
        d = decide(**{"Cost Price (£)": str(cost), "Selling Price (£)": "9999"})
        band = pricing.profit_percent_for(cost) if hasattr(pricing, "profit_percent_for") else pricing.profit_percent(cost)
        assert d["band_now"] == band
        assert em.new_cells(d, 5)["Profit %"] == f"{band:.2f}"


def test_the_tool_never_prints_a_cost(capsys):
    d = decide(**{"Cost Price (£)": "12.34", "Selling Price (£)": f"{schedule_price(12.34) * 1.4:.2f}"})
    em.plan_row(d)
    assert "12.34" not in capsys.readouterr().out
