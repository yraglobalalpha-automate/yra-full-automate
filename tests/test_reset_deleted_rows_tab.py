"""reset_deleted_rows picks its worksheet by NAME: the tab named in SHEET_TAB, else the main product tab (never book.sheet1), and refuses the audited default list on a named tab."""
from pathlib import Path

import pytest
import yaml

import reset_deleted_rows as rdr

ROOT = Path(__file__).resolve().parents[1]


class Book:
    def __init__(self):
        self.asked = []

    def worksheet(self, name):
        self.asked.append(name)
        return f"ws:{name}"


def test_a_named_tab_is_opened_by_name():
    book = Book()
    assert rdr.open_tab(book, "Amazon") == "ws:Amazon" and book.asked == ["Amazon"]


def test_no_tab_means_the_main_product_tab_by_name():
    book = Book()
    assert rdr.open_tab(book, "") == "ws:Sheet1" and book.asked == ["Sheet1"]


def test_a_named_tab_never_falls_back_to_the_audited_default_list(monkeypatch):
    monkeypatch.setattr(rdr, "SHEET_TAB", "Amazon")
    monkeypatch.delenv("RESET_SKUS", raising=False)
    with pytest.raises(SystemExit) as stop:
        rdr.main()
    assert "explicit SKU list" in str(stop.value)


def test_the_workflow_passes_the_tab_and_keeps_the_dry_run_default():
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "reset_deleted_rows.yml").read_text(encoding="utf-8"))
    inputs = doc[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"dry_run", "reset_skus", "tab"} and inputs["dry_run"]["default"] == "yes" and inputs["tab"]["default"] == ""
    step = next(s for s in doc["jobs"]["probe"]["steps"] if s.get("name") == "Probe live listings")
    assert step["env"]["SHEET_TAB"] == "${{ inputs.tab }}" and step["env"]["RESET_SKUS"] == "${{ inputs.reset_skus }}"
