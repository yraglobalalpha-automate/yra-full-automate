"""Backfills the real OnBuy-provided fields (OPC, confirmed active status)
for products whose create_product call only returned a queue_id.

A queue_id means "accepted for async processing" - not created, not
approved. Per OnBuy support (2026-07-02), GET /v2/queues shows the real
outcome once it's processed. The main pipeline (generate_xml.py) doesn't
wait for that - it would add unpredictable delay to every run - so this runs
separately, the same way fetch_listing_ids.py already backfills Listing ID.

NOTE: this is a first version against a real but only lightly-tested OnBuy
endpoint - the queue_id filter on GET /v2/queues didn't actually filter
anything in testing (every value returned the same recent history), so this
instead pages through recent submissions and matches by "uid" (the SKU).
Check the printed output the first few times you run this to confirm it's
finding what you expect; the pagination behavior may need adjusting once
seen at real scale.
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import supabase_db
from generate_xml import col_letter
from onbuy_client import OnBuyClient

MAX_PAGES = int(os.getenv("BACKFILL_MAX_PAGES") or "20")
PAGE_SIZE = 50

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open("YRA_Full_Feed_Master").sheet1

headers = sheet.row_values(1)
col_map = {col: idx + 1 for idx, col in enumerate(headers)}
data = sheet.get_all_records()

if "Sync Status" not in col_map:
    print("Sheet has no 'Sync Status' column - nothing to check.")
    raise SystemExit(0)

POISONED_TEXT = "Failed: rejected with no reason given by OnBuy"

pending = {}  # sku -> sheet row index
poisoned = set()  # rows carrying the false Failed text (see below)
for idx, row in enumerate(data):
    sku = str(row.get("SKU") or "").strip()
    status = str(row.get("Sync Status") or "").strip()
    opc = str(row.get("OPC") or "").strip().upper()
    # "Pending Approval" = submitted, queue outcome never fetched. A sync run
    # can also flip a row to "Awaiting OnBuy go-live" BEFORE this backfill
    # ever saw its queue outcome (its OPC is still PENDING in that case) -
    # those must be checked too, or a submission that actually FAILED in the
    # queue would sit in "Awaiting" forever, never learning it needs the
    # re-create that a "Failed" status would trigger.
    #
    # Rows carrying POISONED_TEXT are re-checked as a REPAIR (2026-08-06):
    # an earlier version of this script wrote that exact text over every
    # still-queued row each hour (the pending-vs-failed split only guarded
    # the print, not the write) - 1,396 rows on the OpenMaal store, while
    # the queue actually held 1,104 successes. Re-checking them against
    # the queue rewrites the truth: success -> Synced + OPC, real failure
    # -> Failed with OnBuy's reason, still queued -> Pending Approval.
    needs_check = status == "Pending Approval" or (
        status.startswith("Awaiting OnBuy go-live") and opc in ("", "PENDING")
    ) or status == POISONED_TEXT
    if sku and needs_check:
        pending[sku] = idx + 2
        if status == POISONED_TEXT:
            poisoned.add(sku)

if not pending:
    print("No rows needing a queue-status check found - nothing to do.")
    raise SystemExit(0)

print(f"Checking {len(pending)} pending SKU(s) against OnBuy's queue history...")

use_sandbox = os.getenv("ONBUY_USE_SANDBOX", "false").strip().lower() == "true"
onbuy = OnBuyClient(use_sandbox=use_sandbox)
if not onbuy.authenticate():
    print("FAILED to authenticate with OnBuy")
    raise SystemExit(1)

found = {}
offset = 0
for _ in range(MAX_PAGES):
    if len(found) >= len(pending):
        break
    try:
        result = onbuy.list_queue(limit=PAGE_SIZE, offset=offset)
    except Exception as exc:
        print(f"Queue lookup failed at offset {offset}: {exc}")
        break

    entries = result.get("results", []) if isinstance(result, dict) else []
    if not entries:
        break

    for entry in entries:
        uid = str(entry.get("uid", "")).strip()
        if uid in pending and uid not in found:
            found[uid] = entry

    offset += PAGE_SIZE

print(f"Found {len(found)} of {len(pending)} pending SKU(s) in the queue history.")

sheet_updates = []
supabase_rows = []
# Postgres validates NOT NULL columns on the candidate row before it even
# checks ON CONFLICT, so upserting a bare {"SKU", "Sync Status", ...} dict
# fails outright if that column set omits any NOT NULL column (Title, etc.) -
# same issue generate_xml.py hit and fixed the same way. Fetch the full
# existing row and update just the tracking columns on top of it instead.
existing_rows = supabase_db.fetch_full_rows(list(found.keys()))

for sku, entry in found.items():
    row_index = pending[sku]
    status = entry.get("status")

    # "pending" (or any unrecognised status) is NOT an outcome, and must
    # write NOTHING. The previous version special-cased pending only in the
    # PRINT below - the write still went through the failure branch,
    # stamping "Failed: rejected with no reason given by OnBuy" onto every
    # still-queued row each hour (1,396 rows on the OpenMaal store by
    # 2026-08-06). That false Failed status then reopened generate_xml's
    # create fallback, so syncs re-submitted those products run after run
    # (OnBuy deduplicated by uid and returned the same OPC - no duplicate
    # products, but endless create churn and a sheet full of phantom
    # failures). The only pending-row write allowed is the REPAIR:
    # restoring "Pending Approval" over the poisoned text.
    if status not in ("success", "failed"):
        if sku in poisoned:
            sheet_updates.append({"range": f"{col_letter(col_map['Sync Status'])}{row_index}", "values": [["Pending Approval"]]})
            existing = existing_rows.get(sku)
            if existing is not None:
                supabase_row = dict(existing)
                supabase_row["Sync Status"] = "Pending Approval"
                supabase_rows.append(supabase_row)
            print(f"{sku}: still in OnBuy's approval queue - restored 'Pending Approval' over the poisoned Failed text")
        else:
            print(f"{sku}: still in OnBuy's approval queue (no outcome yet)")
        continue

    if status == "success":
        opc = entry.get("opc", "")
        # OnBuy's queue history is the only place this ever appears - not in
        # create_product/update_listing's own responses - and confirmed
        # 2026-07-06 to be the canonical live page, distinct from whatever
        # URL the Add Listing page's own search links to.
        product_url = entry.get("product_url", "")
        sync_status = "Synced"
        listing_active = "TRUE"
    else:  # status == "failed" - a real outcome OnBuy reported
        opc = None
        product_url = None
        sync_status = f"Failed: {entry.get('error_message') or 'rejected with no reason given by OnBuy'}"
        listing_active = "FALSE"

    print(f"{sku}: status={status}, opc={opc}"
          + (f", reason={entry.get('error_message') or 'no reason given'}" if status != "success" else ""))

    if opc and "OPC" in col_map:
        sheet_updates.append({"range": f"{col_letter(col_map['OPC'])}{row_index}", "values": [[opc]]})
    if product_url and "Product URL" in col_map:
        sheet_updates.append({"range": f"{col_letter(col_map['Product URL'])}{row_index}", "values": [[product_url]]})
    if "Sync Status" in col_map:
        sheet_updates.append({"range": f"{col_letter(col_map['Sync Status'])}{row_index}", "values": [[sync_status]]})
    if "OnBuy Listing Active" in col_map:
        sheet_updates.append({"range": f"{col_letter(col_map['OnBuy Listing Active'])}{row_index}", "values": [[listing_active]]})

    existing = existing_rows.get(sku)
    if existing is None:
        print(f"{sku}: no existing Supabase row yet - skipping Supabase update "
              f"(the next generate_xml.py run will create it with full data)")
        continue

    supabase_row = dict(existing)
    supabase_row["Sync Status"] = sync_status
    supabase_row["OnBuy Listing Active"] = listing_active
    if opc:
        supabase_row["OPC"] = opc
    # "Product URL" is a brand-new column (2026-07-06) - only write it if the
    # table actually has it (select=* would have returned it in `existing`
    # if so). Without this guard, upserting an unknown column would reject
    # the whole batch's Supabase write, not just skip this one field.
    if product_url and "Product URL" in existing:
        supabase_row["Product URL"] = product_url
    supabase_rows.append(supabase_row)

if sheet_updates:
    sheet.batch_update(sheet_updates)
    print(f"Updated {len(sheet_updates)} sheet cell(s).")

if supabase_rows:
    supabase_db.upsert_products(supabase_rows)
    print(f"Upserted {len(supabase_rows)} Supabase row(s).")

still_pending = set(pending) - set(found)
if still_pending:
    print(f"Still pending (not found in queue history yet): {', '.join(still_pending)}")
