"""Pricing ranges: every edge pinned. The schedule is user policy
(2026-09-11 rewrite: plain profit percentages 100/80/40/20, the older
total-markup notation retired) - a failing test here means the policy
changed on purpose (update the cases) or a regression (fix it)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pricing  # noqa: E402


PROFIT_CASES = [
    # (cost + shipping, expected PROFIT %)
    (0.01, 100), (4.99, 100),                    # under 5
    (5.00, 80), (7.50, 80), (10.00, 80),         # 5-10 inclusive
    (10.01, 40), (30.00, 40), (50.00, 40),       # over 10 to 50 inclusive
    (50.01, 20), (100.00, 20), (150.00, 20), (999.00, 20),  # above 50
]


def test_profit_range_edges():
    for total_cost, expected in PROFIT_CASES:
        got = pricing.profit_percent(total_cost)
        assert got == expected, f"cost {total_cost}: profit {got}% != {expected}%"


PRICE_CASES = [
    # (kwargs, expected selling price at the flat 20% fallback fee, label)
    (dict(cost_price=3.0), 7.50, "GBP 3 -> 100% profit / 0.8 = x2.5"),
    (dict(cost_price=8.0), 18.0, "GBP 8 -> 80% profit / 0.8 = x2.25"),
    (dict(cost_price=20.0), 35.0, "GBP 20 -> 40% profit / 0.8 = x1.75"),
    (dict(cost_price=28.0, shipping_cost=4.0), 56.0, "28+4 ship = 32 total -> x1.75"),
    (dict(cost_price=45.0, shipping_cost=5.0), 87.50, "45+5 = 50 total -> still the 40% range"),
    (dict(cost_price=60.0), 90.0, "GBP 60 -> 20% profit / 0.8 = x1.5"),
    (dict(cost_price=150.0), 225.0, "GBP 150 -> 20% profit / 0.8 = x1.5"),
    (dict(cost_price=95.0, shipping_cost=10.0), 157.50, "95+10 = 105 total -> x1.5"),
    (dict(cost_price=9.0, shipping_cost=0.5), 21.38, "9.50 total -> 80% profit range"),
]


def test_selling_prices():
    for kwargs, expected, label in PRICE_CASES:
        got = pricing.calculate_selling_price(**kwargs)
        assert abs(got - expected) < 0.001, f"{label}: got {got}, expected {expected}"


def test_fee_comes_out_of_the_selling_price():
    """After OnBuy takes its cut of the SELLING price, what is retained
    must be exactly cost x (1 + profit%) - the fee never eats the profit
    and never stacks on top of it."""
    for base in (3.0, 8.0, 20.0, 60.0, 150.0, 400.0):
        sell = pricing.calculate_selling_price(base)
        retained = sell * (1 - pricing.PLATFORM_FEE_PERCENT / 100)
        expected = base * (1 + pricing.profit_percent(base) / 100)
        assert abs(retained - expected) < 0.02, (
            f"cost {base}: retained {retained:.2f} != {expected:.2f}")


def test_category_fee_keeps_the_same_profit():
    # A 7% category widens the price less than the flat 20% would, but the
    # retained amount is identical: cost x (1 + profit).
    rule = pricing.FeeRule("Consumer Electronics", 7)
    price = pricing.calculate_selling_price(150.0, fee_rule=rule)
    assert price == round(150 * 1.20 / 0.93, 2)          # 193.55
    assert abs(price * 0.93 - 150 * 1.20) < 0.02


def test_higher_category_fee_still_pays_the_range_profit():
    # A 25% commission widens the divisor instead of eating the margin:
    # 20 x 1.40 / 0.75 = 37.33, and 37.33 x 0.75 = 28.00 = 20 x 1.40.
    price = pricing.calculate_selling_price(20.0, platform_fee_percent=25)
    assert price == 37.33
    assert abs(price * 0.75 - 28.0) < 0.01


def test_legacy_profit_percents_expose_only_changed_ranges():
    # The 2026-09-11 rewrite: GBP 50-100 dropped 40 -> 20, above 100
    # 30/25 -> 20, under 5 rose to 100. Old DOWNWARD values must stay
    # recognisable so existing prices reprice down.
    assert pricing.legacy_profit_percents(150.0) == [30, 25]
    assert pricing.legacy_profit_percents(60.0) == [40]
    assert pricing.legacy_profit_percents(100.0) == [40]
    assert pricing.legacy_profit_percents(3.0) == [80]
    # Ranges that kept their value offer no legacy - and never the current one.
    assert pricing.legacy_profit_percents(7.5) == []
    assert pricing.legacy_profit_percents(20.0) == []
    assert pricing.legacy_profit_percents(0) == []


def test_absurd_fee_is_clamped_not_divided_by_zero():
    assert pricing.calculate_selling_price(20.0, platform_fee_percent=100) > 0
    assert pricing.calculate_selling_price(20.0, platform_fee_percent=250) > 0


def test_zero_and_negative_cost():
    assert pricing.calculate_selling_price(0) == 0.0
    assert pricing.calculate_selling_price(-5) == 0.0
