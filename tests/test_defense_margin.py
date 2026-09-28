"""Buy Box floor: profit is DEFENSE_PROFIT_PERCENT of the SELLING price
after commission (user 2026-09-28), not of cost."""
import pricing


def test_flat_rate_margin_of_price():
    # base / (1 - 0.215 - 0.15)
    p = pricing.price_for_margin_of_price(18.44, 15)
    assert abs(p - 18.44 / 0.635) < 0.001
    assert abs((p - pricing.fee_amount(p) - 18.44) / p - 0.15) < 0.001


def test_category_rule_margin_of_price():
    rule = pricing.FeeRule("Most categories", 15)   # 16.5% effective
    p = pricing.price_for_margin_of_price(18.44, 15, rule)
    assert abs(p - 18.44 / 0.685) < 0.001            # ~26.92, the incident SKU
    assert abs((p - pricing.fee_amount(p, rule) - 18.44) / p - 0.15) < 0.001


def test_min_fee_binds_for_cheap_items():
    rule = pricing.FeeRule("Cheap", 15, min_fee=0.25)
    p = pricing.price_for_margin_of_price(0.50, 15, rule)
    # the percentage fee at the naive price is under 25p - the minimum binds
    assert abs(p - (0.50 + 0.25) / 0.85) < 0.001
    assert (p - pricing.fee_amount(p, rule) - 0.50) / p >= 0.1499


def test_impossible_margin_returns_none():
    rule = pricing.FeeRule("Jewellery", 90)
    assert pricing.price_for_margin_of_price(10.0, 15, rule) is None
