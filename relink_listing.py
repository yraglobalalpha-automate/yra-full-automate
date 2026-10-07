"""Relink listings onto the CORRECT catalogue product (2026-10-06).

A listing that sits on a WRONG product (another product's title, description and images - OnBuy's matcher attached it there when it
was created) cannot be repaired in place. Re-submitting the row's own content (create_product + force_update) makes OnBuy build the
RIGHT product under a new OPC, but the existing listing stays where it is: the queue answers "success" with the new OPC while the
listing keeps showing the old product. The fix is to MOVE the listing: delete it, then attach the same SKU to the right OPC.

Env:
  RELINK            "sku:opc,sku:opc" (required). The OPC must be the one OnBuy's queue answered for that SKU's own content
                    submission (repair_shifted_content.py); it is checked against the visible queue history before anything is deleted.
  DRY_RUN           default 1 = read-only plan, nothing is written anywhere.
  BLOCKED_SKUS      comma-separated SKUs that must not be touched (e.g. a listing with an open order).
  UPDATE_SHEET      default 1: after the new listing verifies, write the new OPC into the row's OPC cell (guard: the cell must still
                    hold the old OPC, or be empty/PENDING).
  SHEET_TAB         worksheet (default: the main product tab).
  QUEUE_PAGES       pages of 50 queue entries read for the OPC check (default 8); ALLOW_UNVERIFIED_OPC=1 proceeds without a queue entry.
  MAX_RELINK        hard cap per run (default 2).
  ATTACH_RETRIES / ATTACH_WAIT   the SKU may stay reserved for a while after the delete (default 24 tries, 30 s apart).
  ATTACH_ONLY       1 = resume mode for a SKU whose listing is already deleted (a previous run stopped between delete and attach): no delete,
                    the price comes from the sheet row; refused when the SKU still has a live listing.

Safety: a listing is only deleted when it exists, sits on a DIFFERENT OPC than the target, and shows stock 0 (never a sellable one); the
new listing is attached at stock 0 with the old listing's own price (the sync or the team restocks it once the page is checked). SKUs are
processed one at a time and the run stops at the first problem.
"""
import json
import logging
import os
import re
import time

import gspread
from gspread.utils import rowcol_to_a1
from oauth2client.service_account import ServiceAccountCredentials

import sheet_tabs
import sku_aliases
from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import RateLimitError, raise_for_status, with_retry

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _flag(name, default):
    return (os.getenv(name) or default).strip().lower() not in ("0", "no", "false", "")


DRY_RUN = _flag("DRY_RUN", "1")
UPDATE_SHEET = _flag("UPDATE_SHEET", "1")
ALLOW_UNVERIFIED_OPC = _flag("ALLOW_UNVERIFIED_OPC", "0")
RELINK = (os.getenv("RELINK") or "").strip()
BLOCKED_SKUS = {s.strip() for s in (os.getenv("BLOCKED_SKUS") or "").split(",") if s.strip()}
QUEUE_PAGES = int(os.getenv("QUEUE_PAGES") or "8")
MAX_RELINK = int(os.getenv("MAX_RELINK") or "2")
ATTACH_RETRIES = int(os.getenv("ATTACH_RETRIES") or "24")
ATTACH_WAIT = float(os.getenv("ATTACH_WAIT") or "30")
ATTACH_ONLY = _flag("ATTACH_ONLY", "0")

OPC_RE = re.compile(r"^P[A-Z0-9]{4,10}$")


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def parse_pairs(text):
    """'sku:opc,sku:opc' -> [(sku, OPC)]; anything malformed or repeated is an error (a typo must never reach OnBuy)."""
    pairs, seen = [], set()
    for part in [p.strip() for p in str(text or "").split(",") if p.strip()]:
        sku, sep, opc = part.partition(":")
        sku, opc = sku.strip(), opc.strip().upper()
        if not sep or not sku.isdigit() or not OPC_RE.match(opc):
            raise ValueError(f"bad RELINK item {part!r} - expected <digits>:<OPC>")
        if sku in seen:
            raise ValueError(f"SKU {sku} given twice")
        seen.add(sku)
        pairs.append((sku, opc))
    return pairs


