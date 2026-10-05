"""Remove rows from the product tabs - guarded, backed up, explicit (2026-10-02; extra guards 2026-10-05).

What goes (see removal_plan.py for the rules):
  A. the SKUs in OOS_LIST_FILE (one per line) - held back if back in stock;
  B. LIVE DUPLICATES (a later row for a supplier product an earlier, live row
     already owns) - held back unless the original row survives and is live,
     optionally limited to the SKUs in DUP_LIST_FILE.

Extra guards (2026-10-05): a duplicate only goes while its original row is
Synced (REQUIRE_KEEPER_SYNCED, default yes) and never when its SKU is in
EXCLUDE_SKUS_FILE or protected_skus.txt (open orders, protected listings).
OOS_LIST_FILE is optional - empty = duplicates only.

Safety, in order: DRY_RUN is on unless set to 0 (it only reports and writes
the plan); nothing is removed when the plan exceeds MAX_REMOVE; every removed
row is first copied, whole, to a BACKUP_TAB in this spreadsheet and the copy
is counted before a single row is deleted; rows are located by their SKU in a
FRESH read just before each deletion chunk (never by a row number computed
earlier) and a SKU found on other than exactly one row is skipped.

Supabase is deliberately NOT touched: the SKU registry must outlive the sheet
row ("one barcode = one product, forever"). Does not touch OnBuy - the
workflow runs delete_listings_batch.py on onbuy_delete_list.txt afterwards.
"""
import csv
import json
import os
import re
import time
from datetime import datetime, timezone

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import keepa_client
import removal_plan
import sheet_tabs
from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
MAX_REMOVE = int(os.getenv("MAX_REMOVE") or "200")
BACKUP_TAB = os.getenv("BACKUP_TAB") or "Removed 2026-10-05"
INCLUDE_DUPS = (os.getenv("REMOVE_LIVE_DUPS") or "yes").strip().lower() in ("1", "yes", "true")
REQUIRE_KEEPER_SYNCED = (os.getenv("REQUIRE_KEEPER_SYNCED") or "yes").strip().lower() in ("1", "yes", "true")


def load_list(path):
    out = []
    if path:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
    return list(dict.fromkeys(out))


def supplier_identity(url):
    """Same rule as generate_xml._supplier_identity."""
    u = str(url or "")
    if "amazon." in u.lower() and keepa_client.parse_asin(u):
        return f"amazon:{keepa_client.parse_asin(u)}"
    m = re.search(r"/itm/(\d+)", u)
    if not m:
        return ""
    v = re.search(r"[?&]var(?:iationId)?=(\d+)", u)
    return f"ebay:{m.group(1)}" + (f":{v.group(1)}" if v else "")


def to_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def load_tab(ws):
    values = with_retry(ws.get_all_values, what=f"read {ws.title}", max_attempts=3)
    headers = [str(x).strip() for x in values[0]]
    ix = {k: i for i, k in enumerate(headers) if k}

    def cell(r, k):
        i = ix.get(k)
        return str(r[i]).strip() if i is not None and i < len(r) else ""
    rows = []
    for n, r in enumerate(values[1:], start=2):
        sku = cell(r, "SKU").replace(",", "").strip()
        url = cell(r, "Supplier URL")
        if not sku and not url:
            continue
        rows.append({"tab": ws.title, "row": n, "sku": sku, "ident": supplier_identity(url),
                     "created": cell(r, "OnBuy Product Created").upper() == "TRUE",
                     "stock": to_int(cell(r, "Stock")), "sync": cell(r, "Sync Status")[:100],
                     "price": cell(r, "Selling Price (£)"), "cells": list(r)})
    return headers, rows


def write_backup(book, headers_by_tab, removed):
    union = []
    for hs in headers_by_tab.values():
        for h in hs:
            if h and h not in union:
                union.append(h)
    header = ["bk_removed_utc", "bk_tab", "bk_row", "bk_reason"] + ["bk_" + h for h in union]
    try:
        ws = book.worksheet(BACKUP_TAB)
        print(f"backup tab '{BACKUP_TAB}' already exists - appending")
    except gspread.exceptions.WorksheetNotFound:
        ws = book.add_worksheet(title=BACKUP_TAB, rows=len(removed) + 50, cols=len(header))
        ws.update("A1", [header], value_input_option="RAW")
        print(f"backup tab '{BACKUP_TAB}' created")
    before = len(with_retry(lambda: ws.col_values(3), what="backup count", max_attempts=3))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for r in removed:
        hs = headers_by_tab[r["tab"]]
        ix = {h: i for i, h in enumerate(hs) if h}
        out.append([now, r["tab"], r["row"], r["reason"]] +
                   [(r["cells"][ix[h]] if h in ix and ix[h] < len(r["cells"]) else "") for h in union])
    for c in range(0, len(out), 250):
        chunk = out[c:c + 250]
        with_retry(lambda ch=chunk: ws.append_rows(ch, value_input_option="RAW"), what="backup append", max_attempts=3)
        time.sleep(1.0)
    after = len(with_retry(lambda: ws.col_values(3), what="backup count", max_attempts=3))
    if after - before < len(removed):
        raise SystemExit(f"BACKUP INCOMPLETE ({after - before} of {len(removed)} rows written) - nothing deleted")
    print(f"backup verified: {after - before} row(s) written to '{BACKUP_TAB}'")


