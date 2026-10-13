"""Delete the OUT-OF-STOCK and BRAND-BLOCKED rows of the eBay tab - from OnBuy AND from the sheet - right after the sync found them
(user 2026-10-12: "All the out of stock and brand blocked rows of eBay should be immediately deleted from store as well as sheet").

Rows (first product tab only; the Amazon tab is not touched):
  brand  Sync Status carries OnBuy's brand verdict ("BRAND BLOCKED (date) - OnBuy says the brand 'X' is owned by another seller" or the raw refusal) and the Brand
         cell still holds the refused brand. A corrected brand lifts the block exactly as before; the 7-day retry no longer applies - the row is deleted.
  oos    Stock is 0, the sync has visited the row (Last Checked Time set) and the supplier link is an eBay item - AND eBay confirms it right now: the item is removed /
         ended / reported OUT_OF_STOCK, or - when eBay merely sent no price or no availability block ("estimatedAvailabilities can be null at random") - a second look a few
         seconds later says the same. An item eBay shows in stock is left alone (the sheet's 0 was stale); a failed fetch deletes nothing.
Never touched: a SKU on more than one sheet row, a SKU in protected_skus.txt, a SKU on an order that still has to be dispatched (when the orders cannot be read the
rows that may be live are held, never guessed), a row without a SKU.

Order of work: OnBuy first (DELETE /listings/by-sku in batches; "not found" = already gone; "suspended" = OnBuy refuses - the SKU is mailed to the user to delete by hand),
then the rows that were really dealt with are copied whole to the tab "Purged <YYYY-MM>" (count verified) and only then deleted from the sheet by SKU in a fresh read.
Supabase is left alone on purpose (the SKU registry outlives the row; the deletion reconciler marks the SKU gone). Nothing is deleted when the plan is implausible
(MASS_MIN and MASS_FRACTION of the tab, or a Stock column that looks unread) unless ALLOW_MASS is set; MAX_ROWS caps one run, so a backlog is worked off run by run.

Env: DRY_RUN (default 1 = report only), MAX_ROWS (200), MAX_EBAY_CHECKS (250) and EBAY_RESERVE (300 calls kept for the sync), KINDS ("brand,oos"), MASS_MIN / MASS_FRACTION /
ALLOW_MASS, SHEET_NAME. Runs as the last step of the eBay sync workflow and as a manual workflow (purge_dead_rows.yml). Prints SKUs, titles and reasons - never a cost."""
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

import delete_listings_batch as dlb
import generate_xml as gx
import held_skus
import notify
import sheet_tabs
import sku_aliases
from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import PermanentError, RateLimitError, TransientError, with_retry

HERE = os.path.dirname(os.path.abspath(__file__))
SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
MAX_ROWS = int(os.getenv("MAX_ROWS") or "200")
MAX_EBAY_CHECKS = int(os.getenv("MAX_EBAY_CHECKS") or "250")
EBAY_RESERVE = int(os.getenv("EBAY_RESERVE") or "300")
KINDS = tuple(k.strip() for k in (os.getenv("KINDS") or "brand,oos").split(",") if k.strip())
MASS_MIN = int(os.getenv("MASS_MIN") or "300")
MASS_FRACTION = float(os.getenv("MASS_FRACTION") or "0.35")
ALLOW_MASS = (os.getenv("ALLOW_MASS") or "").strip().lower() in ("1", "yes", "true")
NEEDED = ("SKU", "Supplier URL", "Stock", "Sync Status", "Last Checked Time")
DEFINITIVE = ("removed", "ended", "out_of_stock")        # what eBay itself says about an item that is gone
REMOVABLE_OUTCOMES = ("ok", "gone", "suspended")         # OnBuy answers after which the sheet row may go


def to_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def norm(sku):
    s = str(sku or "").strip()
    return s.lstrip("0") or s