def _num(value, default=0.0):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def decide(sku, new_opc, listing, row, queue_entry, blocked, allow_unverified=False, attach_only=False):
    """Pure plan for ONE SKU. listing: the live OnBuy record (dict) or None; row: {"row", "title", "price", "opc"} or None;
    queue_entry: the newest queue entry for the SKU ({"status","opc"}) or None. -> {"go": bool, "reason": str, ...}"""
    out = {"sku": sku, "new_opc": new_opc, "go": False, "reason": ""}
    if sku in blocked:
        out["reason"] = "blocked (BLOCKED_SKUS - e.g. an open order)"
        return out
    if attach_only:
        if listing:
            out["reason"] = ("the SKU still has a live listing - ATTACH_ONLY is only for a SKU whose listing is already deleted "
                             "(use the normal relink)")
            return out
        old_opc = str((row or {}).get("opc") or "").strip().upper()
        out["old_opc"] = old_opc
    else:
        if not listing:
            out["reason"] = "no live listing found for this SKU - nothing to relink"
            return out
        old_opc = str(listing.get("product_encoded_id") or listing.get("opc") or "").strip().upper()
        out["old_opc"] = old_opc
        if old_opc == new_opc:
            out["reason"] = "the listing is already on the target product"
            return out
        stock = _num(listing.get("stock"), -1)
        if stock != 0:
            out["reason"] = f"the listing still shows stock {listing.get('stock')!r} - never delete a sellable listing (zero it first)"
            return out
    if not row:
        out["reason"] = "SKU not found on the sheet"
        return out
    if not str(row.get("title") or "").strip():
        out["reason"] = "the sheet row has no title"
        return out
    sheet_opc = str(row.get("opc") or "").strip().upper()
    if sheet_opc not in ("", "PENDING", old_opc):
        out["reason"] = f"the sheet's OPC cell says {sheet_opc}, the live listing is on {old_opc} - not touching a row that disagrees"
        return out
    if queue_entry is None:
        if not allow_unverified:
            out["reason"] = "no queue entry for this SKU in the visible history - the target OPC cannot be verified"
            return out
    else:
        if str(queue_entry.get("status") or "").lower() != "success":
            out["reason"] = f"the newest queue entry for the SKU is {queue_entry.get('status')!r}, not success"
            return out
        if str(queue_entry.get("opc") or "").strip().upper() != new_opc:
            out["reason"] = f"the queue says this SKU's product is {queue_entry.get('opc')}, not {new_opc}"
            return out
    price = _num((listing or {}).get("price"))
    if price <= 0:
        price = _num(row.get("price"))
    if price <= 0:
        out["reason"] = "no usable price on the listing or the sheet"
        return out
    out.update(go=True, reason="ok", price=round(price, 2), row=row.get("row"), title=row.get("title"))
    return out


def verify_after(listing, new_opc, price, title, fit_title=lambda t: t):
    """Problems found on the re-read listing (empty list = the relink is good)."""
    problems = []
    if not listing:
        return ["the SKU has no listing after the attach"]
    got = str(listing.get("product_encoded_id") or listing.get("opc") or "").strip().upper()
    if got != new_opc:
        problems.append(f"listing is on {got or '?'}, expected {new_opc}")
    if _num(listing.get("stock"), -1) != 0:
        problems.append(f"stock is {listing.get('stock')!r}, expected 0")
    if abs(_num(listing.get("price")) - price) > 0.011:
        problems.append(f"price is {listing.get('price')!r}, expected {price:.2f}")
    name, want = norm(listing.get("name")), norm(fit_title(title))
    if not name or not want or not (name == want or name.startswith(want) or want.startswith(name)):
        problems.append(f"product name {str(listing.get('name'))[:80]!r} does not match the sheet title {str(title)[:80]!r}")
    return problems


# ---------------------------------------------------------------- OnBuy calls

def _json(resp):
    try:
        return resp.json()
    except ValueError:
        return {}


def read_listing(onbuy, sku):
    """The one live listing of a SKU through the filtered GET (1 request); None when there is none."""
    wire = sku_aliases.to_onbuy(sku)

    def _do():
        r = onbuy._send("GET", f"{BASE_URL}/listings", what=f"relink read {sku}",
                        params={"site_id": onbuy.site_id, "limit": 5, "offset": 0, "filter[sku]": wire}, timeout=60)
        raise_for_status(r, what=f"relink read {sku}")
        return r
    body = _json(with_retry(_do, what=f"relink read {sku}", max_attempts=3))
    items = (body.get("results") if isinstance(body, dict) else body) or []
    hit = [i for i in items if str((i or {}).get("sku") or "").strip() == wire]
    if len(hit) > 1:
        raise RuntimeError(f"{len(hit)} listings answer to SKU {sku} - refusing to guess")
    return hit[0] if hit else None


def read_queue_entries(onbuy, skus, pages):
    """{sku: newest queue entry} for the wanted SKUs from the visible queue history (newest first)."""
    found, want = {}, set(skus)
    for page in range(max(0, pages)):
        try:
            result = onbuy.list_queue(limit=50, offset=page * 50)
        except RateLimitError:
            log.warning("queue read rate limited at page %d - using what was read", page)
            break
        entries = result.get("results", []) if isinstance(result, dict) else []
        if not entries:
            break
        for e in entries:
            uid = str((e or {}).get("uid") or "").strip()
            if uid in want and uid not in found:
                found[uid] = {"status": e.get("status"), "opc": e.get("opc"), "queue_id": e.get("queue_id")}
        if len(found) == len(want) or len(entries) < 50:
            break
    return found


