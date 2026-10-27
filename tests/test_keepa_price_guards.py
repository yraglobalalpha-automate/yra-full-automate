"""Keepa price guards (2026-10-27, Arden 0000069168437 -Messam / B0FB2L7CCX): Amazon's own unbuyable offer is not a third-party source, and a price far
below the product's usual level is held. The first test is the real shape of the ASIN that took an order at GBP 19.73 (prices in pence)."""
import keepa_client as kc


def arr(amazon=-1, new=-1):
    a = [-1] * 20
    a[kc.IDX_AMAZON], a[kc.IDX_NEW] = amazon, new
    return a


def product(amazon=-1, new=-1, avail=-1, avg30=None, avg90=None, fba=-1, fbm=-1):
    current = arr(amazon, new)
    current[kc.IDX_NEW_FBA], current[kc.IDX_NEW_FBM_SHIPPING] = fba, fbm
    stats = {"current": current}
    if avg30 is not None:
        stats["avg30"] = avg30
    if avg90 is not None:
        stats["avg90"] = avg90
    return {"asin": "B0TEST0001", "productType": 0, "availabilityAmazon": avail, "stats": stats}


def test_the_asin_that_took_the_order_is_unavailable_not_a_third_party_source():
    p = product(amazon=998, new=998, avail=4, avg30=arr(amazon=998, new=3170), avg90=arr(amazon=998, new=3280))
    price, seller, availability, reason = kc.choose_offer(p, use_buybox=False, own_only=False)
    assert price == -1 and seller == "" and reason == "amazon-unbuyable" and availability == "Delayed (Amazon's own offer only)"
    available, data = kc.normalize_product(p)
    assert available is False and data["amazon_availability"] == "Delayed (Amazon's own offer only)" and data["stock"] == 0


def test_every_amazon_status_that_is_not_in_stock_is_treated_the_same():
    for avail in (1, 2, 3, 4, -1):
        p = product(amazon=998, new=998, avail=avail)
        assert kc.choose_offer(p, use_buybox=False, own_only=False)[3] == "amazon-unbuyable", avail


def test_a_third_party_cheaper_than_amazons_unbuyable_offer_is_still_a_source():
    p = product(amazon=2000, new=1500, avail=4)
    assert kc.choose_offer(p, use_buybox=False, own_only=False) == (1500, "3rd party (lowest New)", "In stock (3rd party)", "new")


def test_a_third_party_price_type_is_used_when_the_lowest_new_is_amazons_own_offer():
    assert kc.choose_offer(product(amazon=998, new=998, avail=4, fba=2500), use_buybox=False, own_only=False)[:2] == (2500, "3rd party (lowest New)")
    assert kc.choose_offer(product(amazon=998, new=998, avail=4, fba=2500, fbm=2300), use_buybox=False, own_only=False)[0] == 2300     # shipping included
    assert kc.choose_offer(product(amazon=998, new=998, avail=4, fbm=2300), use_buybox=False, own_only=True)[3] == "third-party-only"


def test_amazon_in_stock_and_plain_third_party_rows_are_untouched():
    assert kc.choose_offer(product(amazon=4599, new=3299, avail=0), use_buybox=False, own_only=False)[:2] == (4599, "Amazon")
    assert kc.choose_offer(product(new=3299, avail=-1), use_buybox=False, own_only=False)[3] == "new"
    assert kc.choose_offer(product(amazon=-1, new=-1, avail=-1), use_buybox=False, own_only=False)[3] == "unavailable"


def test_a_price_far_below_the_usual_level_is_held_with_the_percentage_in_the_sheet_text():
    p = product(new=1000, avg30=arr(new=3000), avg90=arr(new=3200))
    before = kc.DROP_HELD
    assert kc.drop_held(p, 1000, "new") == "Price held: now 33% of its usual level" and kc.DROP_HELD == before + 1
    available, data = kc.normalize_product(p)
    assert available is False and data["amazon_availability"].startswith("Price held: now 33%")


def test_the_line_is_exactly_forty_percent_of_the_lower_average():
    p = product(new=1200, avg30=arr(new=3000), avg90=arr(new=3400))
    assert kc.drop_held(p, 1200, "new") == ""                       # 40.0% of 3000: kept
    assert kc.drop_held(p, 1199, "new").startswith("Price held")


def test_an_ordinary_discount_is_kept_and_still_taken_at_the_usual_level():
    p = product(new=2400, avg30=arr(new=3000), avg90=arr(new=3200))
    assert kc.drop_held(p, 2400, "new") == ""
    assert kc.regular_price(p, 2400, "new") == 3000                 # the earlier rule: the usual level, here within +35% of the current price


def test_amazons_own_in_stock_offer_is_held_too_when_it_collapses():
    p = product(amazon=500, new=500, avail=0, avg90=arr(amazon=2000, new=2000))
    assert kc.drop_held(p, 500, "amazon").startswith("Price held: now 25%")


def test_no_history_means_no_hold_and_the_switch_turns_the_guard_off(monkeypatch):
    assert kc.drop_held(product(new=100), 100, "new") == ""
    assert kc.drop_held(product(new=100, avg30=arr(new=-1), avg90=arr()), 100, "new") == ""
    p = product(new=1000, avg30=arr(new=3000), avg90=arr(new=3200))
    monkeypatch.setattr(kc, "DROP_HOLD_PERCENT", 0)
    assert kc.drop_held(p, 1000, "new") == ""
    monkeypatch.setattr(kc, "DROP_HOLD_PERCENT", 50)
    assert kc.drop_held(product(new=1400, avg30=arr(new=3000), avg90=arr(new=3200)), 1400, "new").startswith("Price held")


def test_a_buy_box_price_is_judged_against_the_new_averages_and_a_dead_product_is_not():
    assert kc.drop_held(product(new=100, avg30=arr(new=3000)), 100, "buybox").startswith("Price held: now 3%")           # an error price that won the Buy Box is no source either
    assert kc.drop_held(product(new=100, avg30=arr(new=3000)), 3900, "buybox") == ""
    assert kc.drop_held(product(new=100, avg30=arr(new=3000)), -1, "dead") == ""


def test_the_default_line_is_forty_percent():
    assert kc.DROP_HOLD_PERCENT == 40.0
