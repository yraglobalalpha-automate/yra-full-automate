"""category_dropdown: the rule is the one the main tab has (strict dropdown from the OnBuy Categories tab), set on the whole column from row 2 with no end row; the workflow only reports unless asked."""
from pathlib import Path

import yaml

import category_dropdown as cd

ROOT = Path(__file__).resolve().parents[1]


def test_the_rule_is_the_main_tabs_strict_dropdown_from_the_categories_tab():
    rule = cd.dropdown_rule()
    assert rule["condition"]["type"] == "ONE_OF_RANGE" and rule["strict"] is True and rule["showCustomUi"] is True
    assert rule["condition"]["values"] == [{"userEnteredValue": "='OnBuy Categories'!$A$1:$A$10000"}]


def test_the_request_covers_the_column_from_row_two_with_no_end_row_so_new_rows_get_it():
    request = cd.set_request(1462972385, 5)["setDataValidation"]
    assert request["range"] == {"sheetId": 1462972385, "startRowIndex": 1, "startColumnIndex": 5, "endColumnIndex": 6}
    assert "endRowIndex" not in request["range"] and request["rule"] == cd.dropdown_rule()


def test_column_letters():
    assert [cd.column_letter(n) for n in (1, 6, 26, 27, 31, 40)] == ["A", "F", "Z", "AA", "AE", "AN"]


def test_describe_tells_a_missing_dropdown_from_a_set_one():
    assert cd.describe(None) == "no dropdown"
    assert "ONE_OF_RANGE" in cd.describe(cd.dropdown_rule()) and "strict True" in cd.describe(cd.dropdown_rule())
    assert cd.describe(cd.dropdown_rule()) == cd.describe(cd.dropdown_rule(10000)) != cd.describe(cd.dropdown_rule(6574))


def test_the_workflow_reports_unless_asked_and_touches_only_the_sheet():
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "category_dropdown.yml").read_text(encoding="utf-8"))
    inputs = doc[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"tab", "column", "dry_run"} and inputs["dry_run"]["default"] == "yes" and inputs["tab"]["default"] == "Amazon" and inputs["column"]["default"] == "Category"
    step = next(s for s in doc["jobs"]["dropdown"]["steps"] if s.get("name") == "Category dropdown")
    assert step["env"]["DRY_RUN"] == "${{ inputs.dry_run == 'no' && '0' || '1' }}" and step["run"] == "python category_dropdown.py"
    assert set(step["env"]) == {"GOOGLE_CREDENTIALS", "SHEET_TAB", "COLUMN", "DRY_RUN"}                          # no OnBuy or Keepa secret
