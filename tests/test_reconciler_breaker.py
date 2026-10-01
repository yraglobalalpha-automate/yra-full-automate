"""The deletion reconciler's circuit breaker (2026-10-01). Replays the
2026-09-30 incident: a broken sheet read made ~5,000 of ~5,800 created
listings look deleted, and the reconciler zeroed their stock and queued
them for deletion. It must now refuse - changing nothing - and still do its
normal small jobs."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import deletion_reconciler as dr


def test_breaker_thresholds():
    t = dr.mass_action_tripped
    assert t(5055, 5816, 300, 0.10, False)        # the incident
    assert not t(40, 5816, 300, 0.10, False)      # ordinary churn
    assert not t(500, 5816, 300, 0.10, False)     # big count, but under 10% of the registry
    assert t(600, 5816, 300, 0.10, False)         # big count and over 10%
    assert not t(80, 100, 300, 0.10, False)       # small registry: count floor protects it
    assert not t(5055, 5816, 300, 0.10, True)     # deliberate mass cleanup (DELIST_ALLOW_MASS)
    assert not t(10, 0, 300, 0.10, False)         # no registry


class FakeOnBuy:
    site_id = 1

    def __init__(self):
        self.updates = []
        self.sends = []

    def update_listings_by_sku_batch(self, chunk):
        self.updates.append(list(chunk))
        return [{"sku": s} for s, _p, _st in chunk]

    def check_winning(self, skus):
        return []          # nobody is "already gone"

    def _send(self, *a, **k):
        self.sends.append((a, k))
        raise AssertionError("no DELETE may be sent in this scenario")


class FakeQueueTab:
    id = 7

    def __init__(self):
        self.appended = []

    def append_rows(self, rows, **kw):
        self.appended.extend(rows)

    def update(self, *a, **k):
        raise AssertionError("no queue cell may be updated in this scenario")


class FakeNotify:
    def __init__(self):
        self.sent = []

    def send_alert_email(self, subject, body, routine=False):
        self.sent.append((subject, routine))


@pytest.fixture
def harness(monkeypatch):
    class H:
        pass
    h = H()
    h.onbuy, h.tab, h.notify = FakeOnBuy(), FakeQueueTab(), FakeNotify()
    monkeypatch.setitem(sys.modules, "notify", h.notify)
    monkeypatch.setattr(dr.supabase_db, "mark_listing_inactive", lambda skus: True)

    def arm(created_n, on_sheet, queue=None):
        created = [{"sku": f"SKU{i:04d}", "price": 10.0, "active": True} for i in range(created_n)]
        sheet = {created[i]["sku"] for i in range(on_sheet)}
        monkeypatch.setattr(dr.supabase_db, "fetch_created_rows", lambda: created)
        monkeypatch.setattr(dr, "_product_tab_skus", lambda book: (sheet, {dr._zeroless(s) for s in sheet}))
        monkeypatch.setattr(dr, "_queue", lambda book: (h.tab, dict(queue or {})))
    h.arm = arm
    return h


def test_incident_replay_empty_sheet_read_changes_nothing(harness):
    harness.arm(created_n=1000, on_sheet=0)          # the wrong-tab read: nothing found on the sheet
    dr.run(object(), harness.onbuy)
    assert harness.onbuy.updates == []               # no stock zeroed
    assert harness.tab.appended == []                # nothing queued
    assert harness.onbuy.sends == []                 # nothing deleted
    assert harness.notify.sent == [("Delist reconciler refused to act", False)]   # loud, never routine


def test_due_deletions_are_held_while_the_read_looks_broken(harness):
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime(dr.TS)
    queue = {f"SKU{i:04d}": {"row": i + 2, "first": old, "zeroed": old, "deleted": "", "note": ""}
             for i in range(400)}                    # 400 queued entries, all past the grace window
    harness.arm(created_n=1000, on_sheet=0, queue=queue)
    dr.run(object(), harness.onbuy)
    assert harness.onbuy.sends == []                 # FakeOnBuy._send would raise if a DELETE went out
    assert [s for s, _r in harness.notify.sent] == ["Delist reconciler refused to act"]


def test_unreadable_product_tab_is_a_refusal_not_an_empty_sheet(harness, monkeypatch):
    harness.arm(created_n=1000, on_sheet=1000)

    def boom(book):
        raise RuntimeError("product tab 'OnBuy Categories' has no SKU column")
    monkeypatch.setattr(dr, "_product_tab_skus", boom)
    dr.run(object(), harness.onbuy)
    assert harness.onbuy.updates == [] and harness.tab.appended == []
    assert [s for s, _r in harness.notify.sent] == ["Delist reconciler refused to act"]


def test_normal_small_deletion_is_still_handled(harness):
    harness.arm(created_n=1000, on_sheet=990)        # ten rows really were deleted from the sheet
    dr.run(object(), harness.onbuy)
    assert len(harness.onbuy.updates) == 1
    assert len(harness.onbuy.updates[0]) == 10
    assert all(stock == 0 for _s, _p, stock in harness.onbuy.updates[0])    # stock zeroed
    assert len(harness.tab.appended) == 10                                  # queued for the grace window
    assert harness.notify.sent == [("Delist reconciler: sheet-deleted products taken off OnBuy", True)]  # routine
