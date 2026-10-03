"""The mirror prefetch asks Supabase in chunks. One `in.(...)` URL carrying a whole run's batch (1,448-2,275 SKUs on
the Amazon tabs) came back 400 Bad Request, so those runs priced and exported with NO mirror (found 2026-10-03):
every stale Fee % / Profit % cell then read as a manual override and the tracking columns fell back to defaults."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supabase_db  # noqa: E402


class Resp:
    def __init__(self, status=200, rows=None, text=""):
        self.status_code, self._rows, self.text = status, rows, text

    def json(self):
        return self._rows


def _env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-key")


def _skus_of(params):
    raw = params["SKU"]
    assert raw.startswith("in.(") and raw.endswith(")")
    return raw[4:-1].split(",")


def test_a_big_batch_is_fetched_in_small_requests_and_merged(monkeypatch):
    _env(monkeypatch)
    sizes = []

    def fake_get(url, headers=None, params=None, timeout=None):
        skus = _skus_of(params)
        sizes.append(len(skus))
        if len(skus) > 400:                                  # the real service refused a long URL
            return Resp(400, text="Bad Request")
        return Resp(200, [{"SKU": s, "OPC": "X"} for s in skus if s != "sku-missing"])

    monkeypatch.setattr(supabase_db.requests, "get", fake_get)
    skus = [f"sku{i}" for i in range(2275)] + ["sku-missing"]
    got = supabase_db.fetch_existing_fields(skus)
    assert len(got) == 2275 and got["sku1000"]["OPC"] == "X" and "sku-missing" not in got
    assert max(sizes) <= supabase_db.PREFETCH_CHUNK and len(sizes) == -(-len(skus) // supabase_db.PREFETCH_CHUNK)


def test_one_failing_chunk_costs_only_its_own_skus(monkeypatch):
    _env(monkeypatch)
    calls = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        skus = _skus_of(params)
        if calls["n"] == 2:
            return Resp(500, text="boom")
        if calls["n"] == 3:
            raise supabase_db.requests.exceptions.ConnectionError("down")
        return Resp(200, [{"SKU": s} for s in skus])

    monkeypatch.setattr(supabase_db.requests, "get", fake_get)
    n = supabase_db.PREFETCH_CHUNK
    skus = [f"s{i}" for i in range(n * 4)]
    got = supabase_db.fetch_existing_fields(skus)
    assert set(got) == set(skus[:n]) | set(skus[3 * n:])         # chunks 2 and 3 are lost, 1 and 4 survive


def test_a_chunk_with_an_unexpected_answer_is_skipped_not_fatal(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(supabase_db.requests, "get", lambda *a, **k: Resp(200, [{"no_sku_here": 1}]))
    assert supabase_db.fetch_existing_fields(["a", "b"]) == {}


def test_full_rows_are_chunked_too_and_ask_for_every_column(monkeypatch):
    _env(monkeypatch)
    selects = []

    def fake_get(url, headers=None, params=None, timeout=None):
        selects.append(params["select"])
        return Resp(200, [{"SKU": s, "Title": "t"} for s in _skus_of(params)])

    monkeypatch.setattr(supabase_db.requests, "get", fake_get)
    got = supabase_db.fetch_full_rows([f"k{i}" for i in range(supabase_db.PREFETCH_CHUNK + 5)])
    assert len(got) == supabase_db.PREFETCH_CHUNK + 5 and selects == ["*", "*"]


def test_nothing_to_fetch_and_no_credentials_are_quiet_empty_answers(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.setattr(supabase_db.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert supabase_db.fetch_existing_fields([]) == {}
    assert supabase_db.fetch_existing_fields(["a"]) == {}
    assert supabase_db.fetch_full_rows(["a"]) == {}
    assert supabase_db.fetch_existing_fields(["", None]) == {}


def test_the_tracking_columns_are_still_selected(monkeypatch):
    _env(monkeypatch)
    seen = []
    monkeypatch.setattr(supabase_db.requests, "get",
                        lambda url, headers=None, params=None, timeout=None: (seen.append(params["select"]), Resp(200, []))[1])
    supabase_db.fetch_existing_fields(["x"])
    assert '"Profit %"' in seen[0] and '"Fee %"' in seen[0] and "SKU" in seen[0]
