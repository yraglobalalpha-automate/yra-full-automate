"""The backfill fills a blank OPC on a row the sync already marked Synced, from the account's listing sweep (2026-10-24)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backfill_onbuy_status as bf  # noqa: E402


class FakeSheet:
    def __init__(self, title, sku_column):
        self.title = title
        self._skus = sku_column
        self.written = []

    def col_values(self, _col):
        return list(self._skus)

    def batch_update(self, updates):
        self.written.extend(updates)


def tab(rows, title="Amazon"):
    col_map = {"SKU": 1, "OPC": 2, "Sync Status": 3}
    sku_display = ["SKU"] + [r["SKU"] for r in rows]
    return {"sheet": FakeSheet(title, sku_display), "col_map": col_map, "sku_display": sku_display, "data": rows, "updates": []}


ROWS = [
    {"SKU": "735719813442", "OPC": "", "Sync Status": "Synced"},                   # the case: Synced, no OPC, OnBuy knows it
    {"SKU": "735739982043", "OPC": "PENDING", "Sync Status": "Synced"},            # the backfill's own marker counts as blank
    {"SKU": "111", "OPC": "", "Sync Status": "Pending Approval"},                   # not Synced: the queue check owns it
    {"SKU": "222", "OPC": "PYQ1", "Sync Status": "Synced"},                        # already has its OPC
    {"SKU": "333", "OPC": "", "Sync Status": "Synced"},                             # Synced but OnBuy does not list it
    {"SKU": "", "OPC": "", "Sync Status": "Synced"},                                # no SKU: never
]


def test_candidates_are_synced_rows_without_an_opc():
    assert bf._blank_opc_candidates(ROWS) == [(0, "735719813442"), (1, "735739982043"), (4, "333")]


def test_plan_writes_only_the_opc_cell_of_rows_onbuy_lists():
    t = tab(ROWS)
    updates, filled, unknown = bf.plan_opc_fills(t, {"735719813442": "PYQSBCN", "735739982043": "PYQSBCM"})
    assert updates == [{"range": "B2", "values": [["PYQSBCN"]]}, {"range": "B3", "values": [["PYQSBCM"]]}]
    assert filled == 2 and unknown == 1
    capped, filled, _ = bf.plan_opc_fills(t, {"735719813442": "PYQSBCN", "735739982043": "PYQSBCM"}, limit=1)
    assert len(capped) == 1 and filled == 1
    assert bf.plan_opc_fills({"col_map": {"SKU": 1}, "data": ROWS}, {}) == ([], 0, 0)


def test_fill_sweeps_once_writes_the_cells_and_skips_a_sku_on_two_products(monkeypatch):
    calls = []
    monkeypatch.setattr(bf, "fetch_opc_sku_map", lambda onbuy: (calls.append(1) or {"PYQSBCN": "735719813442", "PYQA": "333", "PYQB": "333"}))
    t = tab(ROWS)
    returned = bf.fill_blank_opcs([t], onbuy=object())
    assert calls == [1] and returned == {"PYQSBCN": "735719813442", "PYQA": "333", "PYQB": "333"}
    assert t["sheet"].written == [{"range": "B2", "values": [["PYQSBCN"]]}]     # 333 sits on two products: left to a person


def test_no_candidates_means_no_sweep(monkeypatch):
    monkeypatch.setattr(bf, "fetch_opc_sku_map", lambda onbuy: (_ for _ in ()).throw(AssertionError("must not sweep")))
    t = tab([{"SKU": "222", "OPC": "PYQ1", "Sync Status": "Synced"}])
    assert bf.fill_blank_opcs([t], onbuy=object()) is None and t["sheet"].written == []


def test_a_failed_sweep_writes_nothing(monkeypatch):
    monkeypatch.setattr(bf, "fetch_opc_sku_map", lambda onbuy: (_ for _ in ()).throw(RuntimeError("503")))
    t = tab(ROWS)
    assert bf.fill_blank_opcs([t], onbuy=object()) is None and t["sheet"].written == []


def test_the_write_follows_a_row_that_moved():
    t = tab(ROWS)
    # the sheet gained a row at the top after the read: every SKU is one row lower now
    t["sheet"]._skus = ["SKU", "new-row"] + [r["SKU"] for r in ROWS]
    bf.fill_blank_opcs([t], onbuy=object(), opc_to_sku={"PYQSBCN": "735719813442"})
    assert t["sheet"].written == [{"range": "B3", "values": [["PYQSBCN"]]}]
