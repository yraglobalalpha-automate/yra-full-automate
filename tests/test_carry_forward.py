"""carry_forward: the fix for the 22P02 batch-loss bug (GTV, 2026-08-04;
fleet-wide port 2026-08-06). Extracted from source so no gspread import is
needed - same pattern as test_backfill_outcome."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "generate_xml.py"


def _carry_forward():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "carry_forward")
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
    return ns["carry_forward"]


carry_forward = _carry_forward()


def test_fresh_value_wins():
    assert carry_forward("TRUE", False, "FALSE") == "TRUE"
    assert carry_forward("FALSE", True, "FALSE") == "FALSE"


def test_stored_false_is_preserved():
    # THE regression `or` caused: a stored boolean False is falsy but
    # perfectly valid - it must carry forward, never fall to the default.
    got = carry_forward(None, False, "FALSE")
    assert got is False


def test_stored_true_carries_forward():
    assert carry_forward(None, True, "FALSE") is True


def test_never_pushed_row_gets_the_default():
    assert carry_forward(None, None, "FALSE") == "FALSE"


def test_timestamp_default_is_null_not_empty_string():
    # "" into a timestamp column is 22007; NULL is the truthful value for
    # a sync that never happened.
    assert carry_forward(None, None) is None


def test_stored_timestamp_carries_forward():
    assert carry_forward(None, "2026-08-04 06:37:01") == "2026-08-04 06:37:01"