def delete_listing(onbuy, sku):
    """DELETE /listings/by-sku for one SKU -> 'ok' | 'gone' | 'suspended' | error text."""
    wire = sku_aliases.to_onbuy(sku)

    def _send():
        return onbuy._send("DELETE", f"{BASE_URL}/listings/by-sku", what=f"relink delete {sku}",
                           json={"site_id": onbuy.site_id, "skus": [wire]}, timeout=60)
    try:
        resp = with_retry(_send, what=f"relink delete {sku}", max_attempts=3)
    except RateLimitError:
        return "rate limited"
    except Exception as exc:  # noqa: BLE001
        return str(exc)[:120]
    body = _json(resp)
    results = body.get("results") if isinstance(body, dict) else None
    node = results.get(wire) if isinstance(results, dict) else None
    if node is None and isinstance(results, list):
        node = next((r for r in results if isinstance(r, dict) and str(r.get("sku") or "") == wire), None)
    node = node or {}
    err = str(node.get("error") or "").strip()
    if not err and str(node.get("status") or "").lower() == "ok":
        return "ok"
    low = err.lower()
    if "not found" in low or "does not exist" in low:
        return "gone"
    if "suspended" in low:
        return "suspended"
    return err or f"no answer (HTTP {resp.status_code}: {resp.text[:120]})"


def item_failure(item):
    """Failure text of ONE per-item answer. OnBuy answers HTTP 200 and a top-level success=true even when the item itself failed:
    {"success": false, "message": "The SKU already exists for a different product", ...} (seen 2026-10-07), or carries an "error" key."""
    if not isinstance(item, dict):
        return ""
    if str(item.get("error") or "").strip():
        return str(item["error"]).strip()
    if item.get("success") is False:
        return str(item.get("message") or "item failed without a message").strip()
    return ""


def attach_listing(onbuy, sku, opc, price, sleep=time.sleep):
    """POST /listings: the SKU onto the right OPC at stock 0. Retries while the SKU is still being freed. -> (ok, text)"""
    wire = sku_aliases.to_onbuy(sku)
    payload = {"site_id": onbuy.site_id, "seller_id": onbuy.seller_id,
               "listings": [{"opc": opc, "sku": wire, "condition": "new", "price": round(price, 2), "stock": 0}]}
    last = ""
    for attempt in range(1, ATTACH_RETRIES + 1):
        resp = onbuy._send("POST", f"{BASE_URL}/listings", what=f"relink attach {sku}", json=payload, timeout=120)
        body = _json(resp)
        results = body.get("results") if isinstance(body, dict) else None
        item = None
        if isinstance(results, list):
            item = next((r for r in results if isinstance(r, dict) and str(r.get("sku") or "") == wire), results[0] if len(results) == 1 else None)
        elif isinstance(results, dict):
            item = results.get(wire)
        err = item_failure(item)
        log.info("attach %s attempt %d: HTTP %s %s", sku, attempt, resp.status_code, (err or resp.text[:160]))
        if resp.status_code < 300 and not err and isinstance(item, dict):
            return True, "ok"
        last = err or f"HTTP {resp.status_code}: {resp.text[:160]}"
        low = last.lower()
        if "exist" in low:
            # either the old listing is not gone yet, or the new one is already there - the re-read decides
            current = read_listing(onbuy, sku)
            if current and str(current.get("product_encoded_id") or "").strip().upper() == opc:
                return True, "already attached"
        elif not (resp.status_code in (429, 500, 502, 503) or "try again" in low or "queue" in low or "process" in low):
            return False, last
        sleep(ATTACH_WAIT)
    return False, last


# ---------------------------------------------------------------- sheet

def read_sheet(book):
    tab = (os.getenv("SHEET_TAB") or "").strip()
    sheet = sheet_tabs.product_sheet(book) if not tab else book.worksheet(tab)
    rows = sheet.get_all_records()
    headers = [str(h).strip() for h in sheet.row_values(1)]
    if "SKU" in headers:
        shown = sheet.col_values(headers.index("SKU") + 1)
        for i, r in enumerate(rows):
            if i + 1 < len(shown):
                r["SKU"] = str(shown[i + 1]).replace(",", "").strip()
    by_sku = {}
    for i, r in enumerate(rows):
        sku = str(r.get("SKU") or "").strip()
        if sku and sku not in by_sku:
            by_sku[sku] = {"row": i + 2, "title": str(r.get("Title") or "").strip(),
                           "price": _num(r.get("Selling Price (£)")), "opc": str(r.get("OPC") or "").strip()}
    return sheet, headers, by_sku


