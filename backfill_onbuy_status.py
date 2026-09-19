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

ALSO (2026-09-14): imports already-listed products. A row seeded with an
OPC + Supplier URL and NO SKU means "this product is already live on
OnBuy - bring it under sync": the account's listings are paged once,
the OPC's own SKU is fetched from OnBuy and written into the row, and the
row is marked Synced/created so every later sync run treats it strictly
update-only (the anti-duplicate guard: an OPC on record never re-creates).
A fetched SKU that already exists anywhere on the sheet is refused and
flagged instead - one product, one row. BACKFILL_IMPORT_OPC=0 disables.

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
from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import raise_for_status, with_retry

MAX_PAGES = int(os.getenv("BACKFILL_MAX_PAGES") or "20")
PAGE_SIZE = 50
EXTRA_TABS = [t.strip() for t in (os.getenv("BACKFILL_EXTRA_TABS") or "Amazon").split(",") if t.strip()]
IMPORT_OPC = (os.getenv("BACKFILL_IMPORT_OPC") or "1").strip().lower() not in ("0", "no", "false")
IMPORT_MAX_PAGES = int(os.getenv("BACKFILL_IMPORT_MAX_PAGES") or "120")

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


def _listing_opc(item):
    """The OPC of one GET /v2/listings item. OnBuy's exact field name is
    verified from the first real run's key log (see fetch_opc_sku_map) -
    every observed spelling is tried."""
    item = item or {}
    for key in ("opc", "product_opc", "onbuy_product_code"):
        val = str(item.get(key) or "").strip().upper()
        if val:
            return val
    product = item.get("product")
    if isinstance(product, dict):
        val = str(product.get("opc") or product.get("code") or "").strip().upper()
        if val:
            return val
    return ""


def _import_candidates(data):
    """[(data index, OPC)] for rows seeded for import: a real OPC (not the
    backfill's own PENDING marker), a Supplier URL, and NO SKU yet."""
    out = []
    for idx, row in enumerate(data):
        sku = str(row.get("SKU") or "").replace(",", "").strip()
        opc = str(row.get("OPC") or "").strip().upper()
        url = str(row.get("Supplier URL") or "").strip()
        if not sku and url and opc and opc != "PENDING":
            out.append((idx, opc))
    return out


def fetch_opc_sku_map(onbuy):
    """OPC -> SKU for every listing on this OnBuy account (paged once).
    Logs the first item's keys so the field spelling _listing_opc relies on
    is verifiable from the run log. Raises on a page error - a HALF-built
    map must never mark unmatched OPCs as not-found."""
    mapping = {}
    offset = 0
    for _page in range(IMPORT_MAX_PAGES):
        def _do(off=offset):
            resp = onbuy._send("GET", f"{BASE_URL}/listings", what=f"listings page {off}",
                               params={"site_id": onbuy.site_id, "limit": 100, "offset": off},
                               timeout=60)
            raise_for_status(resp, what=f"listings page {off}")
            return resp
        body = with_retry(_do, what=f"listings page {offset}", max_attempts=3).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            break
        if offset == 0 and items:
            print(f"[opc-import] first listing item keys: {sorted((items[0] or {}).keys())}")
        for item in items:
            sku = str((item or {}).get("sku") or "").strip()
            opc = _listing_opc(item)
            if sku and opc:
                mapping[opc] = sku
        if len(items) < 100:
            break
        offset += 100
    print(f"[opc-import] listings swept: {len(mapping)} OPC->SKU pairs")
    return mapping


