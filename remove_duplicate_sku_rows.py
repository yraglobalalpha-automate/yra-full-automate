"""Remove duplicate-SKU ROWS from the Amazon tab (2026-09-17 incident):
a block re-pasted from ~row 1165 repeated existing SKUs with shifted
links, and those rows pushed wrong products' prices onto the original
SKUs' live listings (two SKUs both live at GBP 97.41). For every SKU
appearing on more than one row of the tab, the FIRST row is kept and
every later row is deleted - row-precise (deleteDimension, descending),
never by SKU, so the original rows and their Supabase records survive.
DRY_RUN default on; BOUNDARY prints how the duplicates split around a
suspected paste row.
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from retry_utils import with_retry

SHEET_NAME = "YRA_Full_Feed_Master"
TAB = (os.getenv("SHEET_TAB") or "Amazon").strip()
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
BOUNDARY = int(os.getenv("BOUNDARY") or "1165")


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    tab = book.worksheet(TAB)
    data = tab.get_all_records()
    by_sku = {}
    for idx, row in enumerate(data):
        sku = str(row.get("SKU") or "").replace(",", "").strip()
        if sku:
            by_sku.setdefault(sku, []).append((idx + 2, str(row.get("Title") or "")[:40],
                                               str(row.get("Supplier URL") or "")[:60]))
    dup_rows = []
    for sku, rows in by_sku.items():
        if len(rows) > 1:
            for n, title, url in rows[1:]:
                dup_rows.append((n, sku, title, url, rows[0][0]))
    dup_rows.sort()
    above = sum(1 for n, *_ in dup_rows if n >= BOUNDARY)
    print(f"Tab '{TAB}': {len(data)} data rows | duplicated SKUs: "
          f"{sum(1 for r in by_sku.values() if len(r) > 1)} | duplicate ROWS to delete: {len(dup_rows)}")
    print(f"of them at/after row {BOUNDARY}: {above} | before it: {len(dup_rows) - above}")
    for n, sku, title, url, first in dup_rows[:40]:
        print(f"  row {n}: SKU {sku} (original at row {first}) {title!r} {url}")
    if len(dup_rows) > 40:
        print(f"  ... and {len(dup_rows) - 40} more")

    if DRY_RUN:
        print("\nDRY RUN - nothing deleted.")
        return
    if not dup_rows:
        print("No duplicate rows - done.")
        return
    ordered = sorted({n for n, *_ in dup_rows}, reverse=True)
    for c in range(0, len(ordered), 200):
        chunk = ordered[c:c + 200]
        reqs = [{"deleteDimension": {"range": {
            "sheetId": tab.id, "dimension": "ROWS",
            "startIndex": n - 1, "endIndex": n}}} for n in chunk]
        tab.spreadsheet.batch_update({"requests": reqs})
    print(f"\nDELETED {len(ordered)} duplicate row(s) - originals kept, Supabase untouched "
          "(the kept rows own those SKUs).")


if __name__ == "__main__":
    main()
