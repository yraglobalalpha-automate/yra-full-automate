"""One-off: make sure each worksheet has a "Fee %" header.

The sync writes every row's effective OnBuy commission to the "Fee %"
column when the header exists (category-fee feature, 2026-09-09) and
silently skips it otherwise - the sheets never had the column, only the
Supabase mirror did. This appends the header to the first empty cell of
row 1 on each tab named in SHEET_TABS (first tab always included), and
touches nothing else; the sync then fills the values as rows rotate.
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from generate_xml import col_letter

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
TABS = [t.strip() for t in (os.getenv("SHEET_TABS") or "Amazon").split(",") if t.strip()]
COLUMNS = [c.strip() for c in (os.getenv("COLUMNS") or "Fee %,Profit %").split(",") if c.strip()]
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(SHEET_NAME)
    titles = [w.title for w in book.worksheets()]
    sheets = [book.sheet1] + [book.worksheet(t) for t in TABS if t in titles and t != book.sheet1.title]
    for ws in sheets:
        headers = [str(h).strip() for h in ws.row_values(1)]
        target = len(headers) + 1          # first cell after the last filled header
        for column in COLUMNS:
            if column in headers:
                print(f"[{ws.title}] already has {column!r} (column {headers.index(column) + 1})")
                continue
            cell = f"{col_letter(target)}1"
            print(f"[{ws.title}] would write {column!r} at {cell}")
            if DRY_RUN:
                print(f"[{ws.title}] DRY RUN - nothing written")
            else:
                if target > ws.col_count:
                    ws.add_cols(1)
                ws.update_acell(cell, column)
                print(f"[{ws.title}] WROTE {column!r} at {cell}")
            target += 1


if __name__ == "__main__":
    main()
