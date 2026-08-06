"""Pricing bands: every edge pinned. The schedule is user policy
(2026-07-21 bands, rewritten 2026-08-05) - a failing test here means the
policy changed on purpose (update the cases) or a regression (fix it)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pricing  # noqa: E402


BAND_CASES = [
    # (cost + shipping, expected total markup %)
    (0.01, 100), (4.99, 100),          # under 5
    (5.00, 100), (7.50, 100), (10.00, 100),   # 5-10 (raised from 80, 2026-08-05)
    (10.01, 60), (15.00, 60), (30.00, 60),    # over 10 to 30 inclusive
    (30.01, 60), (75.00, 60), (100.00, 60),   # over 30 to 100 (raised from 40)
    (100.01, 50), (250.00, 50), (999.00, 50), # above 100 (raised from 40)
]


def test_band_edges():
    for total_cost, expected in BAND_CASES:
        got = pricing.total_markup_percent(total_cost)
        assert got == expected, f"cost {total_cost}: markup {got}% != {expected}%"


PRICE_CASES = [
    # (kwargs, expected selling price, label)
    (dict(cost_price=8.0), 16.0, "GBP 8 -> x2.00"),
    (dict(cost_price=20.0), 32.0, "GBP 20 -> x1.60"),
    (dict(cost_price=60.0), 96.0, "GBP 60 -> x1.60"),
    (dict(cost_price=28.0, shipping_cost=4.0), 51.2, "28+4 ship = 32 total -> x1.60"),
    (dict(cost_price=150.0), 225.0, "GBP 150 -> x1.50"),
    (dict(cost_price=95.0, shipping_cost=10.0), 157.5, "95+10 = 105 total -> x1.50"),
    (dict(cost_price=9.0, shipping_cost=0.5), 19.0, "9.50 total -> 100% band"),
]


def test_selling_prices():
    for kwargs, expected, label in PRICE_CASES:
        got = pricing.calculate_selling_price(**kwargs)
        assert abs(got - expected) < 0.001, f"{label}: got {got}, expected {expected}"


def test_extra_fee_stacks_on_top_of_band():
    # A category fee above the standard 20% must not eat the band's profit.
    assert pricing.calculate_selling_price(20.0, platform_fee_percent=25) == 33.0  # 60 + 5 -> x1.65


def test_zero_and_negative_cost():
    assert pricing.calculate_selling_price(0) == 0.0
    assert pricing.calculate_selling_price(-5) == 0.0
