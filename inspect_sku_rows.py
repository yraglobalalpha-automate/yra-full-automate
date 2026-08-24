"""One-off, READ-ONLY (2026-08-14): an OnBuy order for SKU 941214867068
shows the title/description/images of neighbouring SKU 939828415904 (only
the price matches the ordered SKU). Print the full-sheet rows for both
SKUs plus their immediate neighbours so we can see whether OUR data pairs
each SKU with its own eBay URL/title (sheet clean -> mismatch is on
OnBuy's side from the CSV era) or is itself shifted. Changes nothing."""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import os as _os
TARGETS = set((_os.getenv("INSPECT_SKUS") or "941214867068,939828415904").split(","))


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    client = gspread.authorize(creds)
    sheet = client.open("YRA_Full_Feed_Master").sheet1
    rows = sheet.get_all_records()
    hits = [i for i, r in enumerate(rows)
            if str(r.get("SKU") or "").strip() in TARGETS]
    show = sorted({j for i in hits for j in (i - 1, i, i + 1) if 0 <= j < len(rows)})
    for i in show:
        r = rows[i]
        print(f"row {i + 2}: SKU={str(r.get('SKU') or '').strip()}"
              f" | price={r.get('Selling Price (£)')}"
              f" | status={str(r.get('Sync Status') or '')[:40]}"
              f" | OPC={r.get('OPC')}")
        print(f"   title: {str(r.get('Title') or '')[:100]}")
        print(f"   url:   {str(r.get('Supplier URL') or '')[:100]}")
        print(f"   ean={str(r.get('EAN') or '').strip()} | queue_id={str(r.get('OnBuy Product ID') or '').strip()}"
              f" | last_sync={str(r.get('Last OnBuy Sync') or '').strip()} | checked={str(r.get('Last Checked Time') or '').strip()}")

    n_sample = int(_os.getenv("AWAITING_SAMPLE") or "0")
    if n_sample:
        print(f"--- first {n_sample} rows still Awaiting OnBuy go-live (SKU | queue id | ean) ---")
        shown = 0
        for i, r in enumerate(rows):
            if not str(r.get("Sync Status") or "").startswith("Awaiting OnBuy go-live"):
                continue
            print(f"AWAITING row {i + 2} | SKU {str(r.get('SKU') or '').strip()}"
                  f" | queue_id={str(r.get('OnBuy Product ID') or '').strip()}"
                  f" | ean={str(r.get('EAN') or '').strip()}"
                  f" | title={str(r.get('Title') or '')[:60]}")
            shown += 1
            if shown >= n_sample:
                break


if __name__ == "__main__":
    main()
