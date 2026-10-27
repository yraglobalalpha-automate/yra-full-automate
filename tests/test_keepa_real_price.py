"""The real-price lookup (2026-10-27): the products guard 1 / guard 2 refuse get a second Keepa request with the Buy Box detail, and are priced from the offer a customer really buys.
The first test is the real shape of B0FB2L7CCX (prices in pence): Amazon's phantom offer at ~GBP 10 'Delayed', the 30/90-day New average ~GBP 32, one Prime FBA seller holding the Buy Box at ~GBP 40."""
import json

import pytest

import keepa_client as kc


def arr(amazon=-1, new=-1):
    a = [-1] * 20
    a[kc.IDX_AMAZON], a[kc.IDX_NEW] = amazon, new
    return a


def phantom(asin="B0PHANTOM01", **stats_over):
    stats = {"current": arr(999, 999), "avg30": arr(999, 3168), "avg90": arr(999, 3276)}
    stats.update(stats_over)
    return {"asin": asin, "productType": 0, "availabilityAmazon": 4, "stats": stats}


BUY_BOX = dict(buyBoxPrice=3999, buyBoxShipping=0, buyBoxIsShippable=True, buyBoxIsAmazon=False, buyBoxIsFBA=True, buyBoxIsUsed=False, buyBoxIsWarehouseDeal=False, buyBoxIsPreorder=False,
               buyBoxIsBackorder=False, buyBoxIsUnqualified=False, buyBoxAvailabilityMessage="IN_STOCK", buyBoxCondition=1)


def ordinary(asin="B0ORDINARY1"):
    return {"asin": asin, "productType": 0, "availabilityAmazon": -1, "stats": {"current": arr(-1, 3299), "avg30": arr(-1, 3300), "avg90": arr(-1, 3400)}}


class FakeKeepa:
    """Replaces KeepaClient._request: answers each ASIN from `first` (no Buy Box in the request) or `second` (buybox=1) and records what was asked."""

    def __init__(self, first, second=None, fail_second=None):
        self.first, self.second, self.fail_second, self.calls = first, second or {}, fail_second, []

    def __call__(self, client, endpoint, params):
        asins = params["asin"].split(",")
        self.calls.append({"asins": asins, "buybox": bool(params.get("buybox"))})
        if params.get("buybox") and self.fail_second:
            raise self.fail_second
        source = self.second if params.get("buybox") else self.first
        return {"tokensLeft": 500, "refillRate": 20, "tokensConsumed": len(asins) * (3 if params.get("buybox") else 1), "products": [json.loads(json.dumps(source[a])) for a in asins if a in source]}


@pytest.fixture
def keepa(monkeypatch):
    monkeypatch.setattr(kc, "validate_images", lambda urls, max_images=10: list(urls)[:max_images])
    monkeypatch.setattr(kc, "LOOKUP_REAL_PRICE", True)
    monkeypatch.setattr(kc, "LOOKUP_MAX", 100)
    monkeypatch.setattr(kc, "UPDATE_HOURS", "")
    monkeypatch.setattr(kc.time, "sleep", lambda seconds: None)

    def install(first, second=None, fail_second=None):
        fake = FakeKeepa(first, second, fail_second)
        monkeypatch.setattr(kc.KeepaClient, "_request", lambda client, endpoint, params: fake(client, endpoint, params))      # a plain function binds as a method, a callable object would not
        return kc.KeepaClient("k", use_buybox=False), fake
    return install


def run(client, asins):
    """What the prefetch / the probe do: the first request, then the real-price lookup merged over it."""
    products = client.fetch_products(asins)
    products.update(client.real_price_pass(products))
    return products


def test_the_phantom_offer_gets_a_second_look_with_the_buy_box_and_is_priced_from_the_real_offer(keepa):
    client, fake = keepa({"B0PHANTOM01": phantom()}, {"B0PHANTOM01": phantom(**BUY_BOX)})
    used = kc.REAL_PRICE_USED
    products = run(client, ["B0PHANTOM01"])
    assert fake.calls == [{"asins": ["B0PHANTOM01"], "buybox": False}, {"asins": ["B0PHANTOM01"], "buybox": True}]
    assert products["B0PHANTOM01"]["_real_price"] is True
    assert kc.choose_offer(products["B0PHANTOM01"]) == (3999, "3rd party (Buy Box)", "In stock (Buy Box)", "buybox")
    available, data = kc.normalize_product(products["B0PHANTOM01"])
    assert available is True and data["price"] == 39.99 and data["amazon_seller"] == "3rd party (Buy Box)" and data["stock"] == kc.DEFAULT_STOCK
    assert kc.REAL_PRICE_USED == used + 1


def test_the_shipping_of_the_buy_box_is_part_of_the_price(keepa):
    client, _fake = keepa({"B0PHANTOM01": phantom()}, {"B0PHANTOM01": phantom(**dict(BUY_BOX, buyBoxPrice=3500, buyBoxShipping=499))})
    assert kc.choose_offer(run(client, ["B0PHANTOM01"])["B0PHANTOM01"])[0] == 3999


def test_without_a_flagged_product_there_is_one_request_and_no_extra_tokens(keepa):
    client, fake = keepa({"B0ORDINARY1": ordinary()})
    products = run(client, ["B0ORDINARY1"])
    assert len(fake.calls) == 1 and "_real_price" not in products["B0ORDINARY1"]


