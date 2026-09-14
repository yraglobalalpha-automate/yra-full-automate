"""Backfills the real OnBuy-provided fields (OPC, confirmed active status)
for products whose create_product call only returned a queue_id.

A queue_id means "accepted for async processing" - not created, not
approved. Per OnBuy support (2026-07-02), GET /v2/queues shows the real
outcome once it's processed. The main pipeline (generate_xml.py) doesn't
wait for that - it would add unpredictable delay to every run - so this runs
separately, the same way fetch_listing_ids.py already backfills Listing ID.

Checks the first tab (eBay rows) and every tab named in BACKFILL_EXTRA_TABS
that exists (default "Amazon" - the Amazon tab shares the header row and
gets its OPCs the same way, 2026-09-09). The queue history is paged once
for all tabs together.

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
EXTRA_TABS = [t.strip() for t in (os.getenv("BACKFILL_EXTRA_TABS") or "Amazon").split(",") if t.strip()]

POISONED_TEXT = "Failed: rejected with no reason given by OnBuy"


def outcome_for(entry):
    """Pure decision for one queue entry - the ONLY place a queue status
    maps to what gets written. Returns one of:
      ("pending", None, None, None)          - write NOTHING (see the loop)
      ("synced", opc, product_url, "Synced")
      ("failed", None, None, "Failed: <OnBuy's reason>")
    Anything that isn't an explicit success/failed - including unknown or
    missing statuses - counts as pending. The 2026-08-06 incident was this
    exact table drifting: pending entries fell through to the failure
    branch's WRITE and stamped 1,396 phantom failures. tests/ pins it.
    """
    status = entry.get("status")
    if status == "success":
        return ("synced", entry.get("opc", ""), entry.get("product_url", ""), "Synced")
    if status == "failed":
        return ("failed", None, None,
                f"Failed: {entry.get('error_message') or 'rejected with no reason given by OnBuy'}")
    return ("pending", None, None, None)


def read_tab(sheet):
    """One tab's rows with a bracketed consistent read (2026-08-31): the SKU
    column is read BEFORE and AFTER the records read and must be identical -
    the 08-29 guard compared two column reads taken after the records read,
    which missed edits landing between the records read and the first
    column read (the hole behind the 08-29 wrong-content creates). Leading
    zeros survive via displayed text. Returns None if the tab would not
    hold still."""
    headers = sheet.row_values(1)
    col_map = {col: idx + 1 for idx, col in enumerate(headers)}
    sku_display, data = [], []
    for _stab in range(3):
        sku_display = sheet.col_values(col_map["SKU"]) if "SKU" in col_map else []
        data = sheet.get_all_records()
        if "SKU" not in col_map:
            break
        if sheet.col_values(col_map["SKU"]) == sku_display:
            break
        print(f"[{sheet.title}] Sheet changed during the read - re-reading for a consistent snapshot")
    else:
        print(f"[{sheet.title}] Sheet still being edited after 3 re-reads - skipping this tab; the next hourly run will retry")
        return None
    if "SKU" in col_map:
        for _i, _row in enumerate(data):
            if _i + 1 < len(sku_display):
                _row["SKU"] = str(sku_display[_i + 1]).replace(",", "").strip()
    return {"sheet": sheet, "col_map": col_map, "sku_display": sku_display, "data": data, "updates": []}


def remap_row_writes(tab, updates, what):
    """Re-anchors row-addressed cell writes right before the flush: each
    write's row is identified by the SKU that occupied it at read time and
    follows that SKU to its current row; anchor gone/duplicated -> dropped
    (redone next hourly run). See generate_xml.py, 2026-08-31 incident."""
    sheet, col_map, sku_display = tab["sheet"], tab["col_map"], tab["sku_display"]
    if "SKU" not in col_map:
        return updates
    fresh = sheet.col_values(col_map["SKU"])
    if fresh == sku_display:
        return updates
    pos = {}
    for _idx, _val in enumerate(fresh):
        _key = str(_val).replace(",", "").strip()
        if _idx and _key:
            pos.setdefault(_key, []).append(_idx + 1)
    kept, n_remap, n_drop = [], 0, 0
    for _u in updates:
        _rng = str(_u["range"])
        _head = _rng.rstrip("0123456789")
        try:
            _row_n = int(_rng[len(_head):])
        except ValueError:
            kept.append(_u)
            continue
        _sku = str(sku_display[_row_n - 1]).replace(",", "").strip() if _row_n - 1 < len(sku_display) else ""
        _tgt = pos.get(_sku) or []
        if _sku and len(_tgt) == 1:
            if _tgt[0] != _row_n:
                _u = {"range": f"{_head}{_tgt[0]}", "values": _u["values"]}
                n_remap += 1
            kept.append(_u)
        else:
            n_drop += 1
    print(f"[{sheet.title}] {what}: sheet rows moved during the run - {n_remap} write(s) re-anchored, {n_drop} dropped (redone next run)")
    return kept


def pending_rows(tab):
    """sku -> sheet row index for the rows whose queue outcome is still
    unknown, plus the set carrying the poisoned text."""
    pending, poisoned = {}, set()
    for idx, row in enumerate(tab["data"]):
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
    return pending, poisoned


def main():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    book = gspread.authorize(creds).open("YRA_Full_Feed_Master")

    sheets = [book.sheet1]
    for name in EXTRA_TABS:
        try:
            extra = book.worksheet(name)
        except gspread.exceptions.WorksheetNotFound:
            continue
        if extra.title != sheets[0].title:
            sheets.append(extra)

    tabs, all_pending, poisoned = [], {}, set()
    for sheet in sheets:
        tab = read_tab(sheet)
        if tab is None:
            continue
        if "Sync Status" not in tab["col_map"]:
            print(f"[{sheet.title}] no 'Sync Status' column - nothing to check on this tab.")
            continue
        pending, tab_poisoned = pending_rows(tab)
        for sku, row_index in pending.items():
            all_pending.setdefault(sku, (tab, row_index))   # a SKU on two tabs: the first tab wins
        poisoned |= tab_poisoned
        tabs.append(tab)
        print(f"[{sheet.title}] {len(pending)} pending SKU(s)")

    if not all_pending:
        print("No rows needing a queue-status check found - nothing to do.")
        return

    print(f"Checking {len(all_pending)} pending SKU(s) against OnBuy's queue history...")

    use_sandbox = os.getenv("ONBUY_USE_SANDBOX", "false").strip().lower() == "true"
    onbuy = OnBuyClient(use_sandbox=use_sandbox)
    if not onbuy.authenticate():
        print("FAILED to authenticate with OnBuy")
        raise SystemExit(1)

    found = {}
    offset = 0
    for _ in range(MAX_PAGES):
        if len(found) >= len(all_pending):
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
            if uid in all_pending and uid not in found:
                found[uid] = entry
        offset += PAGE_SIZE

    print(f"Found {len(found)} of {len(all_pending)} pending SKU(s) in the queue history.")

    supabase_rows = []
    # Postgres validates NOT NULL columns on the candidate row before it even
    # checks ON CONFLICT, so upserting a bare {"SKU", "Sync Status", ...} dict
    # fails outright if that column set omits any NOT NULL column (Title, etc.) -
    # same issue generate_xml.py hit and fixed the same way. Fetch the full
    # existing row and update just the tracking columns on top of it instead.
    existing_rows = supabase_db.fetch_full_rows(list(found.keys()))

    for sku, entry in found.items():
        tab, row_index = all_pending[sku]
        col_map, updates = tab["col_map"], tab["updates"]
        kind, opc, product_url, sync_status = outcome_for(entry)

        # "pending" (or any unrecognised status) is NOT an outcome, and must
        # write NOTHING - see outcome_for()'s docstring for the 2026-08-06
        # incident this rule comes from. The only pending-row write allowed is
        # the REPAIR: restoring "Pending Approval" over the poisoned text.
        if kind == "pending":
            if sku in poisoned:
                updates.append({"range": f"{col_letter(col_map['Sync Status'])}{row_index}", "values": [["Pending Approval"]]})
                existing = existing_rows.get(sku)
                if existing is not None:
                    supabase_row = dict(existing)
                    supabase_row["Sync Status"] = "Pending Approval"
                    supabase_rows.append(supabase_row)
                print(f"{sku}: still in OnBuy's approval queue - restored 'Pending Approval' over the poisoned Failed text")
            else:
                print(f"{sku}: still in OnBuy's approval queue (no outcome yet)")
            continue

        # OnBuy's queue history is the only place the OPC/product_url ever
        # appear - not in create_product/update_listing's own responses - and
        # confirmed 2026-07-06 to be the canonical live page, distinct from
        # whatever URL the Add Listing page's own search links to.
        listing_active = "TRUE" if kind == "synced" else "FALSE"

        print(f"{sku}: status={entry.get('status')}, opc={opc}"
              + (f", reason={entry.get('error_message') or 'no reason given'}" if kind != "synced" else ""))

        if opc and "OPC" in col_map:
            updates.append({"range": f"{col_letter(col_map['OPC'])}{row_index}", "values": [[opc]]})
        if product_url and "Product URL" in col_map:
            updates.append({"range": f"{col_letter(col_map['Product URL'])}{row_index}", "values": [[product_url]]})
        if "Sync Status" in col_map:
            updates.append({"range": f"{col_letter(col_map['Sync Status'])}{row_index}", "values": [[sync_status]]})
        if "OnBuy Listing Active" in col_map:
            updates.append({"range": f"{col_letter(col_map['OnBuy Listing Active'])}{row_index}", "values": [[listing_active]]})

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

    for tab in tabs:
        updates = tab["updates"]
        if updates:
            updates = remap_row_writes(tab, updates, "backfill writes")
        if updates:
            tab["sheet"].batch_update(updates)
            print(f"[{tab['sheet'].title}] Updated {len(updates)} sheet cell(s).")

    if supabase_rows:
        supabase_db.upsert_products(supabase_rows)
        print(f"Upserted {len(supabase_rows)} Supabase row(s).")

    still_pending = set(all_pending) - set(found)
    if still_pending:
        print(f"Still pending (not found in queue history yet): {', '.join(still_pending)}")


if __name__ == "__main__":
    main()
