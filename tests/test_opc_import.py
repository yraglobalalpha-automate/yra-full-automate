"""OPC-seeded import (backfill_onbuy_status, 2026-09-14): the pure pieces.
A row with OPC + Supplier URL and no SKU imports an already-listed OnBuy
product; the SKU comes from the account's own listings. Extracted from
source like test_backfill_outcome (the module's deps aren't local)."""
import ast
from types import SimpleNamespace
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "backfill_onbuy_status.py"


def _extract(*names):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(fns) == len(names), f"missing one of {names}"
    ns = {}
    exec(compile(ast.Module(body=fns, type_ignores=[]), str(SRC), "exec"), ns)
    return SimpleNamespace(**{n: ns[n] for n in names})


bf = _extract("_listing_opc", "_import_candidates")


def test_listing_opc_field_spellings():
    assert bf._listing_opc({"opc": "ab12cd"}) == "AB12CD"
    assert bf._listing_opc({"product_opc": " x9 "}) == "X9"
    assert bf._listing_opc({"onbuy_product_code": "z1"}) == "Z1"
    assert bf._listing_opc({"product": {"opc": "nest1"}}) == "NEST1"
    assert bf._listing_opc({"product": {"code": "c0de"}}) == "C0DE"
    # opc wins over nested when both present
    assert bf._listing_opc({"opc": "top", "product": {"opc": "nest"}}) == "TOP"
    assert bf._listing_opc({"sku": "123"}) == ""
    assert bf._listing_opc({}) == ""
    assert bf._listing_opc(None) == ""


def _row(sku="", opc="", url=""):
    return {"SKU": sku, "OPC": opc, "Supplier URL": url}


def test_import_candidates_rules():
    data = [
        _row(opc="PX1", url="https://x"),          # 0: candidate
        _row(sku="123", opc="PX2", url="https://x"),  # has SKU -> no
        _row(opc="PENDING", url="https://x"),      # backfill marker -> no
        _row(opc="PX3"),                           # no URL -> no
        _row(url="https://x"),                     # no OPC -> no
        _row(opc="px4", url="https://x"),          # 5: candidate, upper-cased
        _row(sku="1,234", opc="PX5", url="https://x"),  # comma-SKU is a SKU -> no
    ]
    assert bf._import_candidates(data) == [(0, "PX1"), (5, "PX4")]


def test_import_candidates_empty():
    assert bf._import_candidates([]) == []
    assert bf._import_candidates([_row()]) == []
