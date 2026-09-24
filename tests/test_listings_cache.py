import json
import time

import listings_cache


def test_disabled_without_env(monkeypatch):
    monkeypatch.delenv("LISTINGS_CACHE", raising=False)
    assert listings_cache.load() is None
    listings_cache.save([{"sku": "1"}])  # no-op, must not raise


def test_roundtrip(monkeypatch, tmp_path):
    path = tmp_path / "cache.json"
    monkeypatch.setenv("LISTINGS_CACHE", str(path))
    monkeypatch.delenv("LISTINGS_CACHE_MAX_AGE", raising=False)
    items = [{"sku": "123", "price": "9.99", "stock": 5}]
    listings_cache.save(items)
    assert listings_cache.load() == items


def test_stale_cache_refetches(monkeypatch, tmp_path):
    path = tmp_path / "cache.json"
    monkeypatch.setenv("LISTINGS_CACHE", str(path))
    path.write_text(json.dumps([{"sku": "1"}]), encoding="utf-8")
    monkeypatch.setenv("LISTINGS_CACHE_MAX_AGE", "0")
    time.sleep(0.05)
    assert listings_cache.load() is None


def test_unreadable_cache_refetches(monkeypatch, tmp_path):
    path = tmp_path / "cache.json"
    monkeypatch.setenv("LISTINGS_CACHE", str(path))
    monkeypatch.delenv("LISTINGS_CACHE_MAX_AGE", raising=False)
    path.write_text("{not json", encoding="utf-8")
    assert listings_cache.load() is None
