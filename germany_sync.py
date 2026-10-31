"""OnBuy Germany (site 2002, prices in EUR) from the same sheet: a "Germany" tab fed by BOTH product tabs (Sheet1 = eBay, Amazon).

OnBuy's catalogue is global - every product YRA sells in the UK already exists with its OPC - so Germany is "attach a seller offer to
the same product on site 2002, priced in EUR". Nothing here touches the UK site: a second client is opened on the German site id and
every OnBuy call goes through it.

Modes (MODE env):
  probe      read-only: what the account can see on the UK and German sites (listing counts; a refusal means Germany is not enabled
             for this seller account yet - that is switched on in the Seller Control Panel, not here).
  setup_tab  create the "Germany" tab (header only; idempotent, never touches other tabs).
  populate   copy eligible rows from Sheet1 + Amazon into the Germany tab (OPC present, priced, in stock, not on the hold lists;
             idempotent by SKU - existing Germany rows are never overwritten). DRY_RUN=1 only prints what it would add.
  sync       the recurring job: refresh each Germany row's GBP price + stock from its source tab, price it in EUR
             (EUR = GBP x EUR_PER_GBP x (1 + GERMANY_UPLIFT_PERCENT/100)), then update the German listings; rows whose OPC has no
             German listing yet are CREATED only with CREATE_MISSING=1, at most MAX_CREATE per run. DRY_RUN=1 plans, writes nothing.
  prune      remove Germany rows whose SKU is on PRUNE_LIST_FILE (one per line) - populate is additive, so a product culled from
             the UK tabs lingers here and would come back as a German listing at launch. Every removed row is backed up to
             BACKUP_TAB first (bulk_remove_rows machinery); matching ignores leading zeros. DRY_RUN=1 only reports.

Prices must be in EUR on site 2002 (the API lists per-site currencies), so EUR_PER_GBP is REQUIRED for populate and sync - there is
no built-in rate: a stale default would misprice a whole market quietly. EU sites also accept GPSR (product safety) fields; if OnBuy
refuses a creation for a missing GPSR field the per-SKU error lands in the tab's "DE Error" column for the owner to see.
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import OnBuyClient
from retry_utils import with_retry

SHEET_NAME = "YRA_Full_Feed_Master"
TAB = (os.getenv("SHEET_TAB") or "Germany").strip()
SOURCE_TABS = ("Sheet1", "Amazon")
SITE_DE = int(os.getenv("ONBUY_SITE_ID_DE") or 2002)            # OnBuy Germany; prices in EUR (docs: International Sites > Currency)

MODE = (os.getenv("MODE") or "probe").strip().lower()
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "off")
PUSH_ENABLED = (os.getenv("ONBUY_API_PUSH_ENABLED") or "").strip().lower() == "true"
CREATE_MISSING = (os.getenv("CREATE_MISSING") or "0").strip().lower() in ("1", "yes", "true", "on")
MAX_CREATE = int(os.getenv("MAX_CREATE") or 25)
ONLY_SKUS = {s.strip() for s in (os.getenv("ONLY_SKUS") or "").replace(",", " ").split() if s.strip()}
INCLUDE_OOS = (os.getenv("INCLUDE_OOS") or "0").strip().lower() in ("1", "yes", "true", "on")

HEADER = ["SKU", "Source Tab", "Title", "OPC", "GBP Price", "EUR Price", "Stock", "DE Listed", "DE Error", "Last Synced", "Note"]
HOLD_FILES = ("protected_skus.txt", "hold_at_zero_skus.txt")


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def rate_and_uplift():
    raw = (os.getenv("EUR_PER_GBP") or "").strip()
    try:
        rate = float(raw)
    except ValueError:
        rate = 0.0
    if not 0.8 <= rate <= 2.0:
        print(f"EUR_PER_GBP is {raw!r}: set the repository variable to the GBP->EUR rate (for example 1.15) before pricing a market in euros.")
        sys.exit(1)
    uplift = float(os.getenv("GERMANY_UPLIFT_PERCENT") or 0)
    return rate, uplift


def eur_price(gbp, rate, uplift):
    return round(float(gbp) * rate * (1.0 + uplift / 100.0), 2)


def open_book():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    return with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)


def held_skus():
    out = set()
    for path in HOLD_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    word = line.split("#", 1)[0].strip()
                    if word:
                        out.add(word.lstrip("0") or word)
        except OSError:
            continue
    return out


def de_client():
    return OnBuyClient(site_id=SITE_DE)


def uk_client():
    return OnBuyClient()                                         # ONBUY_SITE_ID from the environment, as every other tool


def count_listings(onbuy, label):
    """One GET /listings page -> (reachable, total). A refusal is the answer, not a crash: it means the site is not enabled."""
    try:
        resp = onbuy._send("GET", "https://api.onbuy.com/v2/listings", what=f"listings {label}",
                           params={"site_id": onbuy.site_id, "limit": 1, "offset": 0}, timeout=30)
        body = resp.json() if resp.status_code == 200 else {}
        total = ((body.get("metadata") or {}).get("total_rows") if isinstance(body, dict) else None)
        if resp.status_code != 200:
            print(f"{label} (site {onbuy.site_id}): HTTP {resp.status_code} - {resp.text[:300]}")
            return False, None
        print(f"{label} (site {onbuy.site_id}): reachable, total listings {total}")
        return True, total
    except Exception as exc:                                     # noqa: BLE001 - the probe's whole job is to print what happened
        print(f"{label} (site {onbuy.site_id}): {exc}")
        return False, None


def de_listing_skus(onbuy):
    """Every SKU already listed on the German site -> {sku: opc}. Paged like the UK sweeps; small until Germany grows."""
    out = {}
    offset = 0
    while True:
        resp = onbuy._send("GET", "https://api.onbuy.com/v2/listings", what="listings de page",
                           params={"site_id": onbuy.site_id, "limit": 100, "offset": offset}, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"GET /listings site {onbuy.site_id} answered HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        onbuy._listings_total = (body.get("metadata") or {}).get("total_rows") if isinstance(body, dict) else None
        page = body.get("results") if isinstance(body, dict) else body
        page = page if isinstance(page, list) else []
        for item in page:
            sku = str((item or {}).get("sku") or "").strip()
            if sku:
                out[sku] = str((item or {}).get("opc") or "").strip()
        if not onbuy.more_listings(offset, 100) or offset > 20000:
            break
        offset += 100
        time.sleep(0.3)
    return out


def read_sources(book):
    """SKU -> {source, title, opc, gbp, stock} from both product tabs (first tab wins a duplicate SKU)."""
    out = {}
    for tab_name in SOURCE_TABS:
        try:
            records = with_retry(book.worksheet(tab_name).get_all_records, what=f"{tab_name} read", max_attempts=3)
        except gspread.WorksheetNotFound:
            print(f"source tab '{tab_name}' not found - skipped")
            continue
        for row in records:
            sku = str(row.get("SKU") or "").strip()
            if not sku or sku in out:
                continue
            try:
                gbp = float(str(row.get("Selling Price (£)") or "0").replace(",", "") or 0)
            except ValueError:
                gbp = 0.0
            try:
                stock = int(float(str(row.get("Stock") or "0") or 0))
            except ValueError:
                stock = 0
            out[sku] = {"source": tab_name, "title": str(row.get("Title") or "").strip(),
                        "opc": str(row.get("OPC") or "").strip().upper(), "gbp": gbp, "stock": stock}
    print(f"source tabs: {len(out)} distinct SKU(s) read")
    return out


def mode_probe():
    print(f"probe | push enabled: {PUSH_ENABLED}")
    ok_uk, _ = count_listings(uk_client(), "UK")
    de = de_client()
    ok_de, total_de = count_listings(de, "Germany")
    if ok_de:
        print(f"GERMANY: ENABLED for this account - {total_de} listing(s) there today. EUR pricing applies (site {SITE_DE}).")
        try:
            queue = de.list_queue(limit=20) or []
            items = queue.get("results") if isinstance(queue, dict) else queue
            items = items if isinstance(items, list) else []
            print(f"creation queue on the German site: {len(items)} entry(ies) in the last page")
            for q in items[:20]:
                q = q or {}
                print(f"   queue {q.get('queue_id') or q.get('id') or '?'} | sku {q.get('sku') or '?'} | status {q.get('status') or q.get('outcome') or '?'}"
                      + (f" | {str(q.get('error') or '').strip()[:140]}" if q.get("error") else ""))
        except Exception as exc:                                 # noqa: BLE001 - the probe prints, it never crashes
            print(f"creation queue read failed: {exc}")
    else:
        print("GERMANY: NOT reachable with these keys. Enable international selling / Germany in the OnBuy Seller Control Panel, then probe again.")
    for path in ("seller-entities", "sellers/entities", "sites"):
        try:
            resp = de._send("GET", f"https://api.onbuy.com/v2/{path}", what=f"probe {path}", params={}, timeout=30)
            text = resp.text[:700]
            print(f"GET /{path}: HTTP {resp.status_code} - {text}")
        except Exception as exc:                                 # noqa: BLE001
            print(f"GET /{path}: {exc}")
    return 0 if ok_uk else 1


def mode_setup_tab(book):
    try:
        tab = book.worksheet(TAB)
        existing = [str(h).strip() for h in tab.row_values(1)]
        created = False
    except gspread.WorksheetNotFound:
        tab = book.add_worksheet(title=TAB, rows=4000, cols=max(len(HEADER) + 2, 16))
        existing, created = [], True
    header = [h for h in existing if h] if any(existing) else list(HEADER)
    for col in HEADER:
        if col not in header:
            header.append(col)
    if header != existing:
        tab.update(range_name="A1", values=[header])
    print(f"Tab '{TAB}': {'created' if created else 'existed'} - header {'written' if header != existing else 'already complete'} ({len(header)} cells)")
    return 0


def germany_rows(book):
    tab = book.worksheet(TAB)
    records = with_retry(tab.get_all_records, what="germany read", max_attempts=3)
    header = [str(h).strip() for h in tab.row_values(1)]
    return tab, header, records


def mode_populate(book):
    rate, uplift = rate_and_uplift()
    sources = read_sources(book)
    held = held_skus()
    tab, header, records = germany_rows(book)
    already = {str(r.get("SKU") or "").strip() for r in records if str(r.get("SKU") or "").strip()}
    rows, skipped = [], {"no OPC": 0, "no price": 0, "out of stock": 0, "on hold lists": 0, "already on the tab": 0}
    for sku, s in sources.items():
        if ONLY_SKUS and sku not in ONLY_SKUS:
            continue
        if sku in already:
            skipped["already on the tab"] += 1
            continue
        if not s["opc"]:
            skipped["no OPC"] += 1
            continue
        if s["gbp"] <= 0:
            skipped["no price"] += 1
            continue
        if s["stock"] <= 0 and not INCLUDE_OOS:
            skipped["out of stock"] += 1
            continue
        if (sku.lstrip("0") or sku) in held:
            skipped["on hold lists"] += 1
            continue
        rows.append([sku, s["source"], s["title"][:160], s["opc"], s["gbp"], eur_price(s["gbp"], rate, uplift), s["stock"], "", "", "", ""])
    print(f"populate | eligible to add: {len(rows)} | skipped: " + ", ".join(f"{k} {v}" for k, v in skipped.items() if v))
    for row in rows[:5]:
        print(f"   e.g. {row[0]} ({row[1]}) GBP {row[4]} -> EUR {row[5]} stock {row[6]}")
    if DRY_RUN:
        print("DRY RUN - nothing written to the tab.")
        return 0
    for start in range(0, len(rows), 500):
        with_retry(lambda chunk=rows[start:start + 500]: tab.append_rows(chunk, value_input_option="USER_ENTERED"),
                   what="germany append", max_attempts=3)
    print(f"WROTE {len(rows)} row(s) to '{TAB}'.")
    return 0


def mode_prune(book):
    import bulk_remove_rows as brr                                # import here: brr reads BACKUP_TAB from the env when loaded
    path = (os.getenv("PRUNE_LIST_FILE") or "").strip()
    if not path:
        raise SystemExit("PRUNE_LIST_FILE is required for prune - this mode never runs without an explicit list")
    with open(path, encoding="utf-8") as fh:
        wanted = {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}
    canon = {(s.lstrip("0") or s) for s in wanted}                # USER_ENTERED appends can drop a leading zero on the tab
    tab = book.worksheet(TAB)
    values = with_retry(tab.get_all_values, what="germany read", max_attempts=3)
    header = [str(h).strip() for h in values[0]]
    if "SKU" not in header:
        raise SystemExit(f"tab {TAB!r} lacks a SKU header - refusing to continue")
    si = header.index("SKU")
    hits = []
    for n, row in enumerate(values[1:], start=2):
        sku = (str(row[si]) if si < len(row) else "").replace(",", "").strip()
        if sku and (sku.lstrip("0") or sku) in canon:
            hits.append({"tab": TAB, "row": n, "sku": sku,
                         "reason": f"on {os.path.basename(path)} (UK row + listing removed)", "cells": list(row)})
    print(f"prune | Germany rows matching the list: {len(hits)} of {len(values) - 1} (list holds {len(wanted)} SKU(s))")
    for h in hits[:5]:
        print(f"   e.g. row {h['row']} SKU {h['sku']}")
    if DRY_RUN:
        print("DRY RUN - nothing removed from the tab.")
        return 0
    if not hits:
        print("nothing to prune")
        return 0
    brr.write_backup(book, {TAB: header}, hits)
    deleted, skipped = brr.delete_rows(book, TAB, [h["sku"] for h in hits])
    print(f"PRUNED {deleted} row(s) from '{TAB}'" + (f" | skipped (SKU on several rows): {len(skipped)}" if skipped else ""))
    col = with_retry(lambda: tab.col_values(si + 1), what="verify", max_attempts=3)
    left = sum(1 for v in col[1:] if (str(v).replace(",", "").strip().lstrip("0") or str(v).strip()) in canon)
    print(f"VERIFY: {left} listed SKU(s) still on '{TAB}'")
    return 0


def _col(header, name):
    return header.index(name) + 1 if name in header else 0


def mode_sync(book):
    rate, uplift = rate_and_uplift()
    live = PUSH_ENABLED and not DRY_RUN
    print(f"sync | {'LIVE' if live else 'DRY RUN'} | EUR_PER_GBP {rate} | uplift {uplift}% | create missing: {CREATE_MISSING} (max {MAX_CREATE})")
    sources = read_sources(book)
    tab, header, records = germany_rows(book)
    todo = [(n + 2, r) for n, r in enumerate(records) if str(r.get("SKU") or "").strip()
            and (not ONLY_SKUS or str(r.get("SKU")).strip() in ONLY_SKUS)]
    if not todo:
        print("the Germany tab has no rows to sync (populate it first)")
        return 0
    onbuy = de_client()
    existing = de_listing_skus(onbuy)
    print(f"Germany site today: {len(existing)} listing(s)")
    updates, creates, zero_src = [], [], 0
    plan_rows = {}
    for rownum, r in todo:
        sku = str(r.get("SKU")).strip()
        src = sources.get(sku)
        opc = str(r.get("OPC") or "").strip().upper() or (src or {}).get("opc", "")
        if src is None:
            gbp, stock, note = 0.0, 0, "source row gone"
            zero_src += 1
        else:
            gbp, stock, note = src["gbp"], src["stock"], ""
        eur = eur_price(gbp, rate, uplift) if gbp > 0 else 0.0
        plan_rows[sku] = {"row": rownum, "eur": eur, "stock": stock, "gbp": gbp, "note": note}
        if sku in existing:
            if eur > 0:
                updates.append((sku, eur, stock))
        elif opc and eur > 0 and stock > 0:
            creates.append({"opc": opc, "sku": sku, "condition": "new", "price": eur, "stock": stock})
    creates = creates[:MAX_CREATE] if CREATE_MISSING else []
    print(f"plan: update {len(updates)} German listing(s), create {len(creates)}, source rows gone {zero_src}")
    for c in creates[:5]:
        print(f"   create e.g. {c['sku']} OPC {c['opc']} EUR {c['price']} stock {c['stock']}")
    for u in updates[:5]:
        print(f"   update e.g. {u[0]} -> EUR {u[1]} stock {u[2]}")
    if not live:
        print("DRY RUN - nothing sent to OnBuy." if DRY_RUN else "ONBUY_API_PUSH_ENABLED is not true - nothing sent to OnBuy.")
        return 0

    errors = {}
    for start in range(0, len(updates), 500):
        results = onbuy.update_listings_by_sku_batch(updates[start:start + 500])
        for item in results or []:
            err = str((item or {}).get("error") or "").strip()
            if err:
                errors[str(item.get("sku") or "").strip()] = err
    created_ok, queued = 0, False
    if creates:
        body = onbuy.create_listings_batch(creates)
        results = body.get("results") if isinstance(body, dict) else body
        if isinstance(results, list) and results:
            listed = {}
            for it in results:
                it = it or {}
                sku = str(it.get("sku") or "").strip()
                failed = it.get("success") is False or it.get("error")
                why = str(it.get("message") or it.get("error") or "").strip()
                if it.get("errors"):
                    why = (why + " | " + "; ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
                                                   for k, v in it["errors"].items())).strip(" |")
                listed[sku] = why if failed else ""
            for c in creates:
                err = listed.get(c["sku"], "")
                if err:
                    errors[c["sku"]] = err
                else:
                    created_ok += 1
        else:
            # OnBuy queues creations (the UK flow is built around that queue): the batch was ACCEPTED, the outcomes land in the
            # site's creation queue - probe prints it. Never claim "created" for an answer that named no listings.
            queued = len(creates) > 0
            print(f"creation ACCEPTED for {len(creates)} listing(s) - OnBuy queues creations; run mode=probe for the queue's outcomes. Response keys: {sorted(body) if isinstance(body, dict) else type(body).__name__}")
    print(f"DONE: {len(updates) - sum(1 for s, _, _ in updates if s in errors)} updated, {created_ok} created, {len(creates) if queued else 0} queued, {len(errors)} error(s)")
    for sku, err in list(errors.items())[:10]:
        print(f"   ERROR {sku}: {err[:160]}")

    stamp = now_iso()
    cells = []
    for sku, p in plan_rows.items():
        touched = sku in {s for s, _, _ in updates} or any(c["sku"] == sku for c in creates)
        if not touched:
            continue
        created_here = any(c["sku"] == sku for c in creates) and sku not in errors
        listed_word = "yes" if sku in existing else ("queued" if created_here and queued else ("yes" if created_here else ""))
        for col_name, value in (("EUR Price", p["eur"]), ("Stock", p["stock"]), ("GBP Price", p["gbp"]),
                                ("DE Listed", listed_word), ("DE Error", errors.get(sku, "")),
                                ("Last Synced", stamp), ("Note", p["note"])):
            col = _col(header, col_name)
            if col:
                cells.append(gspread.Cell(p["row"], col, value))
    for start in range(0, len(cells), 2000):
        with_retry(lambda chunk=cells[start:start + 2000]: tab.update_cells(chunk, value_input_option="USER_ENTERED"),
                   what="germany write-back", max_attempts=3)
    print(f"tab updated: {len(cells)} cell(s)")
    return 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")           # the client logs every raw OnBuy answer - the only window into the queue's behaviour
    print(f"germany_sync | mode {MODE} | tab '{TAB}' | site {SITE_DE}")
    if MODE == "probe":
        return mode_probe()
    book = open_book()
    if MODE == "setup_tab":
        return mode_setup_tab(book)
    if MODE == "populate":
        return mode_populate(book)
    if MODE == "sync":
        return mode_sync(book)
    if MODE == "prune":
        return mode_prune(book)
    print(f"unknown MODE '{MODE}' (probe / setup_tab / populate / sync / prune)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
