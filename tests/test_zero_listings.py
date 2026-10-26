"""zero_listings: stock 0 on named listings only, at their own price, never on a listing that is not there or already empty."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import zero_listings as zl


class Resp:
    status_code = 200

    def __init__(self, body):
        self._body = body
        self.headers = {}
        self.text = str(body)

    def json(self):
        return self._body


class FakeOnBuy:
    site_id = "2000"

    def __init__(self, listings):
        self.listings = {k: dict(v) for k, v in listings.items()}
        self.updates = []

    def authenticate(self):
        return True

    def _send(self, method, url, **kw):
        sku = kw["params"]["filter[sku]"]
        rec = self.listings.get(sku)
        return Resp({"results": [rec] if rec else []})

    def update_listing(self, *, sku, price, stock):
        self.updates.append((sku, price, stock))
        self.listings[sku]["stock"] = stock


def _run(monkeypatch, listings, skus, dry_run=False):
    onbuy = FakeOnBuy(listings)
    monkeypatch.setattr(zl, "OnBuyClient", lambda: onbuy)
    monkeypatch.setattr(zl, "WANT", skus)
    monkeypatch.setattr(zl, "DRY_RUN", dry_run)
    monkeypatch.setattr(zl.time, "sleep", lambda s: None)
    monkeypatch.setattr(zl, "READ_TRIES", 3)
    return onbuy


L = {"A-1": {"sku": "A-1", "name": "Wrong Product", "price": "83.82", "stock": 4, "product_encoded_id": "PX1"},
     "B-2": {"sku": "B-2", "name": "Other", "price": "10.00", "stock": 0, "product_encoded_id": "PX2"}}


def test_zeroes_a_listing_with_stock_at_its_own_price(monkeypatch):
    onbuy = _run(monkeypatch, L, ["A-1"])
    zl.main()
    assert onbuy.updates == [("A-1", 83.82, 0)]


def test_dry_run_changes_nothing(monkeypatch):
    onbuy = _run(monkeypatch, L, ["A-1"], dry_run=True)
    zl.main()
    assert onbuy.updates == []


def test_an_empty_listing_is_left_alone_and_a_missing_one_is_a_problem(monkeypatch):
    onbuy = _run(monkeypatch, L, ["B-2"])
    zl.main()
    assert onbuy.updates == []
    onbuy2 = _run(monkeypatch, L, ["NOPE"])
    with pytest.raises(SystemExit):
        zl.main()
    assert onbuy2.updates == []


def test_only_the_named_skus_are_touched(monkeypatch):
    onbuy = _run(monkeypatch, {**L, "C-3": {"sku": "C-3", "name": "x", "price": "5", "stock": 9}}, ["A-1"])
    zl.main()
    assert [u[0] for u in onbuy.updates] == ["A-1"] and onbuy.listings["C-3"]["stock"] == 9


def test_a_listing_without_a_usable_price_is_never_zeroed_blind(monkeypatch):
    onbuy = _run(monkeypatch, {"Z-9": {"sku": "Z-9", "name": "x", "price": "0", "stock": 3}}, ["Z-9"])
    with pytest.raises(SystemExit):
        zl.main()
    assert onbuy.updates == []


def test_a_read_that_lags_the_write_is_given_a_few_looks_before_it_is_a_problem(monkeypatch):
    onbuy = _run(monkeypatch, L, ["A-1"])
    real_update, reads = onbuy.update_listing, {"n": 0}

    def lagging_update(**kw):                       # the write is accepted but reads keep the old stock for two looks
        reads["want"] = kw["stock"]
        onbuy.updates.append((kw["sku"], kw["price"], kw["stock"]))
    onbuy.update_listing = lagging_update
    real_send = onbuy._send

    def send(method, url, **kw):
        reads["n"] += 1
        if reads["n"] >= 4 and "want" in reads:     # first read before the write, then 2 stale looks, then the truth
            onbuy.listings["A-1"]["stock"] = 0
        return real_send(method, url, **kw)
    onbuy._send = send
    zl.main()                                       # does not raise: the third look shows stock 0
    assert onbuy.updates == [("A-1", 83.82, 0)]
