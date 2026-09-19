"""Deletion reconciler (2026-09-19, security change #3): pure helpers,
extracted via AST (module imports gspread, unavailable locally)."""
import ast
import io
import os
from datetime import timezone

SRC = io.open(os.path.join(os.path.dirname(__file__), "..", "deletion_reconciler.py"),
              encoding="utf-8").read()


def _extract(*names):
    tree = ast.parse(SRC)
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    mod = ast.Module(body=keep, type_ignores=[])
    import datetime as _dt
    ns = {"datetime": _dt.datetime, "timezone": timezone, "TS": "%Y-%m-%d %H:%M"}
    exec(compile(mod, "reconciler_extract", "exec"), ns)
    return ns


NS = _extract("_zeroless", "_parse_ts")


def test_zeroless_strips_leading_zeros_only():
    z = NS["_zeroless"]
    assert z("0165379473838") == "165379473838"
    assert z("165379473838") == "165379473838"
    assert z("000") == "0"
    assert z("Ama-B07-30") == "Ama-B07-30"


def test_parse_ts_roundtrip_and_garbage():
    p = NS["_parse_ts"]
    t = p("2026-09-19 10:00")
    assert t is not None and t.tzinfo is not None
    assert p("") is None and p("not a date") is None


def test_grace_and_caps_in_source():
    assert "DELIST_GRACE_HOURS" in SRC and "DELIST_MAX_DELETES_PER_RUN" in SRC
    assert "never touched" in SRC.lower() or "no supabase record" in SRC.lower()
