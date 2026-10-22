"""One-off: correct a single cell on a single row, safely.

For the small data faults that stop a row dead and that no amount of
retrying can clear - a category leaf that does not exist in OnBuy's tree,
a brand spelled in a way their matcher chokes on, a category the matcher
got wrong. Addressed BY ROW NUMBER with the expected SKU as a guard: row
numbers move whenever the team inserts or deletes, so writing to a row
number alone risks correcting the wrong product's cell. If the SKU on that
row is not the one the caller expects, nothing is written.

SHEET_TAB picks the worksheet (blank = the first tab, "Amazon" = the
Amazon tab). Prints the old and new value and refuses a no-op, so the run
log is the audit trail. Touches no other column and no OnBuy API.
(Ported from the Arden repo to GTV 2026-09-09, and to YRA 2026-10-22.)
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
SHEET_TAB = (os.getenv("SHEET_TAB") or "").strip()
ROW = int(os.getenv("ROW") or "0")
EXPECT_SKU = (os.getenv("EXPECT_SKU") or "").strip()
COLUMN = (os.getenv("COLUMN") or "").strip()
VALUE = os.getenv("VALUE")
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")


def col_letter(n):
    s = ""
    while n >= 0:
        s = chr(n % 26 + 65) + s
        n = n // 26 - 1
    return s


def main():
    if not ROW or not COLUMN or VALUE is None:
        raise SystemExit("ROW, COLUMN and VALUE are all required")

    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds",
                     "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(SHEET_NAME)
    sheet = book.worksheet(SHEET_TAB) if SHEET_TAB else book.sheet1
    print(f"worksheet: {sheet.title}")
    values = sheet.get_all_values()
    headers = [str(h).strip() for h in values[0]]
    col_map = {h: i for i, h in enumerate(headers)}

    if COLUMN not in col_map:
        raise SystemExit(f"no {COLUMN!r} column - sheet has: {', '.join(headers[:14])}...")
    if not 2 <= ROW <= len(values):
        raise SystemExit(f"row {ROW} is outside the sheet (rows 2..{len(values)})")

    row = values[ROW - 1]

    def cell(name):
        i = col_map.get(name)
        return (row[i] if i is not None and i < len(row) else "").strip()

    sku, title = cell("SKU").replace(",", ""), cell("Title")
    print(f"row {ROW}: SKU {sku!r} | title {title[:64]!r}")
    if EXPECT_SKU and sku != EXPECT_SKU:
        raise SystemExit(f"ABORT: expected SKU {EXPECT_SKU!r}, found {sku!r} - rows have moved")

    old = cell(COLUMN)
    print(f"  {COLUMN} old: {old!r}")
    print(f"  {COLUMN} new: {VALUE!r}")
    if old == VALUE:
        print("already correct - nothing to do")
        return
    if DRY_RUN:
        print("DRY RUN - nothing written")
        return

    sheet.update_acell(f"{col_letter(col_map[COLUMN])}{ROW}", VALUE)
    print(f"WROTE row {ROW} {COLUMN}")


if __name__ == "__main__":
    main()
