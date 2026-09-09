"""fees.py + the tiered pricing it feeds: the commission tables as
refresh_fees.py writes them, the category lookups, and the fee-as-divisor
algebra for flat, tiered (marginal / step) and minimum-fee cases."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fees  # noqa: E402
import pricing  # noqa: E402


def _tables(tmp_path):
    tiers = tmp_path / "tiers.csv"
    tiers.write_text(
        "Tier ID,Name,Lower %,Upper %,Threshold (£),Min Fee (£)\n"
        "t-elec,Consumer Electronics,7.00,,,0.25\n"
        "t-std,Standard Selling Fee,15.00,,,0.25\n"
        "t-acc,Electronic Accessories,15.00,8.00,100.00,0.25\n"
        "t-jewel,Jewellery,20.00,5.00,225.00,0.25\n", encoding="utf-8")
    cmap = tmp_path / "map.csv"
    cmap.write_text(
        "Category ID,Commission Tier ID,Tier Name\n"
        "3278,t-elec,Consumer Electronics\n"
        "8679,t-elec,Consumer Electronics\n"
        "11628,t-acc,Electronic Accessories\n"
        "999,t-missing,\n", encoding="utf-8")
    cats = tmp_path / "cats.csv"
    cats.write_text(
        "Category ID,OnBuy Category Path\n"
        "3278,Electronics & Technology > TV & Audio > TVs & Accessories > TVs\n"
        "11628,Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitor Privacy Screen\n",
        encoding="utf-8")
    return str(tiers), str(cmap), str(cats)


def test_load_and_lookups(tmp_path):
    table = fees.load(*_tables(tmp_path))
    assert len(table.tiers) == 4
    tv = table.rule_for_category_id(3278)
    assert tv.name == "Consumer Electronics" and tv.lower_pct == 7.0 and not tv.tiered
    acc = table.rule_for_category_path("electronics & technology > computing & gaming > computer monitors & monitor accessories > computer monitor privacy screen")
    assert acc.tiered and acc.threshold == 100.0 and acc.upper_pct == 8.0
    assert table.rule_for_category_id(999) is None          # tier id unknown
    assert table.rule_for_category_id(None) is None
    assert table.rule_for_category_path("no such path") is None
    assert table.standard.lower_pct == 15.0


def test_flat_mode_yields_no_rules(monkeypatch):
    monkeypatch.setattr(fees, "FEE_MODE", "flat")
    monkeypatch.setattr(fees, "_cache", {"loaded": False, "fees": None})
    assert fees.get() is None
    assert fees.rule_for_category_id(3278) is None
    assert fees.enabled() is False


def test_category_mode_loads_the_tables(monkeypatch, tmp_path):
    tiers, cmap, cats = _tables(tmp_path)
    monkeypatch.setattr(fees, "FEE_MODE", "category")
    monkeypatch.setattr(fees, "TIERS_CSV", tiers)
    monkeypatch.setattr(fees, "CATEGORY_TIERS_CSV", cmap)
    monkeypatch.setattr(fees, "CATEGORIES_CSV", cats)
    monkeypatch.setattr(fees, "_cache", {"loaded": False, "fees": None})
    assert fees.enabled() is True
    assert fees.rule_for_category_id("3278").lower_pct == 7.0


def test_category_mode_without_tables_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(fees, "FEE_MODE", "category")
    monkeypatch.setattr(fees, "TIERS_CSV", str(tmp_path / "missing.csv"))
    monkeypatch.setattr(fees, "_cache", {"loaded": False, "fees": None})
    assert fees.get() is None


def test_flat_rule_prices_like_a_divisor():
    elec = pricing.FeeRule("Consumer Electronics", 7)
    # GBP 20 cost -> 40% profit band -> retain 28.00 -> 28 / 0.93
    assert pricing.calculate_selling_price(20.0, fee_rule=elec) == 30.11
    assert pricing.calculate_selling_price(20.0) == 35.0          # flat 20% unchanged
    assert abs(pricing.effective_fee_percent(30.11, elec) - 7.0) < 0.01


def test_tiered_rule_marginal():
    acc = pricing.FeeRule("Electronic Accessories", 15, 8, 100.0, 0.25)
    assert abs(pricing.price_for_retained(50.0, acc, "marginal") - 50 / 0.85) < 0.001
    # above the threshold: P = (200 + 100 x (0.15 - 0.08)) / 0.92 = 225.00,
    # and 15% of 100 + 8% of 125 = 25.00 leaves exactly 200.00
    price = pricing.price_for_retained(200.0, acc, "marginal")
    assert abs(price - 225.0) < 0.001
    assert abs(pricing.fee_amount(price, acc, "marginal") - 25.0) < 0.001


def test_tiered_rule_step_and_the_gap():
    acc = pricing.FeeRule("Electronic Accessories", 15, 8, 100.0, 0.25)
    price = pricing.price_for_retained(200.0, acc, "step")
    assert abs(price - 200 / 0.92) < 0.001
    assert abs(pricing.fee_amount(price, acc, "step") - price * 0.08) < 0.001
    jewel = pricing.FeeRule("Jewellery", 20, 5, 225.0, 0.25)
    # retain 190: 20% needs 237.50 (> 225) but 5% would need 200 (<= 225) -
    # no price fits either rate, so the first price past the threshold wins
    assert abs(pricing.price_for_retained(190.0, jewel, "step") - 225.01) < 0.001


def test_minimum_fee_binds_on_tiny_prices():
    std = pricing.FeeRule("Standard Selling Fee", 15, None, None, 0.25)
    price = pricing.price_for_retained(1.0, std)
    assert abs(price - 1.25) < 0.001
    assert pricing.fee_amount(price, std) == 0.25


def test_price_for_profit_and_buy_box_floor():
    elec = pricing.FeeRule("Consumer Electronics", 7)
    assert pricing.price_for_profit(20.0, 15, elec) == 24.73        # 23 / 0.93
    assert pricing.price_for_profit(20.0, 15) == 28.75              # 23 / 0.80 (flat)
