"""keepa_cache: the hand-over of Keepa answers from the lock-free prefetch job to the locked sync (2026-10-05)."""
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import keepa_cache

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def payload(age_h=0.5, run_id="111", asked=("B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA3"), products=None, v=1):
    done = NOW - timedelta(hours=age_h)
    return {"v": v, "tab": "Amazon", "run_id": run_id, "started_at": "2026-10-05T10:00:00Z",
            "finished_at": done.strftime("%Y-%m-%dT%H:%M:%SZ"), "asked": list(asked),
            "products": products if products is not None else {"B0AAAAAAA1": {"asin": "B0AAAAAAA1"}, "B0AAAAAAA2": {"asin": "B0AAAAAAA2"}}}


def test_object_path_is_a_safe_file_name():
    assert keepa_cache.object_path("Amazon") == "amazon.json.gz"
    assert keepa_cache.object_path("Amazon Tab (2)") == "amazon_tab_2.json.gz"


def test_the_sync_only_trusts_the_cache_of_its_own_run_and_a_young_one():
    assert keepa_cache.usable(payload(), "111", NOW, 3, require_same_run=True)
    assert not keepa_cache.usable(payload(run_id="110"), "111", NOW, 3, require_same_run=True)   # the previous cycle's data
    assert not keepa_cache.usable(payload(), "", NOW, 3, require_same_run=True)
    assert not keepa_cache.usable(payload(age_h=3.5), "111", NOW, 3, require_same_run=True)
    assert keepa_cache.usable(payload(age_h=-0.1), "111", NOW, 3, require_same_run=True)       # clock skew between runners
    assert not keepa_cache.usable(payload(age_h=-2), "111", NOW, 3, require_same_run=True)


def test_a_prefetch_may_reuse_a_young_cache_of_an_earlier_attempt():
    assert keepa_cache.usable(payload(run_id="old", age_h=1.0), "new", NOW, 1.5, require_same_run=False)
    assert not keepa_cache.usable(payload(run_id="old", age_h=2.0), "new", NOW, 1.5, require_same_run=False)


def test_unusable_shapes_are_refused():
    assert not keepa_cache.usable(None, "1", NOW, 3, True)
    assert not keepa_cache.usable(payload(v=2), "111", NOW, 3, True)
    bad = payload()
    bad["finished_at"] = "yesterday"
    assert not keepa_cache.usable(bad, "111", NOW, 3, True)
    bad = payload()
    bad["products"] = []
    assert not keepa_cache.usable(bad, "111", NOW, 3, True)


def test_split_hands_back_cached_answers_and_only_the_unasked_asins_are_fetched():
    have, need = keepa_cache.split(["b0aaaaaaa1", "B0AAAAAAA2", "B0AAAAAAA3", "B0AAAAAAA9"], payload())
    assert set(have) == {"B0AAAAAAA1", "B0AAAAAAA2"}
    # B0AAAAAAA3 was asked and Keepa returned nothing: unknown to Keepa, not asked again; only the never-asked one is fetched
    assert need == ["B0AAAAAAA9"]


class FakeResp:
    def __init__(self, status=200, text="", content=b""):
        self.status_code, self.text, self.content = status, text, content


class FakeRequests:
    """Stands in for the requests module inside keepa_cache: a tiny in-memory Supabase Storage."""
    exceptions = requests.exceptions

    def __init__(self, bucket_exists=True):
        self.bucket_exists, self.objects, self.calls = bucket_exists, {}, []

    def post(self, url, headers=None, data=None, json=None, timeout=None):
        self.calls.append(("POST", url, dict(headers or {})))
        if url.endswith("/storage/v1/bucket"):
            self.bucket_exists = True
            return FakeResp(200)
        if not self.bucket_exists:
            return FakeResp(404, text='{"error":"Bucket not found"}')
        self.objects[url] = data
        return FakeResp(200)

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, dict(headers or {})))
        key = url.replace("/object/authenticated/", "/object/")
        return FakeResp(200, content=self.objects[key]) if key in self.objects else FakeResp(404)


@pytest.fixture
def storage(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co/")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key")
    fake = FakeRequests()
    monkeypatch.setattr(keepa_cache, "requests", fake)
    return fake


def test_save_then_load_round_trips_and_is_private(storage):
    started = NOW - timedelta(hours=1)
    assert keepa_cache.save("Amazon", {"b0aaaaaaa1": {"asin": "B0AAAAAAA1", "price": 1}}, ["B0AAAAAAA1", "b0aaaaaaa2"], 777, started, now=NOW)
    back = keepa_cache.load("Amazon")
    assert back["run_id"] == "777" and back["asked"] == ["B0AAAAAAA1", "B0AAAAAAA2"]
    assert back["products"] == {"B0AAAAAAA1": {"asin": "B0AAAAAAA1", "price": 1}}
    assert back["started_at"] == "2026-10-05T11:00:00Z" and back["finished_at"] == "2026-10-05T12:00:00Z"
    gets = [c for c in storage.calls if c[0] == "GET"]
    assert "/object/authenticated/keepa-cache/amazon.json.gz" in gets[0][1]      # the authenticated (private) route


