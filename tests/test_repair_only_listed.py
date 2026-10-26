"""repair_shifted_content.py can be told to resubmit ONLY the SKUs named (the dashboard's Pictures page resubmits exactly the stuck list and nothing else), and is a dry run by default."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "repair_shifted_content.yml").read_text(encoding="utf-8")
SCRIPT = (ROOT / "repair_shifted_content.py").read_text(encoding="utf-8")


def test_the_workflow_is_a_dry_run_by_default_and_passes_the_named_skus_the_limit_the_tab_and_only_listed_on():
    assert "dry_run:" in WORKFLOW and 'default: "yes"' in WORKFLOW and "DRY_RUN: ${{ inputs.dry_run == 'no' && '0' || '1' }}" in WORKFLOW
    assert "REPAIR_SKUS: ${{ inputs.repair_skus }}" in WORKFLOW and "REPAIR_LIMIT: ${{ inputs.repair_limit }}" in WORKFLOW and "SHEET_TAB: ${{ inputs.tab }}" in WORKFLOW
    assert "only_listed:" in WORKFLOW and "REPAIR_ONLY_LISTED: ${{ inputs.only_listed == 'yes' && '1' || '0' }}" in WORKFLOW and "group: onbuy-api" in WORKFLOW


def test_only_listed_makes_the_script_skip_every_other_shifted_listing():
    assert 'REPAIR_ONLY_LISTED = (os.getenv("REPAIR_ONLY_LISTED") or "").strip().lower() in ("1", "yes", "true")' in SCRIPT
    assert "if REPAIR_ONLY_LISTED and sku not in REPAIR_SKUS:" in SCRIPT
    assert SCRIPT.index("if REPAIR_ONLY_LISTED and sku not in REPAIR_SKUS:") < SCRIPT.index("if sku not in REPAIR_SKUS and similar(listings[sku], title) >= 0.5:")


def test_the_log_lines_the_dashboard_reads_are_still_printed():
    for line in ("shift-repairable: %d (skipped incomplete: %d) | barcode-collision (report-only): %d", "  target %s (onbuy shows: %s)", "DRY RUN - nothing submitted",
                 "REPAIR %s: submitted OK", "REPAIR %s: failed - %s", "DONE: %d submitted, %d failed"):
        assert line in SCRIPT, line
