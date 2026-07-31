"""One-off, READ-ONLY diagnostic: find SKUs currently live on OnBuy that were
ever rejected with "the supplied brand is owned by another seller" - i.e.
candidates for the retroactive cleanup discussed on 2026-07-06 (remove the
row entirely instead of leaving it listed as Unbranded under the old policy).

This does NOT delete, deactivate, or modify anything - it only prints a
report. Run this first, share the output, and a separate change will do the
actual removal only for the SKUs confirmed from this list.

Why this can't just grep the Sheet/Supabase "Sync Status" column: once the
old Unbranded-retry succeeded for a SKU, its Sync Status became "Pending
Approval"/"Synced" on the very next run - the original rejection message is
gone from there. OnBuy's own /v2/queues history is the only place that
original rejection is still visible, so this pages through it instead.

Usage: same credentials as generate_xml.py (GOOGLE_CREDENTIALS, ONBUY_* etc.
as env vars, or a local .env loaded however you normally run these scripts).
Set BRAND_REJECT_MAX_PAGES to scan further back than the default if needed.
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import supabase_db
from onbuy_client import OnBuyClient

MAX_PAGES = int(os.getenv("BRAND_REJECT_MAX_PAGES") or "40")
PAGE_SIZE = 50
REJECTION_PHRASE = "supplied brand is owned by another seller"

use_sandbox = os.getenv("ONBUY_USE_SANDBOX", "false").strip().lower() == "true"
onbuy = OnBuyClient(use_sandbox=use_sandbox)
if not onbuy.authenticate():
    print("FAILED to authenticate with OnBuy")
    raise SystemExit(1)

print(f"Scanning up to {MAX_PAGES} page(s) of {PAGE_SIZE} of OnBuy's queue history "
      f"for '{REJECTION_PHRASE}'...")

flagged = {}  # sku -> most recent matching queue entry
offset = 0
pages_scanned = 0
for _ in range(MAX_PAGES):
    try:
        result = onbuy.list_queue(limit=PAGE_SIZE, offset=offset)
    except Exception as exc:
        print(f"Queue lookup failed at offset {offset}: {exc}")
        break

    entries = result.get("results", []) if isinstance(result, dict) else []
    if not entries:
        break
    pages_scanned += 1

    for entry in entries:
        error_text = str(entry.get("error_message") or entry.get("error") or "")
        if REJECTION_PHRASE in error_text:
            uid = str(entry.get("uid", "")).strip()
            if uid:
                flagged[uid] = entry

    offset += PAGE_SIZE

print(f"Scanned {pages_scanned} page(s) ({pages_scanned * PAGE_SIZE} queue entries).")
print(f"Found {len(flagged)} SKU(s) ever rejected for this reason: {', '.join(sorted(flagged)) or '(none)'}")

if not flagged:
    print("Nothing to review - no cleanup needed.")
    raise SystemExit(0)

# Cross-reference against Supabase for each flagged SKU's current state -
# specifically whether it's actually live now (may have succeeded on a LATER
# resubmission attempt under a different SKU value, or may have never
# actually gone live at all if every attempt failed).
existing = supabase_db.fetch_full_rows(list(flagged.keys()))

print("\n--- Current state per SKU ---")
for sku in sorted(flagged):
    row = existing.get(sku)
    if row is None:
        print(f"{sku}: no Supabase row found (may have already been removed, or never made it past the Sheet)")
        continue
    print(
        f"{sku}: Brand='{row.get('Brand')}' | Sync Status='{row.get('Sync Status')}' | "
        f"OnBuy Listing Active='{row.get('OnBuy Listing Active')}' | "
        f"OnBuy Product Created='{row.get('OnBuy Product Created')}' | OPC='{row.get('OPC')}' | "
        f"Title='{str(row.get('Title'))[:60]}'"
    )

# Also check the Sheet directly, since a SKU could exist there without (yet)
# having a Supabase row, or vice versa.
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
gclient = gspread.authorize(creds)
sheet = gclient.open("YRA_Full_Feed_Master").sheet1
data = sheet.get_all_records()

sheet_rows_by_sku = {}
for idx, row in enumerate(data):
    sku = str(row.get("SKU") or "").strip()
    if sku in flagged:
        sheet_rows_by_sku[sku] = idx + 2

print("\n--- Sheet row numbers (for reference only - nothing will be touched by this script) ---")
for sku in sorted(flagged):
    row_num = sheet_rows_by_sku.get(sku)
    print(f"{sku}: {'row ' + str(row_num) if row_num else 'not found in Sheet'}")

print(
    "\nNo changes were made. Share this output back so the exact SKU list can be "
    "confirmed before anything is actually removed from OnBuy/Sheet/Supabase."
)
