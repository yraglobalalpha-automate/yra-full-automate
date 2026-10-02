"""delete_listings_batch: reading OnBuy's per-SKU answers (2026-10-02)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import delete_listings_batch as d


def test_ok_node():
    assert d.classify({"status": "ok"}) == "ok"


def test_gone_and_suspended_and_other_errors():
    assert d.classify({"error": "Listing not found"}) == "gone"
    assert d.classify({"error": "SKU does not exist"}) == "gone"
    assert d.classify({"error": "Listing is suspended"}) == "suspended"
    assert d.classify({"error": "something odd"}) == "something odd"
    assert d.classify({}) == "no answer"


def test_parse_a_batch_response_keyed_by_sku():
    body = {"success": True, "results": {"111": {"status": "ok"}, "222": {"error": "Listing not found"},
                                         "333": {"error": "Listing is suspended"}}}
    assert d.parse_results(body) == {"111": "ok", "222": "gone", "333": "suspended"}


def test_the_all_failed_400_body_still_carries_per_sku_answers():
    body = {"error": {"message": "No listings were deleted. Please see the results node for more information",
                      "errorCode": "invalidRequest", "responseCode": 400},
            "results": {"367059076715": {"error": "Listing is suspended"}}}
    assert d.parse_results(body) == {"367059076715": "suspended"}


def test_answers_come_back_under_the_sku_the_sheet_holds(monkeypatch):
    monkeypatch.setattr(d.sku_aliases, "TO_TRUE", {"0371763399077": "371763399077"})
    body = {"results": {"0371763399077": {"status": "ok"}}}
    assert d.parse_results(body) == {"371763399077": "ok"}


def test_list_shaped_results_are_read_too():
    body = {"results": [{"sku": "111", "status": "ok"}, {"sku": "222", "error": "Listing not found"}]}
    assert d.parse_results(body) == {"111": "ok", "222": "gone"}


def test_unusable_bodies_give_no_answers():
    assert d.parse_results(None) == {}
    assert d.parse_results({"error": {"message": "bad"}}) == {}
    assert d.parse_results({"results": "nope"}) == {}
