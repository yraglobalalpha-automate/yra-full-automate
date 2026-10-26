"""image_links_export is read-only and writes the files the dashboard's Pictures page reads from the artifact `image-links`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import image_links_export as ile

ROOT = Path(__file__).resolve().parents[1]


def test_sku_keys_ignore_leading_zeros_and_commas():
    assert ile.norm("0123") == "123" and ile.norm(" 1,234 ") == "1234" and ile.norm("000") == "000" and ile.norm(None) == ""
    assert ile.norm("Ama-B0ABC12345-3") == "Ama-B0ABC12345-3"


def test_the_workflow_reads_only_the_sheet_and_uploads_the_artifact_the_dashboard_downloads():
    text = (ROOT / ".github" / "workflows" / "image_links_export.yml").read_text(encoding="utf-8")
    assert "sku_file:" in text and "SKU_FILE: ${{ inputs.sku_file }}" in text and "python image_links_export.py" in text
    assert "name: image-links" in text and "path: out/" in text and "GOOGLE_CREDENTIALS: ${{ secrets.GOOGLE_CREDENTIALS }}" in text
    assert "ONBUY_" not in text and "SUPABASE" not in text and "concurrency" not in text          # no OnBuy call, so it never waits for the store's slot


def test_the_script_writes_exactly_the_files_the_dashboard_reads():
    src = (ROOT / "image_links_export.py").read_text(encoding="utf-8")
    for name in ("ticket_image_links.csv", "reference.csv", "no_opc.csv", "duplicates.csv", "all_found_images.csv", "not_on_sheet.txt"):
        assert name in src, name
    for header in ('"OPC", "SKU", "Main image link", "Additional image links"', '"SKU", "Tab", "Row", "OPC", "Sync Status", "OnBuy Product Created", "Product URL", "Title"',
                   '"SKU", "Tab", "Row", "OPC", "Sync Status", "Main image link", "Additional image links"'):
        assert header in src, header
