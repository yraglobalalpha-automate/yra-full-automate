"""READ-ONLY (2026-10-08): print the stock / price / status cells of named SKUs - never a cost.

Built to answer "the sheet said out of stock but the listing was still sellable" for a wrong order: what does the row say NOW
(Stock, Status, Selling Price, Last Checked Time, Last OnBuy Sync ...), so it can be laid next to the run logs and OnBuy's own
listing record. Writes nothing to the Sheet, Supabase or OnBuy. Env: SKUS (comma-separated, exact), SHEET_NAME.
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import sheet_tabs
from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
WANT = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
SHOW = ["Stock", "Status", "Selling Price (£)", "Price Check Flag", "Sync Status", "OnBuy Product Created", "OnBuy Listing Active",
        "Last Updated", "Last Checked Time", "Last OnBuy Sync"]


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        amz = book.worksheet(sheet_tabs.AMAZON_TAB)
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    found = {s: [] for s in WANT}
    for ws in tabs:
        values = with_retry(ws.get_all_values, what=f"read {ws.title}", max_attempts=3)
        headers = [str(h).strip() for h in values[0]]
        ix = {h: i for i, h in enumerate(headers) if h}
        for n, r in enumerate(values[1:], start=2):
            sku = (str(r[ix["SKU"]]) if "SKU" in ix and ix["SKU"] < len(r) else "").replace(",", "").strip()
            if sku in found:
                cells = {k: (str(r[ix[k]]).strip() if k in ix and ix[k] < len(r) else "") for k in SHOW}
                found[sku].append((ws.title, n, cells))
    for s in WANT:
        if not found[s]:
            print(f"ROW {s}: not on any product tab")
        for tab, n, cells in found[s]:
            print(f"ROW {s} | {tab} row {n} | " + " | ".join(f"{k}: {v[:40]}" for k, v in cells.items()))


if __name__ == "__main__":
    main()