def delete_rows(book, tab_title, skus):
    """Delete the rows holding exactly these SKUs; rows are re-located from a
    fresh read before every chunk."""
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
        reqs = [{"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS",
                                               "startIndex": n - 1, "endIndex": n}}}
                for n in sorted(batch, reverse=True)]
        with_retry(lambda: ws.spreadsheet.batch_update({"requests": reqs}), what=f"{tab_title} delete rows", max_attempts=3)
        deleted += len(batch)
        print(f"  {tab_title}: deleted {deleted} row(s) so far")
        time.sleep(1.0)
    return deleted, skipped


def main():
    listed = load_list(os.getenv("OOS_LIST_FILE") or "")
    approved = load_list(os.getenv("DUP_LIST_FILE") or "") if (os.getenv("DUP_LIST_FILE") or "").strip() else None
    print(f"listed SKUs: {len(listed)} | live duplicates: {'on' if INCLUDE_DUPS else 'off'}"
          f"{'' if approved is None else f' (limited to {len(approved)} approved SKU(s))'} | "
          f"{'DRY RUN' if DRY_RUN else 'LIVE RUN'}")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)

    headers_by_tab, rows = {}, []
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        amz = book.worksheet("Amazon")
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    for ws in tabs:
        h, rs = load_tab(ws)
        if "SKU" not in h or "Supplier URL" not in h:
            raise SystemExit(f"tab {ws.title!r} lacks SKU/Supplier URL headers - refusing to continue")
        headers_by_tab[ws.title] = h
        rows += rs
    print("product rows:", {t: sum(1 for r in rows if r["tab"] == t) for t in headers_by_tab})

    exclude = set(load_list(os.getenv("EXCLUDE_SKUS_FILE") or ""))
    _prot = os.path.join(os.path.dirname(os.path.abspath(__file__)), "protected_skus.txt")
    if os.path.exists(_prot):
        with open(_prot, encoding="utf-8") as fh:
            exclude |= {ln.split("#", 1)[0].strip() for ln in fh if ln.split("#", 1)[0].strip()}
    print(f"never removed (open orders / protected): {len(exclude)} SKU(s) | keeper must be Synced: "
          f"{'yes' if REQUIRE_KEEPER_SYNCED else 'no'}")
    out = removal_plan.plan(rows, listed, include_live_dups=INCLUDE_DUPS, approved_dup_skus=approved,
                            require_keeper_synced=REQUIRE_KEEPER_SYNCED, exclude_skus=exclude)
    remove, held, absent = out["remove"], out["held"], out["absent"]
    removed_keys = {(r["tab"], r["row"]) for r in remove}
    held = [(r, why) for r, why in held if (r["tab"], r["row"]) not in removed_keys]

    by_reason = {}
    for r in remove:
        k = ("listed (out of stock) and a live duplicate" if "and a live duplicate" in r["reason"]
             else "live duplicate" if r["reason"].startswith("live duplicate") else r["reason"])
        by_reason[(r["tab"], k)] = by_reason.get((r["tab"], k), 0) + 1
    print(f"\nWILL REMOVE {len(remove)} row(s):")
    for (tab, k), n in sorted(by_reason.items()):
        print(f"  {n:5d}  {tab:7s} | {k}")
    live_removed = sum(1 for r in remove if r["created"])
    print(f"  of which live on OnBuy (created=TRUE): {live_removed} | in stock at this moment: {sum(1 for r in remove if (r['stock'] or 0) > 0)}")
    print(f"\nHELD BACK {len(held)} row(s):")
    hc = {}
    for r, why in held:
        k = re.sub(r"\(stock \d+\)", "", why)
        hc[(r["tab"], k)] = hc.get((r["tab"], k), 0) + 1
    for (tab, k), n in sorted(hc.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {tab:7s} | {k}")
    for r, why in held[:40]:
        print(f"     held: {r['tab']} row {r['row']} SKU {r['sku']} stock {r['stock']} created {r['created']} - {why}")
    print(f"\nlisted SKUs not on the sheet (OnBuy may still hold them): {len(absent)}")

    # Rows the sync FLAGGED as duplicates and that are live, but that the fresh
    # link groups do not call duplicates (their original row has since been
    # deleted or moved, so the old "...on row N" flag is stale): kept.
    groups = {}
    for r in rows:
        if r["ident"]:
            groups.setdefault(r["ident"], []).append(r)
    # What state are the KEEPERS (the rows that stay) in?
    kc = {}
    for r in remove:
        if r["reason"].startswith("live duplicate"):
            k = groups[r["ident"]][0]
            cls = ("keeper Synced" if k["sync"].startswith("Synced")
                   else "keeper awaiting/pending" if k["sync"].startswith(("Awaiting", "Pending"))
                   else "keeper frozen/failed" if k["sync"].startswith(("Failed", "BRAND", "Skipped"))
                   else "keeper blank/other status")
            kc[cls] = kc.get(cls, 0) + 1
            sk = "keeper in stock" if (k["stock"] or 0) > 0 else "keeper out of stock"
            kc[sk] = kc.get(sk, 0) + 1
            if cls != "keeper Synced" and kc[cls] <= 5:
                print(f"     keeper check: {r['tab']} row {r['row']} SKU {r['sku']} - original {k['tab']} row {k['row']} "
                      f"status {k['sync'][:70]!r} stock {k['stock']} created {k['created']}")
    print("\nkeepers of the duplicates being removed:", kc)
    held_keys = {(h["tab"], h["row"]) for h, _w in held}
    flagged = [r for r in rows if r["created"] and r["sync"].startswith(("Failed: supplier link already used", "Failed: ASIN"))]
    kept_flagged = [r for r in flagged if (r["tab"], r["row"]) not in removed_keys and (r["tab"], r["row"]) not in held_keys]
    fc = {"it is now the FIRST row of its supplier product (the original is gone)": 0,
          "alone on its supplier link": 0, "no supplier id on the link": 0, "other": 0}
    for r in kept_flagged:
        g = groups.get(r["ident"], []) if r["ident"] else None
        k = ("no supplier id on the link" if g is None else "alone on its supplier link" if len(g) < 2
             else "it is now the FIRST row of its supplier product (the original is gone)" if g[0] is r else "other")
        fc[k] += 1
    print(f"\nFLAGGED as duplicate by the sync, live, but NOT removed: {len(kept_flagged)} (of {len(flagged)} flagged live)")
    for k, n in fc.items():
        if n:
            print(f"  {n:5d}  {k}")
    for r in kept_flagged[:12]:
        g = groups.get(r["ident"], [])
        print(f"     kept: {r['tab']} row {r['row']} SKU {r['sku']} stock {r['stock']} | group of {len(g)}: "
              + ", ".join(f"{x['tab']} {x['row']}{'*' if x is r else ''}" for x in g[:5]))

    onbuy_list = sorted({r["sku"] for r in remove} | set(absent))
    with open("onbuy_delete_list.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(onbuy_list) + "\n")
    with open("removal_plan.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["action", "tab", "row", "sku", "stock", "created", "sync", "reason",
                    "keeper_tab", "keeper_row", "keeper_sync", "keeper_stock"])

        def keeper_cells(r):
            k = groups[r["ident"]][0] if r["ident"] in groups and groups[r["ident"]][0] is not r else None
            return [k["tab"], k["row"], k["sync"][:60], k["stock"]] if k else ["", "", "", ""]
        for r in remove:
            w.writerow(["REMOVE", r["tab"], r["row"], r["sku"], r["stock"], r["created"], r["sync"][:60], r["reason"]]
                       + (keeper_cells(r) if r["reason"].startswith("live duplicate") else ["", "", "", ""]))
        for r, why in held:
            w.writerow(["HELD", r["tab"], r["row"], r["sku"], r["stock"], r["created"], r["sync"][:60], why]
                       + keeper_cells(r))
        for s in absent:
            w.writerow(["ABSENT", "", "", s, "", "", "", "listed but not on the sheet", "", "", "", ""])
    print(f"OnBuy delete list: {len(onbuy_list)} SKU(s) -> onbuy_delete_list.txt")

    if DRY_RUN:
        print("\nDRY RUN - nothing changed.")
        return
    if len(remove) > MAX_REMOVE:
        raise SystemExit(f"plan removes {len(remove)} rows, above MAX_REMOVE={MAX_REMOVE} - refusing")
    if not remove:
        print("nothing to remove")
        return

    write_backup(book, headers_by_tab, remove)
    total_deleted, all_skipped = 0, []
    for ws in tabs:
        skus = [r["sku"] for r in remove if r["tab"] == ws.title]
        if skus:
            d, sk = delete_rows(book, ws.title, skus)
            total_deleted += d
            all_skipped += [(ws.title, s, n) for s, n in sk]
    print(f"\nDELETED {total_deleted} of {len(remove)} planned row(s) from the sheet")
    for t, s, n in all_skipped:
        print(f"  skipped {t} SKU {s}: found on {n} row(s) at deletion time")

    # confirm
    left = []
    for ws in tabs:
        col = with_retry(lambda: ws.col_values(headers_by_tab[ws.title].index("SKU") + 1), what="verify", max_attempts=3)
        have = {str(v).replace(",", "").strip() for v in col[1:]}
        left += [r["sku"] for r in remove if r["tab"] == ws.title and r["sku"] in have]
    print(f"VERIFY: {len(left)} removed SKU(s) still on the sheet" + (f": {left[:20]}" if left else ""))
    if left or all_skipped:
        # the OnBuy list must not name a SKU whose row is still there
        keep = set(left) | {s for _t, s, _n in all_skipped}
        with open("onbuy_delete_list.txt", "w", encoding="utf-8") as fh:
            fh.write("\n".join(s for s in onbuy_list if s not in keep) + "\n")
        print(f"{len(keep)} SKU(s) kept out of the OnBuy delete list because their row is still on the sheet")


if __name__ == "__main__":
    main()
