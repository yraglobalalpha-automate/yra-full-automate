"""READ-ONLY: print the key columns of the rows a removal copied to its backup
tab (BACKUP_TAB) for a list of SKUs (SKUS, comma-separated). Long text columns
(description, images) are not printed. Writes nothing."""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
BACKUP_TAB = os.getenv("BACKUP_TAB") or "Removed 2026-10-02"
WANT = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
# No Cost Price / Shipping Cost columns on purpose: this repository (and so every run log) is public.
SHOW = ["bk_removed_utc", "bk_tab", "bk_row", "bk_reason", "bk_SKU", "bk_Title", "bk_Supplier URL", "bk_Stock",
        "bk_Status", "bk_Selling Price (£)", "bk_Last Checked Time",
        "bk_Last OnBuy Sync", "bk_Sync Status", "bk_OnBuy Product Created", "bk_OnBuy Listing Active", "bk_OPC",
        "bk_OnBuy Product ID", "bk_Category", "bk_Brand", "bk_EAN"]


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(SHEET_NAME)
    ws = book.worksheet(BACKUP_TAB)
    values = ws.get_all_values()
    header = [str(h).strip() for h in values[0]]
    ix = {h: i for i, h in enumerate(header)}
    sku_col = ix.get("bk_SKU")
    print(f"backup tab {BACKUP_TAB!r}: {len(values) - 1} row(s); looking for {len(WANT)} SKU(s)")
    found = 0
    for r in values[1:]:
        sku = str(r[sku_col]).replace(",", "").strip() if sku_col is not None and sku_col < len(r) else ""
        if sku in WANT:
            found += 1
            print("---- BACKUP ROW")
            for h in SHOW:
                if h in ix and ix[h] < len(r):
                    print(f"  {h[3:] if h.startswith('bk_') and h not in ('bk_removed_utc', 'bk_tab', 'bk_row', 'bk_reason') else h}: {str(r[ix[h]])[:160]}")
    print(f"found {found} of {len(WANT)}")


if __name__ == "__main__":
    main()
