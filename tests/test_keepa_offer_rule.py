"""AMAZON_OWN_OFFER_ONLY (2026-10-24): a product Amazon itself no longer sells must read as out of stock even when Keepa lists a
third-party offer (Arden 0000049897678-Messam took three orders that way). Off, the old rule stands."""
import keepa_client as kc


def product(amazon=-1, new=-1, avail=-1, **stats):
    current = [-1] * 20
    current[kc.IDX_AMAZON], current[kc.IDX_NEW] = amazon, new
    return {"productType": 0, "availabilityAmazon": avail, "stats": dict(current=current, **stats)}


def test_third_party_only_product_is_out_of_stock_under_the_own_offer_rule():
    p = product(new=3299, avail=-1)
    assert kc.choose_offer(p, use_buybox=False, own_only=False)[3] == "new"                     # the old rule: a source
    price, seller, availability, reason = kc.choose_offer(p, use_buybox=False, own_only=True)
    assert price == -1 and seller == "" and reason == "third-party-only"
    assert availability == "Third-party sellers only (no Amazon offer)"


def test_amazons_own_offer_still_counts_and_a_dead_product_stays_dead():
    p = product(amazon=4599, new=3299, avail=0)
    assert kc.choose_offer(p, use_buybox=False, own_only=True)[:2] == (4599, "Amazon")
    nothing = product()
    assert kc.choose_offer(nothing, use_buybox=False, own_only=True)[3] == "unavailable"
    assert kc.choose_offer(nothing, use_buybox=False, own_only=True)[2] == "No Amazon offer"


def test_buy_box_counts_only_when_amazon_holds_it():
    stats = dict(buyBoxPrice=3999, buyBoxIsShippable=True, buyBoxIsAmazon=False, buyBoxAvailabilityMessage="IN_STOCK")
    third = product(new=3999, avail=-1, **stats)
    assert kc.choose_offer(third, use_buybox=True, own_only=False)[3] == "buybox"
    assert kc.choose_offer(third, use_buybox=True, own_only=True)[3] == "third-party-only"
    stats["buyBoxIsAmazon"] = True
    own = product(new=3999, avail=-1, **stats)
    assert kc.choose_offer(own, use_buybox=True, own_only=True)[:2] == (3999, "Amazon")


def test_the_env_switch_is_off_unless_set():
    assert kc.OWN_OFFER_ONLY is False
    p = product(new=3299, avail=-1)
    assert kc.choose_offer(p, use_buybox=False)[3] == "new"
