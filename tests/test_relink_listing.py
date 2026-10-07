"""relink_listing: move a listing from a wrong catalogue product onto the right one - guarded, one SKU at a time (2026-10-06)."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import relink_listing as rl


# ---------------------------------------------------------------- parse_pairs

def test_parse_pairs_accepts_sku_opc_pairs_and_uppercases_the_opc():
    assert rl.parse_pairs("111111111111:pxysn8g, 222222222222:PXABC12") == [("111111111111", "PXYSN8G"), ("222222222222", "PXABC12")]


@pytest.mark.parametrize("bad", ["111", "111:", ":PXYSN8G", "abc:PXYSN8G", "111:XYZ", "111:PXYSN8G,111:PXABC12", "111:PX YSN"])
def test_parse_pairs_rejects_anything_malformed_or_repeated(bad):
    with pytest.raises(ValueError):
        rl.parse_pairs(bad)


# ---------------------------------------------------------------- decide / verify_after

def _listing(**kw):
    rec = {"sku": "111111111111", "name": "Wrong Product", "price": "67.05", "stock": 0, "product_encoded_id": "PXOLD01"}
    rec.update(kw)
    return rec


ROW = {"row": 5, "title": "Right Product Title", "price": 67.05, "opc": "PXOLD01"}
QOK = {"status": "success", "opc": "PXNEW01"}


def test_decide_go_uses_the_listings_own_price():
    plan = rl.decide("111111111111", "PXNEW01", _listing(price="64.25"), ROW, QOK, set())
    assert plan["go"] and plan["price"] == 64.25 and plan["old_opc"] == "PXOLD01" and plan["row"] == 5


@pytest.mark.parametrize("listing,row,queue,blocked,word", [
    (_listing(), ROW, QOK, {"111111111111"}, "blocked"),
    (None, ROW, QOK, set(), "no live listing"),
    (_listing(product_encoded_id="PXNEW01"), ROW, QOK, set(), "already on the target"),
    (_listing(stock=3), ROW, QOK, set(), "still shows stock"),
    (_listing(stock=None), ROW, QOK, set(), "still shows stock"),
    (_listing(), None, QOK, set(), "not found on the sheet"),
    (_listing(), dict(ROW, title=""), QOK, set(), "no title"),
    (_listing(), dict(ROW, opc="PXOTHER"), QOK, set(), "disagrees"),
    (_listing(), ROW, None, set(), "cannot be verified"),
    (_listing(), ROW, {"status": "failed", "opc": "PXNEW01"}, set(), "not success"),
    (_listing(), ROW, {"status": "success", "opc": "PXELSE9"}, set(), "not PXNEW01"),
    (_listing(price="0"), dict(ROW, price=0), QOK, set(), "no usable price"),
])
def test_decide_skips_unsafe_cases(listing, row, queue, blocked, word):
    plan = rl.decide("111111111111", "PXNEW01", listing, row, queue, blocked)
    assert not plan["go"] and word in plan["reason"]


def test_decide_sheet_opc_may_be_blank_or_pending_and_unverified_opc_needs_the_explicit_flag():
    assert rl.decide("111111111111", "PXNEW01", _listing(), dict(ROW, opc="PENDING"), QOK, set())["go"]
    assert rl.decide("111111111111", "PXNEW01", _listing(), dict(ROW, opc=""), QOK, set())["go"]
    assert not rl.decide("111111111111", "PXNEW01", _listing(), ROW, None, set())["go"]
    assert rl.decide("111111111111", "PXNEW01", _listing(), ROW, None, set(), allow_unverified=True)["go"]


def test_decide_falls_back_to_the_sheet_price_when_the_listing_has_none():
    plan = rl.decide("111111111111", "PXNEW01", _listing(price=""), dict(ROW, price=12.5), QOK, set())
    assert plan["go"] and plan["price"] == 12.5


def test_verify_after_accepts_the_right_product_and_names_every_problem():
    good = _listing(name="Right Product Title", product_encoded_id="PXNEW01", price="67.05", stock=0)
    assert rl.verify_after(good, "PXNEW01", 67.05, "Right Product Title") == []
    assert rl.verify_after(dict(good, name="Right Product Title With Extra Words"), "PXNEW01", 67.05, "Right Product Title") == []  # prefix
    bad = rl.verify_after(dict(good, product_encoded_id="PXOLD01", stock=4, price="70", name="Wrong Product"), "PXNEW01", 67.05, "Right Product Title")
    assert len(bad) == 4
    assert rl.verify_after(None, "PXNEW01", 67.05, "x") == ["the SKU has no listing after the attach"]


# ---------------------------------------------------------------- the whole run with fakes

class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.headers = status, body if body is not None else {}, {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeOnBuy:
    site_id, seller_id = "2000", "9"

    def __init__(self, listings, queue, new_name="Right Product Title", attach_fail_times=0,
                 attach_error="The SKU already exists for a different product",
                 delete_error=None, attach_forever=False, failure_shape="message", delete_lag=0):
        self.listings = {k: dict(v) for k, v in listings.items()}
        self.queue, self.new_name = queue, new_name
        self.attach_fail_times, self.attach_error, self.attach_forever = attach_fail_times, attach_error, attach_forever
        self.delete_error = delete_error
        # "message" = HTTP 200 + {"success": false, "message": ...} inside results (the real answer); "error" = HTTP 400 + an "error" key
        self.failure_shape = failure_shape
        self.delete_lag = delete_lag              # POST attempts that still fail because the deleted listing lingers
        self.pending_delete = {}
        self.calls = []

    def authenticate(self):
        return True

    @staticmethod
    def _fit_product_name(title, limit=150):
        return " ".join(str(title or "").split())

    def list_queue(self, limit=50, offset=0):
        return {"results": self.queue[offset:offset + limit]}

    def _send(self, method, url, *, what, **kw):
        self.calls.append(method)
        if method == "GET":
            rec = self.listings.get(kw["params"].get("filter[sku]"))
            return Resp(200, {"results": [rec] if rec else []})
        if method == "DELETE":
            sku = kw["json"]["skus"][0]
            if self.delete_error:
                return Resp(200, {"results": {sku: {"error": self.delete_error}}})
            if self.delete_lag:
                self.pending_delete[sku] = self.delete_lag
            else:
                self.listings.pop(sku, None)
            return Resp(200, {"results": {sku: {"status": "ok"}}})
        item = kw["json"]["listings"][0]
        lingering = self.pending_delete.get(item["sku"], 0) > 0
        if lingering:
            self.pending_delete[item["sku"]] -= 1
            if self.pending_delete[item["sku"]] == 0:
                self.listings.pop(item["sku"], None)
        if lingering or self.attach_forever or self.attach_fail_times > 0:
            if not lingering:
                self.attach_fail_times -= 1
            if self.failure_shape == "message":
                return Resp(200, {"success": True, "results": [{"success": False, "message": self.attach_error,
                                                                 "sku": item["sku"], "opc": item["opc"]}]})
            return Resp(400, {"success": False, "results": [{"sku": item["sku"], "error": self.attach_error}]})
        self.listings[item["sku"]] = {"sku": item["sku"], "name": self.new_name, "price": str(item["price"]), "stock": item["stock"],
                                      "product_encoded_id": item["opc"]}
        return Resp(200, {"success": True, "results": [{"success": True, "sku": item["sku"], "product_listing_id": 1}]})


class FakeSheet:
    title = "Sheet1"

    def __init__(self):
        self.rows = [{"SKU": "999999999999", "Title": "Other", "Selling Price (£)": 5.0, "OPC": "PXZZZ01"},
                     {"SKU": "111111111111", "Title": "Right Product Title", "Selling Price (£)": 67.05, "OPC": "PXOLD01"}]
        self.batches = []

    def get_all_records(self):
        return [dict(r) for r in self.rows]

    def row_values(self, n):
        return ["SKU", "Title", "Selling Price (£)", "OPC"]

    def col_values(self, n):
        return ["SKU"] + [str(r["SKU"]) for r in self.rows]

    def cell(self, row, col):
        return SimpleNamespace(value=self.rows[row - 2]["OPC"])

    def batch_update(self, data):
        self.batches.append(data)


def _setup(monkeypatch, onbuy, dry_run=False, relink="111111111111:PXNEW01", blocked=None, **flags):
    sheet = FakeSheet()
    monkeypatch.setenv("GOOGLE_CREDENTIALS", "{}")
    monkeypatch.setattr(rl, "OnBuyClient", lambda: onbuy)
    monkeypatch.setattr(rl, "ServiceAccountCredentials", SimpleNamespace(from_json_keyfile_dict=lambda *a, **k: None))
    monkeypatch.setattr(rl, "gspread", SimpleNamespace(authorize=lambda c: SimpleNamespace(open=lambda name: object())))
    monkeypatch.setattr(rl.sheet_tabs, "product_sheet", lambda book: sheet)
    monkeypatch.setattr(rl, "DRY_RUN", dry_run)
    monkeypatch.setattr(rl, "RELINK", relink)
    monkeypatch.setattr(rl, "BLOCKED_SKUS", set(blocked or ()))
    monkeypatch.setattr(rl, "ATTACH_WAIT", 0)
    monkeypatch.setattr(rl, "ATTACH_RETRIES", flags.get("retries", 5))
    monkeypatch.setattr(rl, "UPDATE_SHEET", flags.get("update_sheet", True))
    return sheet


OLD = {"111111111111": {"sku": "111111111111", "name": "Wrong Product", "price": "67.05", "stock": 0, "product_encoded_id": "PXOLD01"}}
QUEUE = [{"uid": "555", "status": "success", "opc": "PXQQQ01"}, {"uid": "111111111111", "status": "success", "opc": "PXNEW01", "queue_id": "q1"}]


def test_dry_run_reads_everything_and_writes_nothing(monkeypatch):
    onbuy = FakeOnBuy(OLD, QUEUE)
    sheet = _setup(monkeypatch, onbuy, dry_run=True)
    rl.main()
    assert set(onbuy.calls) == {"GET"} and sheet.batches == [] and "111111111111" in onbuy.listings


def test_live_relink_deletes_attaches_with_retries_verifies_and_writes_the_new_opc(monkeypatch):
    onbuy = FakeOnBuy(OLD, QUEUE, attach_fail_times=2, failure_shape="error", attach_error="SKU already exists")
    sheet = _setup(monkeypatch, onbuy)
    rl.main()
    assert onbuy.calls.count("DELETE") == 1 and onbuy.calls.count("POST") == 3          # two "SKU already exists" answers, then ok
    now = onbuy.listings["111111111111"]
    assert now["product_encoded_id"] == "PXNEW01" and now["stock"] == 0 and now["price"] == "67.05"
    assert sheet.batches == [[{"range": "D3", "values": [["PXNEW01"]]}]]                  # row 3, column D (OPC)


def test_live_relink_can_leave_the_sheet_alone(monkeypatch):
    onbuy = FakeOnBuy(OLD, QUEUE)
    sheet = _setup(monkeypatch, onbuy, update_sheet=False)
    rl.main()
    assert sheet.batches == [] and onbuy.listings["111111111111"]["product_encoded_id"] == "PXNEW01"


def test_a_listing_with_stock_is_never_deleted(monkeypatch):
    onbuy = FakeOnBuy({"111111111111": dict(OLD["111111111111"], stock=4)}, QUEUE)
    _setup(monkeypatch, onbuy)
    with pytest.raises(SystemExit):
        rl.main()
    assert "DELETE" not in onbuy.calls and "POST" not in onbuy.calls


def test_an_opc_the_queue_does_not_confirm_is_never_used(monkeypatch):
    onbuy = FakeOnBuy(OLD, [{"uid": "111111111111", "status": "success", "opc": "PXELSE9"}])
    _setup(monkeypatch, onbuy)
    with pytest.raises(SystemExit):
        rl.main()
    assert "DELETE" not in onbuy.calls


def test_a_blocked_sku_is_never_touched(monkeypatch):
    onbuy = FakeOnBuy(OLD, QUEUE)
    _setup(monkeypatch, onbuy, blocked=["111111111111"])
    with pytest.raises(SystemExit):
        rl.main()
    assert "DELETE" not in onbuy.calls


def test_a_refused_delete_stops_before_any_attach(monkeypatch):
    onbuy = FakeOnBuy(OLD, QUEUE, delete_error="Listing is suspended")
    _setup(monkeypatch, onbuy)
    with pytest.raises(SystemExit) as exc:
        rl.main()
    assert "delete refused" in str(exc.value) and "POST" not in onbuy.calls


def test_an_attach_that_never_works_says_the_sku_has_no_listing(monkeypatch):
    onbuy = FakeOnBuy(OLD, QUEUE, attach_forever=True, attach_error="Invalid price")
    sheet = _setup(monkeypatch, onbuy)
    with pytest.raises(SystemExit) as exc:
        rl.main()
    assert "NO listing" in str(exc.value) and sheet.batches == []
    assert onbuy.calls.count("POST") == 1                      # a non-retryable answer is not retried


def test_a_wrong_name_after_the_attach_stops_before_the_sheet_is_touched(monkeypatch):
    onbuy = FakeOnBuy(OLD, QUEUE, new_name="Some Other Product Entirely")
    sheet = _setup(monkeypatch, onbuy)
    with pytest.raises(SystemExit) as exc:
        rl.main()
    assert "problems" in str(exc.value) and sheet.batches == []


def test_two_skus_are_all_or_nothing_before_the_first_delete(monkeypatch):
    listings = dict(OLD)
    listings["222222222222"] = dict(OLD["111111111111"], sku="222222222222", stock=2)          # second one still sellable
    onbuy = FakeOnBuy(listings, QUEUE + [{"uid": "222222222222", "status": "success", "opc": "PXNEW02"}])
    sheet = _setup(monkeypatch, onbuy, relink="111111111111:PXNEW01,222222222222:PXNEW02")
    sheet.rows.append({"SKU": "222222222222", "Title": "Second Right", "Selling Price (£)": 9.0, "OPC": "PXOLD01"})
    with pytest.raises(SystemExit):
        rl.main()
    assert "DELETE" not in onbuy.calls                          # nothing deleted although the first SKU alone was fine


def test_item_failure_reads_the_real_answer_shapes():
    msg = "The SKU already exists for a different product"
    assert rl.item_failure({"success": False, "message": msg}) == msg
    assert rl.item_failure({"error": "SKU does not exist"}) == "SKU does not exist"
    assert rl.item_failure({"success": True, "sku": "1"}) == "" and rl.item_failure({"sku": "1"}) == "" and rl.item_failure(None) == ""
    assert rl.item_failure({"success": False}) != ""


def test_http_200_with_a_failed_item_is_never_taken_for_success(monkeypatch):
    # the bug of 2026-10-07: OnBuy answered HTTP 200 + {"success": false, "message": ...} for the item and the run went on to verify a listing that was not there
    onbuy = FakeOnBuy(OLD, QUEUE, attach_forever=True, attach_error="Invalid price")
    ok, text = rl.attach_listing(onbuy, "111111111111", "PXNEW01", 67.05, sleep=lambda s: None)
    assert not ok and "Invalid price" in text and onbuy.calls.count("POST") == 1


def test_a_deleted_listing_that_lingers_is_waited_for_then_attached(monkeypatch):
    onbuy = FakeOnBuy(OLD, QUEUE, delete_lag=3)                 # three attach attempts answer "already exists for a different product"
    sheet = _setup(monkeypatch, onbuy)
    rl.main()
    assert onbuy.calls.count("POST") == 4
    assert onbuy.listings["111111111111"]["product_encoded_id"] == "PXNEW01"
    assert sheet.batches == [[{"range": "D3", "values": [["PXNEW01"]]}]]


def test_attach_only_resumes_a_sku_whose_listing_is_already_gone(monkeypatch):
    onbuy = FakeOnBuy({}, QUEUE)
    sheet = _setup(monkeypatch, onbuy)
    monkeypatch.setattr(rl, "ATTACH_ONLY", True)
    rl.main()
    assert "DELETE" not in onbuy.calls and onbuy.calls.count("POST") == 1
    now = onbuy.listings["111111111111"]
    assert now["product_encoded_id"] == "PXNEW01" and now["stock"] == 0 and now["price"] == "67.05"       # price from the sheet row
    assert sheet.batches == [[{"range": "D3", "values": [["PXNEW01"]]}]]


def test_attach_only_refuses_a_sku_that_still_has_a_listing(monkeypatch):
    onbuy = FakeOnBuy(OLD, QUEUE)
    _setup(monkeypatch, onbuy)
    monkeypatch.setattr(rl, "ATTACH_ONLY", True)
    with pytest.raises(SystemExit):
        rl.main()
    assert "POST" not in onbuy.calls and "DELETE" not in onbuy.calls


def test_the_verify_waits_for_a_created_listing_that_is_not_readable_yet(monkeypatch):
    onbuy = FakeOnBuy(OLD, QUEUE)
    sheet = _setup(monkeypatch, onbuy)
    monkeypatch.setattr(rl, "VERIFY_WAIT", 0)
    real_send, reads = onbuy._send, {"n": 0}

    def lagging(method, url, **kw):
        if method == "GET" and "POST" in onbuy.calls:           # after the attach the first two reads still find nothing
            reads["n"] += 1
            if reads["n"] <= 2:
                return Resp(200, {"results": []})
        return real_send(method, url, **kw)
    onbuy._send = lagging
    rl.main()
    assert reads["n"] >= 3 and sheet.batches == [[{"range": "D3", "values": [["PXNEW01"]]}]]


def test_finish_only_verifies_and_records_without_touching_the_listing(monkeypatch):
    done = {"111111111111": {"sku": "111111111111", "name": "Right Product Title", "price": "67.05", "stock": 0, "product_encoded_id": "PXNEW01"}}
    onbuy = FakeOnBuy(done, QUEUE)
    sheet = _setup(monkeypatch, onbuy)
    monkeypatch.setattr(rl, "FINISH_ONLY", True)
    monkeypatch.setattr(rl, "VERIFY_WAIT", 0)
    rl.main()
    assert "DELETE" not in onbuy.calls and "POST" not in onbuy.calls
    assert sheet.batches == [[{"range": "D3", "values": [["PXNEW01"]]}]]


def test_finish_only_refuses_a_listing_that_is_not_on_the_stated_opc(monkeypatch):
    other = {"111111111111": {"sku": "111111111111", "name": "Wrong Product", "price": "67.05", "stock": 0, "product_encoded_id": "PXOLD01"}}
    onbuy = FakeOnBuy(other, QUEUE)
    sheet = _setup(monkeypatch, onbuy)
    monkeypatch.setattr(rl, "FINISH_ONLY", True)
    monkeypatch.setattr(rl, "VERIFY_RETRIES", 2)
    monkeypatch.setattr(rl, "VERIFY_WAIT", 0)
    with pytest.raises(SystemExit):
        rl.main()
    assert sheet.batches == []