def test_only_the_flagged_products_are_asked_again(keepa):
    client, fake = keepa({"B0PHANTOM01": phantom(), "B0ORDINARY1": ordinary(), "B0PHANTOM02": phantom("B0PHANTOM02")},
                         {"B0PHANTOM01": phantom(**BUY_BOX), "B0PHANTOM02": phantom("B0PHANTOM02", **BUY_BOX)})
    run(client, ["B0PHANTOM01", "B0ORDINARY1", "B0PHANTOM02"])
    assert fake.calls[1] == {"asins": ["B0PHANTOM01", "B0PHANTOM02"], "buybox": True}


def test_a_deep_price_drop_is_looked_up_too_and_priced_from_the_buy_box(keepa):
    low = {"asin": "B0DEEPDROP1", "productType": 0, "availabilityAmazon": -1, "stats": {"current": arr(-1, 1000), "avg30": arr(-1, 3000), "avg90": arr(-1, 3200)}}
    client, fake = keepa({"B0DEEPDROP1": low}, {"B0DEEPDROP1": dict(low, stats=dict(low["stats"], **dict(BUY_BOX, buyBoxPrice=2900)))})
    products = run(client, ["B0DEEPDROP1"])
    assert len(fake.calls) == 2 and kc.normalize_product(products["B0DEEPDROP1"])[1]["price"] == 29.0


def test_a_buy_box_that_is_itself_an_error_price_is_still_held(keepa):
    client, _fake = keepa({"B0PHANTOM01": phantom()}, {"B0PHANTOM01": phantom(**dict(BUY_BOX, buyBoxPrice=900))})
    available, data = kc.normalize_product(run(client, ["B0PHANTOM01"])["B0PHANTOM01"])
    assert available is False and data["amazon_availability"].startswith("Price held")


@pytest.mark.parametrize("bad", [dict(buyBoxIsUsed=True), dict(buyBoxIsWarehouseDeal=True), dict(buyBoxIsPreorder=True), dict(buyBoxIsBackorder=True), dict(buyBoxIsUnqualified=True),
                                 dict(buyBoxIsShippable=False), dict(buyBoxAvailabilityMessage="BACKORDER_NO_ETA"), dict(buyBoxPrice=-2)])
def test_a_buy_box_that_cannot_be_bought_leaves_the_row_held(keepa, bad):
    client, _fake = keepa({"B0PHANTOM01": phantom()}, {"B0PHANTOM01": phantom(**dict(BUY_BOX, **bad))})
    available, data = kc.normalize_product(run(client, ["B0PHANTOM01"])["B0PHANTOM01"])
    assert available is False and data["amazon_availability"] == "Delayed (Amazon's own offer only)"


def test_a_failed_second_request_keeps_the_first_answers_and_never_raises(keepa):
    client, fake = keepa({"B0PHANTOM01": phantom()}, fail_second=kc.TransientError("keepa product: out of tokens"))
    products = run(client, ["B0PHANTOM01"])
    assert len(fake.calls) == 2 and "_real_price" not in products["B0PHANTOM01"]
    assert kc.normalize_product(products["B0PHANTOM01"])[0] is False                 # fail closed: still held


def test_the_cap_limits_the_products_looked_up_and_the_rest_stay_held(keepa, monkeypatch):
    monkeypatch.setattr(kc, "LOOKUP_MAX", 2)
    ids = [f"B0PHANTOM{n:02d}" for n in range(3)]
    client, fake = keepa({a: phantom(a) for a in ids}, {a: phantom(a, **BUY_BOX) for a in ids})
    products = run(client, ids)
    assert fake.calls[1]["asins"] == ids[:2]
    assert kc.normalize_product(products[ids[0]])[0] is True and kc.normalize_product(products[ids[2]])[0] is False


def test_the_switch_turns_the_second_look_off(keepa, monkeypatch):
    monkeypatch.setattr(kc, "LOOKUP_REAL_PRICE", False)
    client, fake = keepa({"B0PHANTOM01": phantom()}, {"B0PHANTOM01": phantom(**BUY_BOX)})
    run(client, ["B0PHANTOM01"])
    assert len(fake.calls) == 1


def test_a_client_that_already_asks_for_the_buy_box_needs_no_second_look(monkeypatch):
    monkeypatch.setattr(kc.KeepaClient, "_request", lambda self, endpoint, params: pytest.fail("no request expected"))
    assert kc.KeepaClient("k", use_buybox=True).real_price_pass({"B0PHANTOM01": phantom()}) == {}


def test_what_needs_a_second_look():
    assert kc.needs_real_price(phantom()) is True
    assert kc.needs_real_price(ordinary()) is False
    assert kc.needs_real_price({"asin": "B0INSTOCK01", "productType": 0, "availabilityAmazon": 0, "stats": {"current": arr(4599, 3299)}}) is False
    assert kc.needs_real_price(dict(phantom(), _real_price=True)) is False                              # looked at once, never twice
    assert kc.needs_real_price({"asin": "B0DEAD00001", "productType": 4, "stats": {}}) is False


def test_the_marker_survives_the_cache_and_the_explicit_switch_still_wins():
    looked = json.loads(json.dumps(dict(phantom(**BUY_BOX), _real_price=True)))                          # the Supabase cache stores JSON
    assert kc.choose_offer(looked)[3] == "buybox"
    assert kc.choose_offer(looked, use_buybox=False)[3] == "amazon-unbuyable"


def test_the_default_cap_is_one_hundred_and_the_lookup_is_on():
    assert kc.LOOKUP_MAX == 100 and kc.LOOKUP_REAL_PRICE is True
