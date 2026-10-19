"""revert_fee_fallback.py - only the automation's own prices carrying the flat-fee signature are put back on the formula."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("FEE_MODE", "category")

import pricing  # noqa: E402
import revert_fee_fallback as rf  # noqa: E402


def d(how="follows", existing=None, selling=None, cost=20.0, ship=0.0, band=40, profit_override=None, fee_override=None, rule=None):
    return {"how": how, "existing": existing, "selling_price": selling, "cost": cost, "ship": ship, "band_now": band,
            "profit_override": profit_override, "fee_override": fee_override, "rule": rule}


def test_the_fallback_signature_is_the_24_cell_or_the_flat_fee_price():
    flat = pricing.price_for_profit(20.0, 40, platform_fee_percent=20)           # 20 x 1.4 / 0.76 = 36.84
    assert rf.fallback_signature(d(existing=flat), "18.00") == "flat price"
    assert rf.fallback_signature(d(existing=30.0), "24.00") == "fee cell"
    assert rf.fallback_signature(d(existing=30.0), "24") == "fee cell"
    assert rf.fallback_signature(d(existing=30.0), "18.00") == ""
    assert rf.fallback_signature(d(existing=30.0, cost=0), "") == ""


def test_plan_reverts_only_the_automations_own_price_with_the_signature_and_a_different_decided_price():
    assert rf.plan_row(d(existing=36.84, selling=34.15), "24.00") == ("revert", "fee cell")
    assert rf.plan_row(d(how="amazon", existing=36.84, selling=34.15), "24.00") == ("revert", "fee cell")
    assert rf.plan_row(d(how="kept", existing=36.84, selling=36.84), "24.00")[0] == "skip"         # a price the sync keeps (manual)
    assert rf.plan_row(d(how="manual", existing=50.0, selling=34.15), "24.00")[0] == "skip"
    assert rf.plan_row(d(existing=36.84, selling=34.15, profit_override=30.0), "24.00") == ("skip", "typed Profit % / Fee % override")
    assert rf.plan_row(d(existing=36.84, selling=34.15), "24.00", frozen=True) == ("skip", "frozen wrong-content listing")
    assert rf.plan_row(None, "24.00") == ("skip", "no cost")
    assert rf.plan_row(d(existing=34.15, selling=34.15), "24.00") == ("ok", "already on the formula")
    assert rf.plan_row(d(existing=36.84, selling=34.15), "18.00") == ("ok", "no fallback signature")   # not the flat price either


def test_new_cells_are_what_the_sync_writes():
    cells = rf.new_cells(d(selling=34.149, band=40, rule=None), stock=3)
    assert cells["Selling Price (£)"] == 34.15 and cells["Price Check Flag"] == "Normal" and cells["Fee %"] == f"{pricing.effective_fee_percent(34.15, None):.2f}"
    assert rf.new_cells(d(selling=10.0, band=80, rule=None), stock=0)["Price Check Flag"] == ""


def test_the_workflow_is_a_dry_run_by_default():
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "revert_fee_fallback.yml").read_text(encoding="utf-8")
    assert 'default: "no"' in text and "python -u revert_fee_fallback.py" in text and "SUPABASE_URL" in text
