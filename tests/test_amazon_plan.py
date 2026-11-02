"""The dynamic Amazon-sync schedule (2026-11-02): the plan for one account from its product count and its share of a Keepa key, and the
state test that launches a run. The four live accounts' real sizes on 2026-11-02 are the fixtures."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import amazon_plan as ap

NOW = datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc).timestamp()


def iso(hours_ago):
    return datetime.fromtimestamp(NOW - hours_ago * 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_the_four_live_accounts_on_two_shared_keys():
    kidoomi = ap.plan(408)                         # a small tab: every two hours, the whole tab each run
    assert (kidoomi["runs"], kidoomi["slice"], kidoomi["cycles"], kidoomi["limited_by"]) == (12, 408, 12.0, "cadence")
    assert kidoomi["interval_h"] == 2.0 and kidoomi["daily"] == 4896 and kidoomi["tokens_day"] == 6120

    yra = ap.plan(1805)                            # a middling tab: the budget decides, every 4 hours
    assert (yra["runs"], yra["limited_by"]) == (6, "budget") and yra["slice"] == 1632 and 5.3 < yra["cycles"] < 5.5

    ardwick, gtv = ap.plan(2719), ap.plan(5220)    # big tabs are cut into slices of the same size: the budget is the same
    assert ardwick["runs"] == gtv["runs"] == 6 and ardwick["slice"] == gtv["slice"] == 1632
    assert ardwick["cycles"] > gtv["cycles"] and gtv["cycles"] > 1.0 and not gtv["warnings"]
    assert gtv["capacity"] == 9792 and gtv["share"] == 12240


def test_more_products_means_fewer_passes_and_a_loud_warning_when_the_share_cannot_cover_one():
    big = ap.plan(20000)
    assert big["cycles"] < 1.0 and big["runs"] == 6 and big["slice"] == 1632
    assert "only every" in big["warnings"][0] and "20,000" in big["warnings"][0]
    assert ap.plan(100)["slice"] == 100 and ap.plan(100)["runs"] == 12      # a tiny tab runs at the top cadence, whole tab each time


def test_a_key_to_itself_gets_the_whole_budget_and_a_bigger_plan_is_cut_by_the_run_size_cap():
    alone = ap.plan(5220, stores_on_key=1)
    assert alone["share"] == 24480 and alone["runs"] == 12 and alone["slice"] == 1632
    rich = ap.plan(3000, tokens_per_day=100000, stores_on_key=1)           # an upgraded plan: the cap on one run now binds
    assert rich["runs"] == 12 and rich["slice"] == 2275 and rich["limited_by"] == "run size"
    assert "run-size cap" in rich["warnings"][0] and rich["daily"] == 27300


def test_no_products_means_no_runs():
    out = ap.plan(0)
    assert out["runs"] == 0 and out["daily"] == 0 and out["limited_by"] == "no products"


def test_busy_counts_running_and_waiting_runs_but_not_ghosts():
    assert ap.is_busy([{"status": "in_progress", "created_at": iso(1.0)}], NOW)
    assert ap.is_busy([{"status": "queued", "created_at": iso(0.2)}], NOW)
    assert not ap.is_busy([{"status": "queued", "created_at": iso(60)}], NOW)          # pending for days = a ghost (house rule)
    assert not ap.is_busy([{"status": "completed", "created_at": iso(0.1)}], NOW)


def test_the_last_planned_run_ignores_a_humans_targeted_run_and_cancelled_ones():
    runs = [{"status": "completed", "conclusion": "success", "event": "workflow_dispatch", "display_title": "Amazon Sync (Keepa)", "created_at": iso(0.5)},
            {"status": "completed", "conclusion": "cancelled", "event": "schedule", "display_title": "Amazon Sync (Keepa)", "created_at": iso(1.0)},
            {"status": "completed", "conclusion": "success", "event": "workflow_dispatch", "display_title": "Amazon Sync (Keepa) · plan", "created_at": iso(3.0)},
            {"status": "completed", "conclusion": "success", "event": "schedule", "display_title": "Amazon Sync (Keepa)", "created_at": iso(9.0)}]
    assert round((NOW - ap.last_planned(runs)) / 3600, 1) == 3.0                       # the dispatcher's own run, not the hand run 0.5 h ago
    assert ap.last_planned([runs[0], runs[1]]) is None


def test_due_when_the_interval_has_passed_and_never_into_a_busy_slot():
    assert ap.decide(NOW, NOW - 4.1 * 3600, False, 4.0)[0]
    assert ap.decide(NOW, NOW - 3.7 * 3600, False, 4.0)[0]                              # inside the 24-minute tolerance
    assert not ap.decide(NOW, NOW - 3.0 * 3600, False, 4.0)[0]
    assert ap.decide(NOW, None, False, 4.0) == (True, "no earlier planned sync on record")
    assert ap.decide(NOW, NOW - 9 * 3600, True, 4.0)[0] is False                        # busy beats overdue
    assert ap.decide(NOW, NOW - 0.1 * 3600, False, 4.0, force=True)[0]                  # force skips the interval only
    assert ap.decide(NOW, NOW - 9 * 3600, True, 4.0, force=True)[0] is False


def test_the_log_line_is_one_pipe_separated_record_without_stray_pipes():
    info = ap.plan(2719)
    text = ap.line(info, True, "4.2 h since the last sync | the plan wants one every 4 h", True, elapsed_h=4.2)
    assert text.startswith("AMAZON_PLAN|n=2719|runs=6|slice=1632|") and "due=yes|dispatched=yes" in text
    assert text.count("|reason=") == 1 and "since the last sync / the plan" in text
