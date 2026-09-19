"""Deletion propagation (security change #3, user directive 2026-09-19).

A product deleted from the sheet must come off OnBuy too - two wrong-
priced orders came from listings whose rows had been deleted, leaving
them live at frozen prices forever. Every backfill run diffs the
Supabase registry (every SKU the pipeline ever created; it survives
row deletion) against the product tabs:

- a created SKU on NO product tab gets its stock ZEROED immediately;
- after staying absent for DELIST_GRACE_HOURS (24h - an accidental row
  deletion is recoverable by re-adding the row, which pardons the SKU
  and the next sync restores stock), the listing is DELETED;
- listings OnBuy already reports gone are just marked inactive in the
  registry (no delete call wasted);
- "Listing is suspended" refusals stay queued and retry (they self-
  clear, 2026-09-15 pattern);
- legacy listings with no Supabase record are NEVER touched - those
  remain a human decision (Arden/OpenMaal/Makstore legacy catalogs).

State lives on a "Delist Queue" system tab (SKU | First Missing At |
Stock Zeroed | Deleted At | Note) - auditable, and product-tabs-only
tooling ignores it by design. All timestamps UTC.
"""
import os
from datetime import datetime, timedelta, timezone

import gspread

import supabase_db
from onbuy_client import BASE_URL
from retry_utils import RateLimitError, with_retry

QUEUE_TAB = "Delist Queue"
HEADER = ["SKU", "First Missing At", "Stock Zeroed", "Deleted At", "Note"]
GRACE_HOURS = float(os.getenv("DELIST_GRACE_HOURS") or "24")
MAX_DELETES = int(os.getenv("DELIST_MAX_DELETES_PER_RUN") or "200")
TS = "%Y-%m-%d %H:%M"


def _now():
    return datetime.now(timezone.utc)


def _parse_ts(s):
    try:
        return datetime.strptime(str(s).strip(), TS).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _zeroless(s):
    return s.lstrip("0") or "0"


def _product_tab_skus(book):
    """Display-text SKUs of the first/eBay tab + the Amazon tab, plus a
    zero-insensitive view (the OnBuy dashboard and the API spell some
    numeric SKUs with/without leading zeros - 2026-09-19 lesson; a
    spelling twin on the sheet must count as present)."""
    out = set()
    tabs = [book.sheet1]
    try:
        amz = book.worksheet("Amazon")
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    for ws in tabs:
        headers = [str(h).strip() for h in ws.row_values(1)]
        if "SKU" not in headers:
            continue
        for v in ws.col_values(headers.index("SKU") + 1)[1:]:
            v = str(v).replace(",", "").strip()
            if v:
                out.add(v)
    return out, {_zeroless(s) for s in out}


def _queue(book):
    try:
        tab = book.worksheet(QUEUE_TAB)
    except gspread.exceptions.WorksheetNotFound:
        tab = book.add_worksheet(title=QUEUE_TAB, rows=200, cols=len(HEADER))
        tab.update("A1", [HEADER], value_input_option="RAW")
    rows = tab.get_all_values()
    entries = {}
    for idx, r in enumerate(rows[1:], start=2):
        sku = str(r[0] if r else "").strip()
        if sku:
            r = list(r) + [""] * (len(HEADER) - len(r))
            entries[sku] = {"row": idx, "first": r[1], "zeroed": r[2], "deleted": r[3], "note": r[4]}
    return tab, entries


def _delete_rows(tab, rownums):
    for n in sorted(set(rownums), reverse=True):
        with_retry(lambda n=n: tab.spreadsheet.batch_update({"requests": [{"deleteDimension": {
            "range": {"sheetId": tab.id, "dimension": "ROWS",
                      "startIndex": n - 1, "endIndex": n}}}]}),
            what="delist queue row delete", max_attempts=3)


def _check_gone(onbuy, skus):
    """check_winning in chunks -> set of SKUs OnBuy says do not exist."""
    gone = set()
    for c in range(0, len(skus), 50):
        chunk = skus[c:c + 50]
        try:
            results = onbuy.check_winning(chunk)
        except Exception as exc:  # noqa: BLE001 - probe only, stay conservative
            print(f"delist: check_winning probe failed ({exc}) - treating chunk as live")
            continue
        for it in results or []:
            it = it or {}
            if "not found" in str(it.get("error") or "").lower():
                gone.add(str(it.get("sku") or "").strip())
    return gone


def _delete_listing(onbuy, sku):
    """One DELETE /listings/by-sku. Returns 'ok', 'gone', 'suspended' or
    the error text."""
    def _send():
        return onbuy._send("DELETE", f"{BASE_URL}/listings/by-sku",
                           what=f"delete {sku}",
                           json={"site_id": onbuy.site_id, "skus": [sku]}, timeout=60)
    try:
        resp = with_retry(_send, what=f"delete {sku}", max_attempts=3)
    except RateLimitError:
        return "rate limited"
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:80]
    try:
        node = (resp.json().get("results") or {}).get(sku) or {}
    except ValueError:
        node = {}
    err = str(node.get("error") or "").strip()
    if not err and str(node.get("status") or "").lower() == "ok":
        return "ok"
    low = err.lower()
    if "not found" in low or "does not exist" in low:
        return "gone"
    if "suspended" in low:
        return "suspended"
    return err or "no answer"