# ---------------------------------------------------------------- reading the tab
def load_tab(ws):
    """(headers, rows) of one product tab; refuses a tab that lacks the columns this tool decides on."""
    values = with_retry(ws.get_all_values, what=f"read {ws.title}", max_attempts=3)
    headers = [str(x).strip() for x in values[0]]
    ix = {k: i for i, k in enumerate(headers) if k}
    missing = [h for h in NEEDED if h not in ix]
    if missing:
        raise SystemExit(f"tab {ws.title!r} lacks {missing} - refusing to continue")

    def cell(r, k):
        i = ix.get(k)
        return str(r[i]).strip() if i is not None and i < len(r) else ""

    rows = []
    for n, r in enumerate(values[1:], start=2):
        sku = cell(r, "SKU").replace(",", "").strip()
        url = cell(r, "Supplier URL")
        if not sku and not url:
            continue
        rows.append({"tab": ws.title, "row": n, "sku": sku, "url": url, "stock": to_int(cell(r, "Stock")), "sync": cell(r, "Sync Status"),
                     "brand": cell(r, "Brand"), "checked": cell(r, "Last Checked Time"),
                     "created": cell(r, "OnBuy Product Created").upper() == "TRUE", "title": cell(r, "Title"), "cells": list(r)})
    return headers, rows


def sku_counts(rows_by_tab, extra_tabs=()):
    """How many sheet rows carry each SKU, over the rows already read plus the SKU column of any other product tab."""
    counts = Counter()
    for rows in rows_by_tab:
        for r in rows:
            if r["sku"]:
                counts[r["sku"]] += 1
    for ws in extra_tabs:
        headers = [str(h).strip() for h in with_retry(lambda: ws.row_values(1), what="header", max_attempts=3)]
        if "SKU" in headers:
            for v in with_retry(lambda: ws.col_values(headers.index("SKU") + 1), what=f"{ws.title} SKU column", max_attempts=3)[1:]:
                v = str(v).replace(",", "").strip()
                if v:
                    counts[v] += 1
    return counts


# ---------------------------------------------------------------- what is a dead row
def is_brand_blocked(sync, brand, today=None):
    """True while the row carries OnBuy's brand verdict and the Brand cell still holds the refused brand (the sync's own brand_block_state, with the
    7-day retry switched off: such a row is deleted now, not re-tested)."""
    today = today or datetime.now(timezone.utc).date()
    skip, _ = gx.brand_block_state((sync,), gx.normalize_brand(brand or ""), today, retry_days=10 ** 6)
    return bool(skip)


def classify(r, today=None):
    """'brand' | 'oos' | None for one eBay-tab row (the eBay confirmation of an 'oos' row comes later)."""
    if is_brand_blocked(r["sync"], r["brand"], today):
        return "brand"
    if r["stock"] == 0 and r["checked"] and "/itm/" in r["url"]:
        return "oos"
    return None


def plan_purge(rows, *, protected=frozenset(), counts=None, kinds=("brand", "oos"), today=None):
    """-> (candidates, held): candidates = rows + kind; held = [(row, kind, why)] for rows that look dead but must not be touched."""
    counts = counts or {}
    cands, held = [], []
    for r in rows:
        kind = classify(r, today)
        if kind is None or kind not in kinds:
            continue
        if not r["sku"]:
            held.append((r, kind, "no SKU"))
        elif counts.get(r["sku"], 0) > 1:
            held.append((r, kind, "SKU is on more than one sheet row"))
        elif held_skus.is_held(r["sku"], protected):
            held.append((r, kind, "protected listing (protected_skus.txt)"))
        else:
            cands.append(dict(r, kind=kind))
    return cands, held


def hold_open_orders(cands, open_skus):
    """Split candidates on the open-order test. open_skus None = the orders could not be read: whatever may be live (an 'oos' row, or a created one) is held."""
    keep, held = [], []
    for c in cands:
        if open_skus is None:
            if c["kind"] == "oos" or c["created"]:
                held.append((c, c["kind"], "orders could not be read - a row that may be live is not touched"))
                continue
        elif norm(c["sku"]) in open_skus:
            held.append((c, c["kind"], "open order (awaiting dispatch)"))
            continue
        keep.append(c)
    return keep, held


def mass_tripped(n_candidates, n_rows, allow=ALLOW_MASS, min_count=MASS_MIN, fraction=MASS_FRACTION):
    """True when so many rows look dead that a broken read is likelier than a real clean-up."""
    return (not allow) and n_candidates >= min_count and n_candidates >= fraction * n_rows


