"""One-off REMOVAL script: deletes specific SKUs' Sheet row + Supabase row
entirely. Only run this for SKUs already confirmed (via
find_brand_rejected_skus.py + a human check) to have no live OnBuy listing -
this script does not touch OnBuy at all, only the Sheet and Supabase.

Re-locates each SKU fresh in the Sheet at run time rather than trusting a row
number from an earlier diagnostic run - the Sheet may have changed since
(including possibly already being auto-removed by generate_xml.py's own
brand-rejection handling, which is fine - this script just reports "not
found" for anything already gone rather than erroring).

Usage: BRAND_REJECTED_SKUS_TO_REMOVE=sku1,sku2 python remove_brand_rejected_skus.py
Defaults to the SKU confirmed on 2026-07-06 (848276464399, "Schallen" fan -
never actually approved on OnBuy per its queue history, so no OnBuy-side
cleanup is needed for it).
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import supabase_db

DEFAULT_SKUS = "848276464399"
raw = os.getenv("BRAND_REJECTED_SKUS_TO_REMOVE") or DEFAULT_SKUS
target_skus = {s.strip() for s in raw.split(",") if s.strip()}

if not target_skus:
    print("No SKUs specified - nothing to do.")
    raise SystemExit(0)

print(f"Removing {len(target_skus)} confirmed brand-rejected SKU(s): {', '.join(sorted(target_skus))}")

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open("YRA_Full_Feed_Master").sheet1

data = sheet.get_all_records()

rows_to_delete = []
found_skus = []
for idx, row in enumerate(data):
    sku = str(row.get("SKU") or "").strip()
    if sku in target_skus:
        row_num = idx + 2
        rows_to_delete.append(row_num)
        found_skus.append(sku)
        print(f"Located SKU {sku} at Sheet row {row_num} (Title: {str(row.get('Title'))[:60]!r})")

missing = target_skus - set(found_skus)
if missing:
    print(f"Not found in the Sheet (already removed, or never made it there): {', '.join(sorted(missing))}")

if not found_skus:
    print("Nothing found to delete - done.")
    raise SystemExit(0)

# Supabase first, then the Sheet row - same ordering generate_xml.py's own
# brand-rejection removal uses: if the Sheet delete below then fails, the
# row survives to be caught again by that same automated logic next run
# (it's still in the Sheet with the same rejected Sync Status); deleting the
# Sheet row first and having Supabase fail after would instead risk an
# orphaned Supabase row with nothing left to ever trigger cleaning it up.
supabase_ok = supabase_db.delete_products(sorted(found_skus))
print(f"Supabase delete: {'OK' if supabase_ok else 'FAILED - see error above'} ({len(found_skus)} row(s))")

delete_requests = [
    {
        "deleteDimension": {
            "range": {
                "sheetId": sheet.id,
                "dimension": "ROWS",
                "startIndex": row_num - 1,
                "endIndex": row_num,
            }
        }
    }
    for row_num in sorted(set(rows_to_delete), reverse=True)
]
sheet.spreadsheet.batch_update({"requests": delete_requests})
print(f"Sheet delete: OK ({len(rows_to_delete)} row(s))")
print("Done.")
