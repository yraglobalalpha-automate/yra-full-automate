"""keepa_client: the offer decision (what we would buy, at what price) and
the shape handed to the row loop, pinned against Keepa product objects as
documented at keepa.com/api-docs (product-object, statistics-object)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keepa_client as kc  # noqa: E402


def _product(**over):
    base = {
        "asin": "B0F3GWXLTS",
        "productType": 0,
        "title": "Example Wireless Speaker \U0001F525 (Black)",
        "brand": "ExampleBrand",
        "manufacturer": "ExampleBrand Inc.",
        "availabilityAmazon": 0,
        "lastUpdate": 7663880,
        "eanList": ["5012345678900"],
        "upcList": ["012345678905"],
        "images": [{"l": "61ni3t6ryLL.jpg", "m": "71fP4WKKvNL.jpg"}, {"l": "71abc.jpg"}],
        "features": ["Bluetooth 5.3", "12 hours playback"],
        "description": "A compact speaker.\nWater resistant.",
        "categoryTree": [{"catId": 1, "name": "Electronics"}, {"catId": 2, "name": "Speakers"}],
        "stats": {"current": [3499, 3289, 2646, 5321, 4999, -1, -1, 3428, -1, 2795, 3289, 14, 5]},
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(kc, "validate_images", lambda urls, max_images=10: list(urls)[:max_images])


def test_parse_asin_from_links_and_bare():
    assert kc.parse_asin("https://www.amazon.co.uk/dp/B0F3GWXLTS/ref=sr_1_1?keywords=x") == "B0F3GWXLTS"
    assert kc.parse_asin("https://www.amazon.co.uk/gp/product/b0f3gwxlts") == "B0F3GWXLTS"
    assert kc.parse_asin("https://www.amazon.co.uk/Some-Title-Words/dp/B07FZ8S74R") == "B07FZ8S74R"
    assert kc.parse_asin("B09HM94VDS") == "B09HM94VDS"
    assert kc.parse_asin("https://www.ebay.co.uk/itm/123456789012") == ""
    assert kc.parse_asin("") == ""


def test_keepa_time_conversion():
    assert kc.keepa_time_to_iso(7663880) == "2025-07-28 03:20 UTC"
    assert kc.keepa_time_to_iso(None) == ""


def test_amazon_in_stock_wins():
    price, seller, availability, reason = kc.choose_offer(_product(), use_buybox=False)
    assert (price, seller, reason) == (3499, "Amazon", "amazon")
    assert availability == "In stock (Amazon)"


def test_amazon_out_of_stock_falls_back_to_lowest_new():
    p = _product(availabilityAmazon=-1, stats={"current": [-1, 3289]})
    price, seller, availability, reason = kc.choose_offer(p, use_buybox=False)
    assert (price, reason) == (3289, "new")
    assert seller == "3rd party (lowest New)"


def test_preorder_amazon_offer_is_not_in_stock():
    p = _product(availabilityAmazon=1, stats={"current": [3499, -1]})
    price, _seller, availability, reason = kc.choose_offer(p, use_buybox=False)
    assert price == -1 and reason == "unavailable"
    assert availability == "Pre-order"


def test_nothing_buyable_reports_amazon_availability():
    p = _product(availabilityAmazon=-1, stats={"current": [-1, -1]})
    assert kc.choose_offer(p, use_buybox=False)[2] == "No Amazon offer"
    p = _product(availabilityAmazon=3, stats={"current": [-1, -1]})
    assert kc.choose_offer(p, use_buybox=False)[2] == "Back-order"


def test_dead_asin_and_odd_product_types():
    assert kc.choose_offer(_product(productType=4))[3] == "dead"
    assert kc.choose_offer(_product(productType=2))[3] == "no-price-data"


def test_buybox_used_only_when_enabled_and_shippable():
    stats = {"current": [-1, 2999], "buyBoxPrice": 2599, "buyBoxShipping": 349,
             "buyBoxIsShippable": True, "buyBoxIsAmazon": False,
             "buyBoxAvailabilityMessage": "IN_STOCK"}
    p = _product(availabilityAmazon=-1, stats=stats)
    assert kc.choose_offer(p, use_buybox=False)[:2] == (2999, "3rd party (lowest New)")
    price, seller, _a, reason = kc.choose_offer(p, use_buybox=True)
    assert (price, seller, reason) == (2948, "3rd party (Buy Box)", "buybox")
    stats["buyBoxAvailabilityMessage"] = "BACKORDER_NO_ETA"
    assert kc.choose_offer(_product(availabilityAmazon=-1, stats=stats), use_buybox=True)[3] == "new"


def test_normalize_gives_the_row_loop_shape():
    available, data = kc.normalize_product(_product(), use_buybox=False)
    assert available is True
    assert data["price"] == 34.99 and data["stock"] == kc.DEFAULT_STOCK
    assert data["title"] == "Example Wireless Speaker (Black)"
    assert data["brand"] == "ExampleBrand"
    assert data["main_image"] == "https://m.media-amazon.com/images/I/61ni3t6ryLL.jpg"
    assert data["additional_images"] == ["https://m.media-amazon.com/images/I/71abc.jpg"]
    assert data["eans"] == ["5012345678900", "012345678905"]
    assert data["product_code"] == "5012345678900"
    assert data["category_path"] == "Electronics > Speakers" and data["product_type"] == "Speakers"
    assert data["amazon_seller"] == "Amazon" and data["keepa_updated"] == "2025-07-28 03:20 UTC"
    assert "Bluetooth 5.3" in data["description"] and "Water resistant" in data["description"]
    assert data["condition"] == "New"
    assert data["amazon_type"] == ""
    assert set(kc.empty_amazon_response()) <= set(data)
    _a, typed = kc.normalize_product(_product(type="monitor", categoryTree=None), use_buybox=False)
    assert typed["amazon_type"] == "MONITOR" and typed["product_type"] == "monitor"


def test_unavailable_keeps_descriptive_fields_blank_but_reports_state():
    p = _product(availabilityAmazon=-1, stats={"current": [-1, -1]})
    available, data = kc.normalize_product(p, use_buybox=False)
    assert available is False
    assert data["price"] == 0 and data["stock"] == 0 and data["title"] == ""
    assert data["amazon_availability"] == "No Amazon offer"
    assert data["eans"] == ["5012345678900", "012345678905"]


def test_get_amazon_data_unknown_asin_is_not_available():
    available, data = kc.get_amazon_data("B000000000", {})
    assert available is False
    assert data["amazon_availability"] == "Not found on Amazon/Keepa"
    assert data["asin"] == "B000000000"


def test_client_requires_a_key():
    with pytest.raises(kc.PermanentError):
        kc.KeepaClient("")


def test_fetch_products_batches_and_paces(monkeypatch):
    calls = []

    def fake_request(self, endpoint, params):
        calls.append(params["asin"].split(","))
        return {"tokensLeft": 50, "refillRate": 20, "tokensConsumed": len(calls[-1]),
                "products": [{"asin": a} for a in calls[-1]]}

    monkeypatch.setattr(kc.KeepaClient, "_request", fake_request)
    client = kc.KeepaClient("k", use_buybox=False)
    asins = [f"B{i:09d}" for i in range(150)] + ["B000000000", " b000000001 "]
    out = client.fetch_products(asins)
    assert [len(c) for c in calls] == [100, 50]
    assert len(out) == 150 and "B000000001" in out