def stock_column_unread(rows):
    """A Stock column that is mostly blank / non-numeric means the tab was read wrongly (or never synced): deciding on it would be a guess."""
    withsku = [r for r in rows if r["sku"]]
    return len(withsku) >= 100 and sum(1 for r in withsku if r["stock"] is not None) < 0.5 * len(withsku)


# ---------------------------------------------------------------- the eBay confirmation of an 'oos' row
def oos_verdict(fetch, url, sleep=time.sleep):
    """(verdict, why, calls): 'gone' | 'alive' | 'unknown'. `fetch(url)` -> (available, data) like generate_xml.get_ebay_data, data["reason"] says why not.
    A definitive reason (removed / ended / out_of_stock) is final; no price / no availability block is looked at a second time before it counts."""
    calls = 0
    try:
        available, data = fetch(url)
        calls += 1
        if available:
            return "alive", "eBay shows the item in stock", calls
        reason = str((data or {}).get("reason") or "")
        if reason in DEFINITIVE:
            return "gone", reason, calls
        sleep(3)
        available, data = fetch(url)
        calls += 1
        if available:
            return "alive", "in stock on the second look", calls
        return "gone", f"{reason or 'unavailable'} (the same on a second look)", calls
    except RateLimitError:
        raise
    except (TransientError, PermanentError, requests.RequestException) as exc:
        return "unknown", f"eBay fetch failed: {str(exc)[:60]}", calls


def select_final(cands, fetch, max_rows, max_checks, sleep=time.sleep):
    """The rows to delete this run, in order: brand rows first, then 'oos' rows - freshest Last Checked Time first (what this sync run just found) - each
    confirmed with eBay. Returns (final, stats) where stats counts the verdicts and says whether eBay's rate limit stopped the confirmations."""
    final = [c for c in cands if c["kind"] == "brand"][:max_rows]
    oos = sorted([c for c in cands if c["kind"] == "oos"], key=lambda c: gx.parse_time(c["checked"]), reverse=True)
    stats = {"gone": 0, "alive": 0, "unknown": 0, "calls": 0, "unconfirmed": 0, "rate_limited": False, "why": Counter()}
    for n, c in enumerate(oos):
        if len(final) >= max_rows or stats["calls"] >= max_checks:
            stats["unconfirmed"] = len(oos) - n
            break
        try:
            verdict, why, calls = oos_verdict(fetch, c["url"], sleep)
        except RateLimitError:
            stats["rate_limited"] = True
            stats["unconfirmed"] = len(oos) - n
            break
        stats["calls"] += calls
        stats[verdict] += 1
        stats["why"][why] += 1
        if verdict == "gone":
            final.append(dict(c, confirmed=why))
        sleep(0.25)
    return final, stats


# ---------------------------------------------------------------- open orders
def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def open_order_skus(onbuy, max_pages=10, sleep=time.sleep):
    """Normalised SKUs on the orders OnBuy still lists as awaiting dispatch (its default status). Raises when the orders cannot be read."""
    skus, offset, retries = set(), 0, 0
    for _ in range(max_pages):
        resp = onbuy._send("GET", f"{BASE_URL}/orders", what="open orders page",
                           params={"site_id": onbuy.site_id, "limit": 100, "offset": offset}, timeout=90)
        if resp.status_code == 429 and retries < 3:
            retries += 1
            sleep(60)
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"orders: HTTP {resp.status_code} {resp.text[:120]}")
        body = resp.json()
        results = body.get("results") if isinstance(body, dict) else body
        if not isinstance(results, list) or not results:
            break
        for o in results:
            for d in walk(o):
                if d.get("sku") is not None:
                    skus.add(norm(d["sku"]))
        if len(results) < 100:
            break
        offset += 100
        sleep(1.0)
    return skus