def import_opc_rows(tabs, onbuy):
    """Fill the SKU (from OnBuy) on OPC-seeded rows and mark them Synced.
    Writes are anchored to the OPC's CURRENT row (re-located just before
    writing) - these rows have no SKU, so remap_row_writes cannot protect
    them and they must not ride tab["updates"]."""
    per_tab = {}
    for tab in tabs:
        cands = _import_candidates(tab["data"])
        if cands and "OPC" in tab["col_map"]:
            per_tab[tab["sheet"].title] = (tab, cands)
    if not per_tab:
        return False
    total = sum(len(c) for _t, c in per_tab.values())
    print(f"[opc-import] {total} OPC-seeded row(s) to import: "
          + ", ".join(f"{t}: {len(c)}" for t, (_tab, c) in per_tab.items()))

    try:
        opc_to_sku = fetch_opc_sku_map(onbuy)
    except Exception as exc:
        print(f"[opc-import] listings sweep failed ({str(exc)[:200]}) - imports retried next hourly run")
        return True

    taken = set()   # SKUs already on the sheet anywhere, plus ones claimed this run
    for tab in tabs:
        for row in tab["data"]:
            sku = str(row.get("SKU") or "").replace(",", "").strip()
            if sku:
                taken.add(sku)

    for title, (tab, cands) in per_tab.items():
        sheet, col_map = tab["sheet"], tab["col_map"]
        # Re-locate each OPC's current row right before writing: the sheet
        # may have moved since read_tab, and a SKU-less row has no other
        # anchor. An OPC missing or duplicated in the fresh column is
        # skipped (redone next hourly run).
        fresh = sheet.col_values(col_map["OPC"])
        pos = {}
        for _i, _v in enumerate(fresh):
            _v = str(_v).strip().upper()
            if _i and _v and _v != "PENDING":
                pos.setdefault(_v, []).append(_i + 1)
        updates, imported, dropped = [], 0, 0
        for _idx, opc in cands:
            where = pos.get(opc) or []
            if len(where) != 1:
                dropped += 1
                continue
            n = where[0]
            sku = opc_to_sku.get(opc)
            if not sku:
                if "Sync Status" in col_map:
                    updates.append({"range": f"{col_letter(col_map['Sync Status'])}{n}",
                                    "values": [["Failed: OPC not found among this account's OnBuy listings"]]})
                print(f"[opc-import] {title} row {n}: OPC {opc} not on this account")
                continue
            if sku in taken:
                if "Sync Status" in col_map:
                    updates.append({"range": f"{col_letter(col_map['Sync Status'])}{n}",
                                    "values": [[f"Failed: OPC {opc} maps to SKU {sku} which is already on the sheet - one product, one row"]]})
                print(f"[opc-import] {title} row {n}: OPC {opc} -> SKU {sku} already on the sheet")
                continue
            taken.add(sku)
            updates.append({"range": f"{col_letter(col_map['SKU'])}{n}", "values": [[sku]]})
            if "Sync Status" in col_map:
                updates.append({"range": f"{col_letter(col_map['Sync Status'])}{n}", "values": [["Synced"]]})
            if "OnBuy Product Created" in col_map:
                updates.append({"range": f"{col_letter(col_map['OnBuy Product Created'])}{n}", "values": [["TRUE"]]})
            imported += 1
            print(f"[opc-import] {title} row {n}: OPC {opc} -> SKU {sku} imported "
                  f"(next sync run prices it from its supplier link and updates the listing)")
        if updates:
            sheet.batch_update(updates)
        print(f"[opc-import] {title}: {imported} imported, {dropped} deferred (row moved/OPC ambiguous)")
    return True


def _reconcile_deletions(book, onbuy=None):
    """Security change #3 (2026-09-19): sheet-deleted products come off
    OnBuy - stock zeroed on first sight, listing deleted after the grace
    window (deletion_reconciler.py). Called at EVERY exit of main() so
    quiet backfill hours still reconcile; a reconciler hiccup never
    costs the backfill's own work."""
    try:
        if onbuy is None:
            use_sandbox = os.getenv("ONBUY_USE_SANDBOX", "false").strip().lower() == "true"
            onbuy = OnBuyClient(use_sandbox=use_sandbox)
            if not onbuy.authenticate():
                print("delist: OnBuy auth failed - reconciler skipped this run")
                return
        import deletion_reconciler
        deletion_reconciler.run(book, onbuy)
    except Exception as exc:  # noqa: BLE001 - never break the backfill
        print(f"deletion reconciler failed (backfill unaffected): {exc}")


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

    has_imports = IMPORT_OPC and any(_import_candidates(tab["data"]) for tab in tabs)
    if not all_pending and not has_imports:
        print("No rows needing a queue-status check and no OPC-seeded rows - nothing to do.")
        _reconcile_deletions(book)
        return

    use_sandbox = os.getenv("ONBUY_USE_SANDBOX", "false").strip().lower() == "true"
    onbuy = OnBuyClient(use_sandbox=use_sandbox)
    if not onbuy.authenticate():
        print("FAILED to authenticate with OnBuy")
        raise SystemExit(1)

    if has_imports:
        import_opc_rows(tabs, onbuy)

    if not all_pending:
        print("No rows needing a queue-status check found - done.")
        _reconcile_deletions(book, onbuy)
        return

    print(f"Checking {len(all_pending)} pending SKU(s) against OnBuy's queue history...")

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

    _reconcile_deletions(book, onbuy)


if __name__ == "__main__":
    main()