def run(book, onbuy):
    if (os.getenv("DELIST_RECONCILER") or "1").strip().lower() in ("0", "no", "false"):
        print("delist: reconciler disabled (DELIST_RECONCILER)")
        return
    created = supabase_db.fetch_created_rows()
    if not created:
        print("delist: no registry rows (or fetch failed) - nothing to reconcile")
        return
    sheet_skus, sheet_zeroless = _product_tab_skus(book)
    missing = [r for r in created
               if r["sku"] not in sheet_skus and _zeroless(r["sku"]) not in sheet_zeroless]
    print(f"delist: registry created={len(created)} | on sheet={len(created) - len(missing)} | missing={len(missing)}")

    tab, queue = _queue(book)
    now = _now()
    now_s = now.strftime(TS)
    pardoned, zeroed, deleted, kept_suspended = [], [], [], []

    # 1. pardons: queued SKUs back on a product tab (and not yet deleted)
    pardon_rows = []
    missing_skus = {r["sku"] for r in missing}
    for sku, ent in list(queue.items()):
        if sku not in missing_skus and not ent["deleted"]:
            pardon_rows.append(ent["row"])
            pardoned.append(sku)
            del queue[sku]

    # 2. new missing SKUs: probe, mark the already-gone, queue + zero the live
    new = [r for r in missing if r["sku"] not in queue]
    if new:
        gone = _check_gone(onbuy, [r["sku"] for r in new])
        if gone:
            supabase_db.mark_listing_inactive(sorted(gone))
            print(f"delist: {len(gone)} missing SKU(s) already gone on OnBuy - registry marked inactive")
        to_queue = [r for r in new if r["sku"] not in gone]
        if to_queue:
            zero_batch = [(r["sku"], r["price"], 0) for r in to_queue if r["price"] and r["price"] > 0]
            zero_ok = set()
            for c in range(0, len(zero_batch), 500):
                chunk = zero_batch[c:c + 500]
                try:
                    results = onbuy.update_listings_by_sku_batch(chunk)
                    errs = {str((it or {}).get("sku") or "").strip(): str((it or {}).get("error") or "").strip()
                            for it in results or []}
                    zero_ok.update(s for s, _p, _st in chunk if not errs.get(s, "missing"))
                except Exception as exc:  # noqa: BLE001
                    print(f"delist: stock-zero batch failed ({exc}) - queued anyway, delete follows the grace window")
            rows = [[r["sku"], now_s, (now_s if r["sku"] in zero_ok else ""), "", ""] for r in to_queue]
            with_retry(lambda: tab.append_rows(rows, value_input_option="RAW", table_range="A1"),
                       what="delist queue append", max_attempts=3)
            zeroed = sorted(zero_ok)
            print(f"delist: queued {len(to_queue)} (stock zeroed on {len(zeroed)}); delete after {GRACE_HOURS:.0f}h absence")

    # 3. grace window over -> delete
    due = [(sku, ent) for sku, ent in queue.items()
           if not ent["deleted"] and (_parse_ts(ent["first"]) or now) <= now - timedelta(hours=GRACE_HOURS)]
    for sku, ent in due[:MAX_DELETES]:
        outcome = _delete_listing(onbuy, sku)
        if outcome == "rate limited":
            print("delist: rate limited - remaining deletes wait for the next run")
            break
        if outcome in ("ok", "gone"):
            deleted.append(sku)
            with_retry(lambda e=ent: tab.update(f"D{e['row']}", [[now_s]], value_input_option="RAW"),
                       what="delist mark deleted", max_attempts=3)
        elif outcome == "suspended":
            kept_suspended.append(sku)
            if ent["note"] != "suspended":
                with_retry(lambda e=ent: tab.update(f"E{e['row']}", [["suspended"]], value_input_option="RAW"),
                           what="delist mark suspended", max_attempts=3)
        else:
            print(f"delist: DELETE {sku} refused: {outcome}")
    if deleted:
        supabase_db.mark_listing_inactive(deleted)

    # 4. housekeeping: drop pardoned rows and deleted rows older than 7 days
    old_rows = [ent["row"] for ent in queue.values()
                if ent["deleted"] and (_parse_ts(ent["deleted"]) or now) <= now - timedelta(days=7)]
    _delete_rows(tab, pardon_rows + old_rows)

    print(f"delist: pardoned {len(pardoned)} | zeroed {len(zeroed)} | deleted {len(deleted)}"
          f" | suspended kept {len(kept_suspended)}")
    if zeroed or deleted:
        try:
            import notify
            notify.send_alert_email(
                "Delist reconciler: sheet-deleted products taken off OnBuy",
                f"Stock zeroed now (deleted after {GRACE_HOURS:.0f}h if still absent): "
                f"{', '.join(zeroed) or '-'}\nDeleted from OnBuy: {', '.join(deleted) or '-'}\n"
                f"Re-add a row with the SKU to keep its listing (pardons automatically).")
        except Exception as exc:  # noqa: BLE001
            print(f"delist: alert email failed ({exc})")