# ---------------------------------------------------------------- OnBuy
def delete_on_onbuy(onbuy, skus, batch=100, first=10, max_singles=60, sleep=time.sleep):
    """{SKU: 'ok' | 'gone' | 'suspended' | error text} from batched by-SKU deletes (delete_listings_batch's own send / parse). A batch OnBuy does not answer
    per SKU is split smaller; singles are capped, the rest are left for the next run."""
    skus = list(dict.fromkeys(skus))
    outcomes, idx, size, singles, validated = {}, 0, min(first, len(skus)) or 1, 0, False
    while idx < len(skus):
        chunk = skus[idx:idx + size]
        status, body, text = dlb.send(onbuy, [sku_aliases.to_onbuy(s) for s in chunk])
        parsed = dlb.parse_results(body)
        if not [s for s in chunk if s in parsed]:
            if size > 1:
                size = max(1, size // 4)
                continue
            singles += 1
            outcomes[chunk[0]] = f"error: {text[:100]}"
            idx += 1
            if singles >= max_singles:
                break
            continue
        for s in chunk:
            outcomes[s] = parsed.get(s, "no answer")
        idx += len(chunk)
        if not validated and all(s in parsed for s in chunk):
            validated = True
            size = batch
        sleep(1.0)
    for s in skus:
        outcomes.setdefault(s, "not attempted")
    return outcomes


def may_remove_row(c, outcome):
    """A row leaves the sheet once OnBuy has dealt with its SKU (deleted / already gone / suspended = OnBuy's side to clear), or when it never had a listing
    to delete (a never-created brand-blocked row)."""
    if outcome in REMOVABLE_OUTCOMES:
        return True
    return c["kind"] == "brand" and not c["created"]


# ---------------------------------------------------------------- the sheet
def write_backup(book, tab_name, headers, removed):
    header = ["bk_removed_utc", "bk_tab", "bk_row", "bk_reason"] + ["bk_" + h for h in headers if h]
    try:
        ws = book.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = book.add_worksheet(title=tab_name, rows=len(removed) + 200, cols=len(header))
        ws.update("A1", [header], value_input_option="RAW")
    before = len(with_retry(lambda: ws.col_values(3), what="backup count", max_attempts=3))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ix = {h: i for i, h in enumerate(headers) if h}
    out = []
    for r in removed:
        out.append([now, r["tab"], r["row"], r["reason"]] +
                   [(r["cells"][ix[h]] if h in ix and ix[h] < len(r["cells"]) else "") for h in headers if h])
    for c in range(0, len(out), 250):
        with_retry(lambda ch=out[c:c + 250]: ws.append_rows(ch, value_input_option="RAW"), what="backup append", max_attempts=3)
        time.sleep(1.0)
    after = len(with_retry(lambda: ws.col_values(3), what="backup count", max_attempts=3))
    if after - before < len(removed):
        raise SystemExit(f"BACKUP INCOMPLETE ({after - before} of {len(removed)} rows written) - no row deleted")
    print(f"backup verified: {after - before} row(s) written to '{tab_name}'")


def delete_rows(book, tab_title, skus):
    """Delete the rows holding exactly these SKUs; every chunk re-locates its rows from a fresh read of the SKU column. Returns (deleted, skipped)."""
    ws = book.worksheet(tab_title)
    headers = [str(x).strip() for x in with_retry(lambda: ws.row_values(1), what="header", max_attempts=3)]
    sku_col = headers.index("SKU") + 1
    todo = set(skus)
    deleted, skipped = 0, []
    while todo:
        col = with_retry(lambda: ws.col_values(sku_col), what=f"{tab_title} SKU column", max_attempts=3)
        where = {}
        for n, v in enumerate(col, start=1):
            if n == 1:
                continue
            v = str(v).replace(",", "").strip()
            if v in todo:
                where.setdefault(v, []).append(n)
        batch = []
        for s in sorted(todo)[:100]:
            todo.discard(s)
            rows = where.get(s, [])
            if len(rows) == 1:
                batch.append(rows[0])
            else:
                skipped.append((s, len(rows)))
        if not batch:
            continue
        reqs = [{"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": n - 1, "endIndex": n}}}
                for n in sorted(batch, reverse=True)]
        with_retry(lambda: ws.spreadsheet.batch_update({"requests": reqs}), what=f"{tab_title} delete rows", max_attempts=3)
        deleted += len(batch)
        time.sleep(1.0)
    return deleted, skipped


