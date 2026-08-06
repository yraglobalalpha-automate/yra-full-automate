"""Decision table for backfill_onbuy_status.outcome_for - pins the exact
regression that poisoned 1,396 rows on 2026-08-06: a still-pending queue
entry must NEVER produce a Failed write. The script runs on import, so
outcome_for is extracted from source instead of imported."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "backfill_onbuy_status.py"


def _outcome_for():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "outcome_for")
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
    return ns["outcome_for"]


outcome_for = _outcome_for()


def test_success_returns_synced_with_opc_and_url():
    kind, opc, url, status = outcome_for(
        {"status": "success", "opc": "PXNQ2WX", "product_url": "https://x/p"})
    assert (kind, opc, url, status) == ("synced", "PXNQ2WX", "https://x/p", "Synced")


def test_failed_with_reason_keeps_onbuys_reason():
    kind, opc, url, status = outcome_for(
        {"status": "failed", "error_message": "An error occurred. Please try again."})
    assert kind == "failed"
    assert opc is None and url is None
    assert status == "Failed: An error occurred. Please try again."


def test_failed_with_empty_reason_uses_the_no_reason_text():
    kind, _, _, status = outcome_for({"status": "failed", "error_message": ""})
    assert kind == "failed"
    assert status == "Failed: rejected with no reason given by OnBuy"


def test_pending_writes_nothing():
    # THE 2026-08-06 regression: pending fell through to the failure WRITE.
    assert outcome_for({"status": "pending"}) == ("pending", None, None, None)


def test_unknown_status_counts_as_pending():
    assert outcome_for({"status": "processing"}) == ("pending", None, None, None)
    assert outcome_for({"status": None}) == ("pending", None, None, None)
    assert outcome_for({}) == ("pending", None, None, None)


def test_no_status_value_ever_yields_a_failed_write_except_failed():
    for status in ("pending", "queued", "", None, "SUCCESS", "Success", 1, True):
        kind, _, _, sync_status = outcome_for({"status": status})
        assert kind == "pending", f"status {status!r} must not write (got {kind})"
        assert sync_status is None