def write_opc(sheet, headers, sku, old_opc, new_opc):
    """Write the new OPC into the SKU's row, located again in a FRESH read; the cell must still hold the old OPC (or nothing)."""
    if "OPC" not in headers or "SKU" not in headers:
        return False, "no OPC/SKU column"
    sku_col, opc_col = headers.index("SKU") + 1, headers.index("OPC") + 1
    skus = [str(v).replace(",", "").strip() for v in sheet.col_values(sku_col)]
    where = [i + 1 for i, v in enumerate(skus) if i and v == sku]
    if len(where) != 1:
        return False, f"SKU found on {len(where)} rows in the fresh read"
    n = where[0]
    current = str(sheet.cell(n, opc_col).value or "").strip().upper()
    if current not in ("", "PENDING", old_opc):
        return False, f"OPC cell now holds {current}"
    with_retry(lambda: sheet.batch_update([{"range": rowcol_to_a1(n, opc_col), "values": [[new_opc]]}]),
               what=f"write OPC row {n}", max_attempts=3)
    return True, f"row {n}"


# ---------------------------------------------------------------- run

def main():
    try:
        pairs = parse_pairs(RELINK)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if not pairs:
        raise SystemExit("RELINK is empty")
    if len(pairs) > MAX_RELINK:
        raise SystemExit(f"{len(pairs)} SKUs requested, MAX_RELINK is {MAX_RELINK}")
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]), ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master")
    sheet, headers, by_sku = read_sheet(book)
    queue = read_queue_entries(onbuy, [s for s, _ in pairs], QUEUE_PAGES) if QUEUE_PAGES > 0 else {}

    plans = []
    for sku, new_opc in pairs:
        listing = read_listing(onbuy, sku)
        plan = decide(sku, new_opc, listing, by_sku.get(sku), queue.get(sku), BLOCKED_SKUS, ALLOW_UNVERIFIED_OPC, ATTACH_ONLY)
        plan["listing"] = listing
        plans.append(plan)
        shown = (f"on {plan.get('old_opc')} -> {new_opc}, price {plan.get('price')}, sheet row {plan.get('row')}" if plan["go"] else plan["reason"])
        log.info("PLAN %s: %s | %s", sku, "GO" if plan["go"] else "SKIP", shown)
        if listing:
            log.info("  live listing now: name=%r stock=%r price=%r", str(listing.get("name"))[:90], listing.get("stock"), listing.get("price"))
        if plan["go"]:
            log.info("  sheet title: %r", str(plan["title"])[:90])
    if DRY_RUN:
        log.info("DRY RUN - nothing deleted, attached or written")
        return
    if not all(p["go"] for p in plans):
        raise SystemExit("at least one SKU is not cleared to relink - nothing was changed (run with those SKUs left out)")

    for p in plans:
        sku, new_opc, price = p["sku"], p["new_opc"], p["price"]
        if ATTACH_ONLY:
            log.info("DELETE %s: skipped (ATTACH_ONLY - the listing is already gone)", sku)
        else:
            outcome = delete_listing(onbuy, sku)
            log.info("DELETE %s: %s", sku, outcome)
            if outcome not in ("ok", "gone"):
                raise SystemExit(f"{sku}: delete refused ({outcome}) - stopped, nothing else touched")
        ok, text = attach_listing(onbuy, sku, new_opc, price)
        log.info("ATTACH %s -> %s: %s (%s)", sku, new_opc, "ok" if ok else "FAILED", text)
        if not ok:
            raise SystemExit(f"{sku}: the old listing is deleted but the attach to {new_opc} failed ({text}) - the SKU has NO listing now; "
                             f"fix by attaching it to {new_opc} (price {price:.2f}, stock 0) and stop here")
        after = read_listing(onbuy, sku)
        problems = verify_after(after, new_opc, price, p["title"], fit_title=onbuy._fit_product_name)
        log.info("VERIFY %s: name=%r opc=%r stock=%r price=%r", sku, str((after or {}).get("name"))[:90],
                 (after or {}).get("product_encoded_id"), (after or {}).get("stock"), (after or {}).get("price"))
        if problems:
            raise SystemExit(f"{sku}: attached, but the re-read shows problems: {'; '.join(problems)} - sheet NOT updated, stopped")
        if UPDATE_SHEET:
            done, where = write_opc(sheet, headers, sku, p["old_opc"], new_opc)
            log.info("SHEET OPC %s: %s (%s)", sku, "updated" if done else "NOT updated", where)
        log.info("RELINKED %s: %s -> %s, stock 0 (held until you restock it)", sku, p["old_opc"], new_opc)
    log.info("DONE: %d relinked", len(plans))


if __name__ == "__main__":
    main()