def list_suspended(book):
    """Print the SKUs of purged rows whose OnBuy listing OnBuy refuses to delete (suspended) - read back from the 'Purged ...' backup tabs, whose reason column keeps
    OnBuy's answer. Returns the (SKU, title) pairs."""
    out = []
    for ws in book.worksheets():
        if not ws.title.startswith("Purged "):
            continue
        values = with_retry(ws.get_all_values, what=f"read {ws.title}", max_attempts=3)
        if not values:
            continue
        h = [str(x).strip() for x in values[0]]
        if "bk_reason" not in h or "bk_SKU" not in h:
            continue
        i_r, i_s, i_t = h.index("bk_reason"), h.index("bk_SKU"), (h.index("bk_Title") if "bk_Title" in h else None)
        for r in values[1:]:
            if i_r < len(r) and "OnBuy: suspended" in r[i_r]:
                out.append((str(r[i_s]).strip() if i_s < len(r) else "", (str(r[i_t]).strip() if i_t is not None and i_t < len(r) else "")[:60]))
    print(f"{len(out)} purged SKU(s) whose OnBuy listing is suspended - OnBuy refuses to delete them, delete them by hand on OnBuy:")
    for sku, title in out:
        print(f"SUSPENDED {sku}  {title}")
    return out


# ---------------------------------------------------------------- the run
def report(rows, cands, held, final, stats):
    kinds = Counter(c["kind"] for c in cands)
    print(f"eBay tab: {len(rows)} rows | looking dead: brand-blocked {kinds.get('brand', 0)}, out of stock {kinds.get('oos', 0)} | held back {len(held)}")
    for why, n in Counter(f"{k}: {w}" for _r, k, w in held).most_common():
        print(f"  held {n:5d}  {why}")
    if stats:
        print(f"eBay confirmation of the out-of-stock rows: gone {stats['gone']}, alive again {stats['alive']}, unknown {stats['unknown']} "
              f"({stats['calls']} call(s)), not looked at this run {stats['unconfirmed']}" + (" - eBay's rate limit stopped it" if stats["rate_limited"] else ""))
        for why, n in stats["why"].most_common(6):
            print(f"    {n:5d}  {why}")
    print(f"TO DELETE THIS RUN: {len(final)} row(s) (brand {sum(1 for c in final if c['kind'] == 'brand')}, out of stock {sum(1 for c in final if c['kind'] == 'oos')}; "
          f"{sum(1 for c in final if c['created'])} marked live on OnBuy)")
    for c in final[:15]:
        print(f"    row {c['row']} SKU {c['sku']} {c['title'][:44]!r} | {c['kind']}{' - ' + c['confirmed'] if c.get('confirmed') else ''}")


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    if (os.getenv("LIST_SUSPENDED") or "").strip().lower() in ("1", "yes", "true"):
        list_suspended(book)
        return
    ws = sheet_tabs.product_sheet(book)
    others = []
    try:
        amz = book.worksheet(sheet_tabs.AMAZON_TAB)
        if amz.title != ws.title:
            others.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    headers, rows = load_tab(ws)
    print(f"{'DRY RUN' if DRY_RUN else 'LIVE RUN'} | tab {ws.title!r} | kinds {','.join(KINDS)} | max {MAX_ROWS} rows, {MAX_EBAY_CHECKS} eBay checks")
    if stock_column_unread(rows):
        raise SystemExit("the Stock column of the eBay tab looks unread (mostly blank) - refusing to decide on it")
    protected = held_skus.load_skus(os.path.join(HERE, "protected_skus.txt"))
    cands, held = plan_purge(rows, protected=protected, counts=sku_counts([rows], others), kinds=KINDS)
    if mass_tripped(len(cands), len(rows)):
        msg = (f"purge refused: {len(cands)} of {len(rows)} eBay-tab rows look dead - more than {MASS_FRACTION:.0%} is a broken read, not a clean-up "
               "(set ALLOW_MASS=yes for a deliberate one)")
        report(rows, cands, held, [], None)
        notify.send_alert_email("Purge of dead eBay rows refused (too many)", msg)
        raise SystemExit(msg)
    if not cands:
        report(rows, cands, held, [], None)
        print("nothing to purge")
        return

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    try:
        open_skus = open_order_skus(onbuy)
    except Exception as exc:  # noqa: BLE001 - unreadable orders = hold what may be live, never guess
        print(f"open orders could not be read ({str(exc)[:120]}) - rows that may be live are held")
        open_skus = None
    cands, order_held = hold_open_orders(cands, open_skus)
    held += order_held

    fetch, checks = None, MAX_EBAY_CHECKS
    if any(c["kind"] == "oos" for c in cands):
        token = gx.get_ebay_token() if os.getenv("EBAY_CLIENT_ID") else None
        if token:
            left = gx.ebay_calls_remaining(token)
            if left is not None:
                checks = min(checks, max(0, left - EBAY_RESERVE))
            fetch = lambda u: gx.get_ebay_data(u, token)
        else:
            print("no eBay token - the out-of-stock rows cannot be confirmed and are left alone this run")
    final_oos_blocked = fetch is None
    if final_oos_blocked:
        cands = [c for c in cands if c["kind"] != "oos"]
    final, stats = select_final(cands, fetch or (lambda u: (True, {})), MAX_ROWS, checks)
    report(rows, cands, held, final, stats if fetch else None)

    if DRY_RUN:
        print("\nDRY RUN - nothing deleted.")
        return
    if not final:
        print("nothing to delete this run")
        return

    outcomes = delete_on_onbuy(onbuy, [c["sku"] for c in final])
    tally = Counter(o if o in ("ok", "gone", "suspended") else "other" for o in outcomes.values())
    print(f"OnBuy: {dict(tally)}")
    removable = [dict(c, reason=("brand blocked" if c["kind"] == "brand" else f"out of stock on eBay ({c.get('confirmed', '')})")
                      + f" | OnBuy: {outcomes.get(c['sku'], 'n/a')}") for c in final if may_remove_row(c, outcomes.get(c["sku"]))]
    kept = [c for c in final if not may_remove_row(c, outcomes.get(c["sku"]))]
    for c in kept[:10]:
        print(f"  kept (OnBuy said {outcomes.get(c['sku'])!r}): row {c['row']} SKU {c['sku']}")
    deleted, skipped = 0, []
    if removable:
        write_backup(book, f"Purged {datetime.now(timezone.utc).strftime('%Y-%m')}", headers, removable)
        deleted, skipped = delete_rows(book, ws.title, [c["sku"] for c in removable])
    print(f"DELETED {deleted} row(s) from the sheet ({len(kept)} kept for the next run, {len(skipped)} skipped at deletion time: {skipped[:5]})")

    suspended = sorted(s for s, o in outcomes.items() if o == "suspended")
    errors = sorted(s for s, o in outcomes.items() if o not in REMOVABLE_OUTCOMES)
    for s in suspended:
        print(f"SUSPENDED {s}  {next((c['title'][:60] for c in final if c['sku'] == s), '')}")
    if suspended:
        titles = {c["sku"]: c["title"][:50] for c in final}
        notify.send_alert_email(
            f"Purge: {len(suspended)} listing(s) OnBuy will not delete - delete them by hand",
            "These eBay-tab rows were removed from the sheet, but OnBuy refuses to delete their listings (suspended). Delete them on OnBuy by hand:\n\n"
            + "\n".join(f"  {s}  {titles.get(s, '')}" for s in suspended))
    if errors:
        notify.send_alert_email(f"Purge: {len(errors)} OnBuy delete(s) failed", "Not deleted this run (their rows stay and are retried):\n" + "\n".join(f"  {s}: {outcomes[s]}" for s in errors[:50]))
    print(f"PURGE DONE: {deleted} row(s) removed (brand {sum(1 for c in removable if c['kind'] == 'brand')}, out of stock {sum(1 for c in removable if c['kind'] == 'oos')}); "
          f"suspended on OnBuy {len(suspended)}; OnBuy errors {len(errors)}")


def guarded_main(fn=None):
    """Run main(); an unexpected crash is mailed (the chained step is continue-on-error, so a failed step still reads "success" in the run) and re-raised."""
    try:
        (fn or main)()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        try:
            notify.send_alert_email("Purge of dead eBay rows crashed", f"{type(exc).__name__}: {str(exc)[:400]}\n\nThe sync itself is unaffected; the purge retries on the next run.")
        except Exception:  # noqa: BLE001
            pass
        raise


if __name__ == "__main__":
    guarded_main()
