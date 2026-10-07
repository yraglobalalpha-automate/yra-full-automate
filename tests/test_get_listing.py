"""OnBuyClient.get_listing: the one live listing of a SKU through a filtered read (used by remove_wrong_content)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.headers, self.text = status, body, {}, str(body)

    def json(self):
        return self._body


def _client(answer):
    from onbuy_client import OnBuyClient
    c = OnBuyClient.__new__(OnBuyClient)              # no credentials needed: only _send and site_id are used
    c.site_id = "2000"
    c.calls = []

    def _send(method, url, **kw):
        c.calls.append((method, kw.get("params")))
        return answer
    c._send = _send
    return c


def test_get_listing_returns_the_one_listing_with_that_exact_sku():
    rec = {"sku": "2258075893340-X", "name": "Some page", "product_encoded_id": "PHMMVQF"}
    c = _client(_Resp(200, {"results": [{"sku": "OTHER"}, rec]}))
    assert c.get_listing("2258075893340-X") == rec
    assert c.calls == [("GET", {"site_id": "2000", "limit": 5, "offset": 0, "filter[sku]": "2258075893340-X"})]


def test_get_listing_is_none_when_nothing_answers_to_the_sku():
    assert _client(_Resp(200, {"results": []})).get_listing("X-1") is None
    assert _client(_Resp(200, {"results": [{"sku": "NOT-X-1"}]})).get_listing("X-1") is None      # a filter that ignores the SKU


def test_get_listing_refuses_to_guess_between_two_listings():
    from retry_utils import PermanentError
    c = _client(_Resp(200, {"results": [{"sku": "X-1"}, {"sku": "X-1"}]}))
    with pytest.raises(PermanentError):
        c.get_listing("X-1")


def test_get_listing_asks_for_the_sku_the_platform_holds(monkeypatch):
    import sku_aliases
    monkeypatch.setitem(sku_aliases.TO_ONBUY, "TRUE-1", "WIRE-1")
    rec = {"sku": "WIRE-1", "name": "Some page"}
    c = _client(_Resp(200, {"results": [rec]}))
    assert c.get_listing("TRUE-1") == rec
    assert c.calls[0][1]["filter[sku]"] == "WIRE-1"
