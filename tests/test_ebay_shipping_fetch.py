"""get_ebay_data hands back eBay's own delivery quote, taken from the item answer
it already fetches (shippingOptions) - no extra API call. Network faked; the
real parsing, retry and multi-variation paths run."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("gspread")                 # generate_xml imports the Google client at module level
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate_xml as gx  # noqa: E402

URL = "https://www.ebay.co.uk/itm/123456789012"


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = {}
        self.text = ""

    def json(self):
        return self._body


def option(value, currency="GBP", **extra):
    return {"shippingCostType": "FIXED", "shippingCost": {"value": value, "currency": currency}, **extra}


def item(options, **extra):
    base = {"title": "Widget", "price": {"value": "10.00", "currency": "GBP"}, "legacyItemId": "123456789012",
            "estimatedAvailabilities": [{"estimatedAvailabilityStatus": "IN_STOCK", "estimatedAvailableQuantity": 7}],
            "description": "<p>Spec</p>", "condition": "New", "localizedAspects": []}
    if options is not None:
        base["shippingOptions"] = options
    base.update(extra)
    return base


@pytest.fixture
def fetch(monkeypatch):
    def run(first, status=200, group=None):
        calls = []

        def fake_get(url, headers=None, params=None, timeout=None):
            calls.append(url.rsplit("/", 1)[-1])
            if url.endswith("get_item_by_legacy_id"):
                return FakeResponse(status, first)
            return FakeResponse(200, group or {})

        monkeypatch.setattr(gx.requests, "get", fake_get)
        monkeypatch.setattr(gx, "validate_images", lambda urls, max_images=10: [u for u in urls if u][:max_images])
        available, data = gx.get_ebay_data(URL, "token")
        return available, data, calls
    return run


def test_free_delivery_is_a_zero_quote_from_the_one_call_the_sync_already_makes(fetch):
    available, data, calls = fetch(item([option("0.00")]))
    assert available is True and data["shipping_cost"] == 0.0 and data["price"] == 10.0
    assert calls == ["get_item_by_legacy_id"]


def test_paid_delivery_is_the_fee(fetch):
    _, data, _ = fetch(item([option("3.99")]))
    assert data["shipping_cost"] == pytest.approx(3.99)


def test_the_cheapest_delivered_option_wins(fetch):
    _, data, _ = fetch(item([option("7.50"), option("2.95"), option("0.00", type="PICKUP")]))
    assert data["shipping_cost"] == pytest.approx(2.95)


@pytest.mark.parametrize("options", [None, [], [{"shippingCostType": "CALCULATED"}], [option("4.00", "USD")]])
def test_no_usable_quote_is_none_not_zero(fetch, options):
    available, data, _ = fetch(item(options))
    assert available is True and data["shipping_cost"] is None          # never "free" by default


def test_a_multi_variation_listing_quotes_its_own_variation(fetch):
    group = {"items": [item([option("9.99")], legacyItemId="111"), item([option("1.49")], legacyItemId="123456789012")]}
    body = {"errors": [{"errorId": gx.ITEM_GROUP_ERROR_ID}]}
    available, data, calls = fetch(body, status=400, group=group)
    assert available is True and data["shipping_cost"] == pytest.approx(1.49)
    assert calls == ["get_item_by_legacy_id", "get_items_by_item_group"]


def test_an_unavailable_item_carries_no_quote(fetch):
    gone = item([option("3.99")], estimatedAvailabilities=[{"estimatedAvailabilityStatus": "OUT_OF_STOCK"}])
    available, data, _ = fetch(gone)
    assert available is False and data.get("shipping_cost") is None
