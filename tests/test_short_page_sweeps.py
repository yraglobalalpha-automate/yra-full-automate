"""A listing sweep must not stop at a SHORT page (2026-10-10).

Makstore: GET /v2/listings said total_rows 11,102 but the page at offset 9,100 held only 99 items; every loop that stopped at the first short page saw
9,199 listings and never the oldest ~1,900 (the user's April examples, the hourly OPC import, the nightly content scan, the price audit ...).
OnBuyClient remembers total_rows of the last GET /listings answer and more_listings(offset, limit) says whether more listings follow the page."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import onbuy_client as oc

ROOT = Path(__file__).resolve().parents[1]


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def client():
    c = oc.OnBuyClient(consumer_key="k", secret_key="s", seller_id=1, site_id=2000, use_sandbox=False)
    c._token = "t"
    return c


def test_total_rows_of_the_last_listings_answer_decides_whether_more_follow(monkeypatch):
    c = client()
    monkeypatch.setattr(oc.requests, "request", lambda *a, **k: FakeResp(200, {"results": [], "metadata": {"limit": 100, "offset": 9100, "total_rows": 11102}}))
    c._send("GET", f"{oc.BASE_URL}/listings", what="page", params={"offset": 9100})
    assert c.more_listings(9100, 100) is True          # a short page at 9,100 is not the end
    assert c.more_listings(11000, 100) is True
    assert c.more_listings(11100, 100) is False        # the real last page (2 items)
    assert c.more_listings(11200, 100) is False


def test_without_total_rows_the_old_behaviour_stays(monkeypatch):
    c = client()
    for body in ({"results": []}, {"results": [], "metadata": {}}, {"results": [], "metadata": {"total_rows": "many"}}, [1, 2], None):
        monkeypatch.setattr(oc.requests, "request", lambda *a, _b=body, **k: FakeResp(200, _b))
        c._send("GET", f"{oc.BASE_URL}/listings", what="page", params={"offset": 0})
        assert c.more_listings(0, 100) is False


def test_an_unreadable_answer_or_another_endpoint_leaves_the_total_alone(monkeypatch):
    c = client()
    monkeypatch.setattr(oc.requests, "request", lambda *a, **k: FakeResp(200, {"metadata": {"total_rows": 500}}))
    c._send("GET", f"{oc.BASE_URL}/listings", what="page", params={"offset": 0})
    assert c.more_listings(0, 100) is True
    monkeypatch.setattr(oc.requests, "request", lambda *a, **k: FakeResp(200, {"metadata": {"total_rows": 3}}))
    c._send("GET", f"{oc.BASE_URL}/orders", what="orders")          # not the listings endpoint: ignored
    assert c.more_listings(0, 100) is True
    monkeypatch.setattr(oc.requests, "request", lambda *a, **k: FakeResp(200, ValueError("no json")))
    c._send("GET", f"{oc.BASE_URL}/listings", what="page")
    assert c.more_listings(0, 100) is False                         # unreadable -> no claim that more follow
    monkeypatch.setattr(oc.requests, "request", lambda *a, **k: FakeResp(200, {"metadata": {"total_rows": 9999}}))
    c._send("PUT", f"{oc.BASE_URL}/listings/by-sku", what="push")  # a write never touches it
    assert c.more_listings(0, 100) is False


def test_every_listing_sweep_loop_in_the_repo_continues_past_a_short_page():
    """Static guard: a loop over GET /listings may stop at a short page only when nothing follows it (more_listings)."""
    skip = {"listing_sweep.py", "onbuy_client.py"}
    stop = re.compile(r"^[ \t]*if len\((items|_items|page)\) < (limit|100)(?P<tail>[^\n]*):\n[ \t]+(break|return \w+)", re.M)
    offenders = []
    for path in sorted(ROOT.glob("*.py")):
        if path.name in skip:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "/listings" not in text:
            continue
        for m in stop.finditer(text):
            if "more_listings" not in m.group("tail"):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.name}:{line}")
    assert offenders == [], "sweep loops that stop at the first short page: " + ", ".join(offenders)