def test_first_ever_save_creates_the_private_bucket_and_retries_once(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key")
    fake = FakeRequests(bucket_exists=False)
    monkeypatch.setattr(keepa_cache, "requests", fake)
    assert keepa_cache.save("Amazon", {}, [], "1", NOW, now=NOW)
    posts = [c[1] for c in fake.calls if c[0] == "POST"]
    assert posts[0].endswith("/object/keepa-cache/amazon.json.gz") and posts[1].endswith("/storage/v1/bucket")
    assert posts[2].endswith("/object/keepa-cache/amazon.json.gz")


def test_nothing_here_ever_raises(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    assert keepa_cache.save("Amazon", {}, [], "1", NOW) is False and keepa_cache.load("Amazon") is None
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-key")

    class Boom:
        exceptions = requests.exceptions

        def post(self, *a, **k):
            raise requests.exceptions.ConnectionError("down")

        def get(self, *a, **k):
            raise requests.exceptions.ConnectionError("down")
    monkeypatch.setattr(keepa_cache, "requests", Boom())
    assert keepa_cache.save("Amazon", {}, [], "1", NOW) is False and keepa_cache.load("Amazon") is None

    class Corrupt:
        exceptions = requests.exceptions

        def get(self, *a, **k):
            return FakeResp(200, content=b"not gzip at all")
    monkeypatch.setattr(keepa_cache, "requests", Corrupt())
    assert keepa_cache.load("Amazon") is None
    assert json.loads(gzip.decompress(gzip.compress(b"{}"))) == {}


class FakeKeepa:
    def __init__(self, known):
        self.known, self.asked, self.tokens_consumed = known, [], 0

    def fetch_products(self, asins):
        self.asked.append(list(asins))
        self.tokens_consumed += len(asins)
        return {a: {"asin": a, "fresh": True} for a in asins if a in self.known}


def test_answers_without_the_cache_fetch_everything_live(storage):
    keepa = FakeKeepa({"B0AAAAAAA1", "B0AAAAAAA2"})
    products, cached, used = keepa_cache.answers(keepa, ["B0AAAAAAA1", "B0AAAAAAA2"], "Amazon", False, False, "9", 3, 1.5, now=NOW)
    assert cached == 0 and used is None and set(products) == {"B0AAAAAAA1", "B0AAAAAAA2"} and keepa.asked == [["B0AAAAAAA1", "B0AAAAAAA2"]]


def test_the_prefetch_saves_and_the_sync_of_the_same_run_reads_it_back_without_a_live_fetch(storage):
    # prefetch job (no cache yet): fetches live, then stores
    pre = FakeKeepa({"B0AAAAAAA1", "B0AAAAAAA2"})
    products, cached, used = keepa_cache.answers(pre, ["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA3"], "Amazon", False, True, "42", 3, 1.5, now=NOW)
    assert cached == 0 and used is None
    assert keepa_cache.store("Amazon", products, ["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA3"], used, "42", NOW - timedelta(minutes=30))
    # sync job (same run id): everything, including the ASIN Keepa does not know, comes from the cache - no live call at all
    sync = FakeKeepa(set())
    got, cached, used = keepa_cache.answers(sync, ["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA3"], "Amazon", True, False, "42", 3, 1.5,
                                            now=datetime.now(timezone.utc))
    assert sync.asked == [] and cached == 3 and set(got) == {"B0AAAAAAA1", "B0AAAAAAA2"}
    # a row that joined the batch after the prefetch is fetched live, only that one
    got, cached, _ = keepa_cache.answers(sync, ["B0AAAAAAA1", "B0AAAAAAA9"], "Amazon", True, False, "42", 3, 1.5, now=datetime.now(timezone.utc))
    assert sync.asked == [["B0AAAAAAA9"]] and cached == 1


def test_the_sync_ignores_a_cache_of_another_run(storage):
    pre = FakeKeepa({"B0AAAAAAA1"})
    products, _, used = keepa_cache.answers(pre, ["B0AAAAAAA1"], "Amazon", False, True, "41", 3, 1.5, now=NOW)
    keepa_cache.store("Amazon", products, ["B0AAAAAAA1"], used, "41", NOW)
    sync = FakeKeepa({"B0AAAAAAA1"})
    _, cached, used = keepa_cache.answers(sync, ["B0AAAAAAA1"], "Amazon", True, False, "42", 3, 1.5, now=datetime.now(timezone.utc))
    assert cached == 0 and used is None and sync.asked == [["B0AAAAAAA1"]]


def test_a_prefetch_rerun_merges_with_the_young_cache_it_reuses(storage):
    first = FakeKeepa({"B0AAAAAAA1", "B0AAAAAAA2"})
    products, _, used = keepa_cache.answers(first, ["B0AAAAAAA1"], "Amazon", False, True, "40", 3, 1.5, now=NOW)
    keepa_cache.store("Amazon", products, ["B0AAAAAAA1"], used, "40", NOW)
    again = FakeKeepa({"B0AAAAAAA1", "B0AAAAAAA2"})
    products, cached, used = keepa_cache.answers(again, ["B0AAAAAAA1", "B0AAAAAAA2"], "Amazon", False, True, "43", 3, 1.5,
                                                 now=datetime.now(timezone.utc))
    assert cached == 1 and again.asked == [["B0AAAAAAA2"]]
    keepa_cache.store("Amazon", products, ["B0AAAAAAA1", "B0AAAAAAA2"], used, "43", datetime.now(timezone.utc))
    assert set(keepa_cache.load("Amazon")["products"]) == {"B0AAAAAAA1", "B0AAAAAAA2"}
