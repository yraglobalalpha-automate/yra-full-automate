"""Pricing bands: every edge pinned. The schedule is user policy
(2026-07-21 bands, rewritten 2026-08-05, fee moved onto the selling price
2026-09-01) - a failing test here means the policy changed on purpose
(update the cases) or a regression (fix it)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pricing  # noqa: E402


BAND_CASES = [
    # (cost + shipping, expected TOTAL markup % - profit + standard fee)
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


def test_profit_percent_strips_the_fee():
    # The band stores the historical TOTAL; profit is that minus the 20% fee.
    assert pricing.profit_percent(3.0) == 80
    assert pricing.profit_percent(20.0) == 40
    assert pricing.profit_percent(150.0) == 30


PRICE_CASES = [
    # (kwargs, expected selling price, label) - fee is a divisor, not a markup
    (dict(cost_price=8.0), 18.0, "GBP 8 -> 80% profit / 0.8 = x2.25"),
    (dict(cost_price=20.0), 35.0, "GBP 20 -> 40% profit / 0.8 = x1.75"),
    (dict(cost_price=60.0), 105.0, "GBP 60 -> x1.75"),
    (dict(cost_price=28.0, shipping_cost=4.0), 56.0, "28+4 ship = 32 total -> x1.75"),
    (dict(cost_price=150.0), 243.75, "GBP 150 -> 30% profit / 0.8 = x1.625"),
    (dict(cost_price=95.0, shipping_cost=10.0), 170.62, "95+10 = 105 total -> x1.625"),
    (dict(cost_price=9.0, shipping_cost=0.5), 21.38, "9.50 total -> 80% profit band"),
]


def test_selling_prices():
    for kwargs, expected, label in PRICE_CASES:
        got = pricing.calculate_selling_price(**kwargs)
        assert abs(got - expected) < 0.001, f"{label}: got {got}, expected {expected}"


def test_fee_comes_out_of_the_selling_price():
    """The point of the 2026-09-01 change: after OnBuy takes its cut of the
    SELLING price, what is retained must be exactly cost x (1 + profit%).
    The old cost-side markup left this short (GBP 20 -> 28% instead of 40%)."""
    for base in (3.0, 8.0, 20.0, 60.0, 150.0, 400.0):
        sell = pricing.calculate_selling_price(base)
        retained = sell * (1 - pricing.PLATFORM_FEE_PERCENT / 100)
        expected = base * (1 + pricing.profit_percent(base) / 100)
        assert abs(retained - expected) < 0.02, (
            f"cost {base}: retained {retained:.2f} != {expected:.2f}")


def test_higher_category_fee_still_pays_the_band_profit():
    # A 25% commission widens the divisor instead of eating the margin:
    # 20 x 1.40 / 0.75 = 37.33, and 37.33 x 0.75 = 28.00 = 20 x 1.40.
    price = pricing.calculate_selling_price(20.0, platform_fee_percent=25)
    assert price == 37.33
    assert abs(price * 0.75 - 28.0) < 0.01


def test_absurd_fee_is_clamped_not_divided_by_zero():
    assert pricing.calculate_selling_price(20.0, platform_fee_percent=100) > 0
    assert pricing.calculate_selling_price(20.0, platform_fee_percent=250) > 0


def test_zero_and_negative_cost():
    assert pricing.calculate_selling_price(0) == 0.0
    assert pricing.calculate_selling_price(-5) == 0.0
