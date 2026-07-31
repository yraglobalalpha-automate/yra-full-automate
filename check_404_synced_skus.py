"""One-off, READ-ONLY diagnostic for SKUs reported as "successfully synced
(OPC issued, appears in the Add Listing search) but 404ing on their own
product page." Pulls together what we can check on our side:

1. Current Supabase state (Category/Category ID, OnBuy Listing Active,
   Sync Status, OPC, Last OnBuy Sync) - to see whether update_listing has
   ever actually succeeded for these SKUs (Listing Active flipping to TRUE),
   as opposed to the product only ever having been create_product'd once.
2. OnBuy's own /v2/queues history for these SKUs directly - independent of
   whatever Supabase has cached, in case there's more than one submission on
   record or a status Supabase never picked up.

Does not modify anything - diagnostic output only.

Usage: CHECK_404_SKUS=sku1,sku2 python check_404_synced_skus.py
Defaults to the two SKUs reported on 2026-07-06.
"""
import os

import supabase_db
from onbuy_client import OnBuyClient

MAX_PAGES = int(os.getenv("CHECK_404_MAX_PAGES") or "40")
PAGE_SIZE = 50

raw = os.getenv("CHECK_404_SKUS") or "9501234567891,404834097401"
target_skus = {s.strip() for s in raw.split(",") if s.strip()}

if not target_skus:
    print("No SKUs specified - nothing to check.")
    raise SystemExit(0)

print(f"Checking {len(target_skus)} SKU(s): {', '.join(sorted(target_skus))}")

# ---- Current Supabase state ----
existing = supabase_db.fetch_full_rows(list(target_skus))
print("\n--- Current Supabase state ---")
for sku in sorted(target_skus):
    row = existing.get(sku)
    if row is None:
        print(f"{sku}: no Supabase row found")
        continue
    print(
        f"{sku}:\n"
        f"  Category='{row.get('Category')}' | Category ID='{row.get('Category ID')}'\n"
        f"  Brand='{row.get('Brand')}'\n"
        f"  OnBuy Product Created='{row.get('OnBuy Product Created')}' | "
        f"OnBuy Listing Active='{row.get('OnBuy Listing Active')}'\n"
        f"  Sync Status='{row.get('Sync Status')}'\n"
        f"  OPC='{row.get('OPC')}' | OnBuy Product ID (queue_id)='{row.get('OnBuy Product ID')}'\n"
        f"  Last OnBuy Sync='{row.get('Last OnBuy Sync')}'"
    )

# ---- OnBuy's own queue history, independent of Supabase's cached view ----
use_sandbox = os.getenv("ONBUY_USE_SANDBOX", "false").strip().lower() == "true"
onbuy = OnBuyClient(use_sandbox=use_sandbox)
if not onbuy.authenticate():
    print("\nFAILED to authenticate with OnBuy - skipping queue history check")
    raise SystemExit(1)

print(f"\nScanning up to {MAX_PAGES} page(s) of {PAGE_SIZE} of OnBuy's queue history...")
found = {}  # sku -> list of matching entries (there may be more than one submission)
offset = 0
pages_scanned = 0
for _ in range(MAX_PAGES):
    if all(sku in found for sku in target_skus):
        break
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
        uid = str(entry.get("uid", "")).strip()
        if uid in target_skus:
            found.setdefault(uid, []).append(entry)
    offset += PAGE_SIZE

print(f"Scanned {pages_scanned} page(s) ({pages_scanned * PAGE_SIZE} queue entries).")

print("\n--- OnBuy's own queue history per SKU ---")
for sku in sorted(target_skus):
    entries = found.get(sku)
    if not entries:
        print(f"{sku}: not found in the scanned history (may be further back - raise CHECK_404_MAX_PAGES)")
        continue
    print(f"{sku}: {len(entries)} submission(s) found in queue history")
    for entry in entries:
        print(f"  {entry}")

print("\nNo changes were made - this is diagnostic output only.")
