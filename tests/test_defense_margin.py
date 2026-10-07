"""Buy Box floor: profit is DEFENSE_PROFIT_PERCENT of the SELLING price
after commission (user 2026-09-28), not of cost."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pricing


def test_flat_rate_margin_of_price():
    # base / (1 - 0.24 - 0.15): the flat 20% commission plus the 20% VAT OnBuy adds to it
    p = pricing.price_for_margin_of_price(18.44, 15)
    assert abs(p - 18.44 / 0.61) < 0.001
    assert abs((p - pricing.fee_amount(p) - 18.44) / p - 0.15) < 0.001


def test_category_rule_margin_of_price():
    rule = pricing.FeeRule("Most categories", 15)   # 18% effective: 15% + 20% VAT on it
    p = pricing.price_for_margin_of_price(18.44, 15, rule)
    assert abs(p - 18.44 / 0.67) < 0.001             # ~27.52 (the incident SKU, 26.92 before the VAT was counted)
    assert abs((p - pricing.fee_amount(p, rule) - 18.44) / p - 0.15) < 0.001


def test_min_fee_binds_for_cheap_items():
    rule = pricing.FeeRule("Cheap", 15, min_fee=0.25)
    p = pricing.price_for_margin_of_price(0.50, 15, rule)
    # the percentage fee at the naive price is under the minimum (25p + VAT = 30p) - the minimum binds
    assert abs(p - (0.50 + 0.30) / 0.85) < 0.001
    assert (p - pricing.fee_amount(p, rule) - 0.50) / p >= 0.1499


def test_impossible_margin_returns_none():
    rule = pricing.FeeRule("Jewellery", 90)
    assert pricing.price_for_margin_of_price(10.0, 15, rule) is None
