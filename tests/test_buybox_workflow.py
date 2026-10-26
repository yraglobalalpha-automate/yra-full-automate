"""buybox_defense.yml is wired to the repository variables the dashboard's Buy Box card sets: the daily run can be switched off, the undercut and the floor's profit % come from
variables (a manual run's own input wins), and the defaults stay what they were (1 penny, 15 %)."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / ".github" / "workflows" / "buybox_defense.yml").read_text(encoding="utf-8")
DOC = yaml.safe_load(TEXT)


def test_the_workflow_takes_a_trial_profit_percent_and_empty_defaults_so_the_variables_apply():
    inputs = DOC[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"dry_run", "undercut_pence", "profit_percent", "check_batch"}
    assert inputs["undercut_pence"]["default"] == "" and inputs["profit_percent"]["default"] == "" and inputs["dry_run"]["default"] == "yes"


def test_the_variables_feed_the_engine_with_the_old_defaults_behind_them():
    assert "UNDERCUT_PENCE: ${{ inputs.undercut_pence || vars.BUYBOX_DEFENSE_UNDERCUT_PENCE || '1' }}" in TEXT
    assert "DEFENSE_PROFIT_PERCENT: ${{ inputs.profit_percent || vars.BUYBOX_DEFENSE_PROFIT_PERCENT }}" in TEXT
    engine = (ROOT / "buybox_defense.py").read_text(encoding="utf-8")
    assert 'os.getenv("UNDERCUT_PENCE") or "1"' in engine and 'os.getenv("DEFENSE_PROFIT_PERCENT") or "15"' in engine


def test_only_the_daily_run_can_be_switched_off_and_it_still_ends_green():
    steps = DOC["jobs"]["defend"]["steps"]
    names = [s.get("name") for s in steps]
    off, defend = steps[names.index("Switched off")], steps[names.index("Defend Buy Box")]
    assert off["if"] == "github.event_name == 'schedule' && vars.BUYBOX_DEFENSE_ENABLED == 'no'" and "echo" in off["run"]
    assert defend["if"] == "${{ !(github.event_name == 'schedule' && vars.BUYBOX_DEFENSE_ENABLED == 'no') }}" and names.index("Switched off") < names.index("Defend Buy Box")
    assert "schedule" in DOC[True] and DOC["concurrency"]["group"] == "onbuy-api"
