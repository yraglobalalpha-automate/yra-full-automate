"""READ-ONLY: where does each SKU of an explicit list stand on the sheet?

For every SKU in LIST_FILE (one per line, # comments allowed): is it on a
product tab, in what state (Sync Status / listing created / stock), which
supplier product does its link point at, and do OTHER rows share that
supplier product (the keeper that would survive a deletion)? Also lists the
live duplicate-link rows that are NOT in the list, so the two sets can be
compared before anything is deleted.

Writes only CSV artifacts; nothing in the Sheet, OnBuy or Supabase.
"""
import csv
import json
import os
import re
from collections import Counter, defaultdict

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import keepa_client
import sheet_tabs

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
LIST_FILE = os.environ["LIST_FILE"]


def supplier_identity(url):
    """Same rule as generate_xml._supplier_identity: 'amazon:<ASIN>' or
    'ebay:<item id>[:<variation>]'."""
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


def sync_class(s):
    s = str(s or "")
    if s.startswith("Failed: supplier link already used"):
        return "Failed: dup supplier link"
    if s.startswith("Failed: ASIN"):
        return "Failed: dup ASIN"
    if s.startswith("Failed: SKU appears"):
        return "Failed: SKU on several rows"
    if s.startswith("Failed: SKU is registered"):
        return "Failed: SKU registry"
    if s.startswith("Failed"):
        return "Failed: other"
    for p in ("Synced", "Pending Approval", "Awaiting", "BRAND BLOCKED", "Skipped"):
        if s.startswith(p):
            return p
    return "(blank)" if not s.strip() else "other"


def load(ws):
    values = ws.get_all_values()
    h = [str(x).strip() for x in values[0]]
    ix = {k: i for i, k in enumerate(h) if k}

    def cell(r, k):
        i = ix.get(k)
        return str(r[i]).strip() if i is not None and i < len(r) else ""
    rows = []
    for n, r in enumerate(values[1:], start=2):
        sku = cell(r, "SKU").replace(",", "").strip()
        url = cell(r, "Supplier URL")
        if not sku and not url:
            continue
        rows.append({"tab": ws.title, "row": n, "sku": sku, "url": url, "ident": supplier_identity(url),
                     "sync": cell(r, "Sync Status"), "created": cell(r, "OnBuy Product Created").upper(),
                     "stock": to_int(cell(r, "Stock")), "status": cell(r, "Status").upper(),
                     "price": cell(r, "Selling Price (£)"), "lastchk": cell(r, "Last Checked Time"),
                     "title": cell(r, "Title")[:70]})
    return rows


def main():
    wanted = []
    with open(LIST_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                wanted.append(line)
    L = set(wanted)
    print(f"list: {len(wanted)} line(s), {len(L)} distinct SKU(s)")

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(SHEET_NAME)
    rows = load(sheet_tabs.product_sheet(book))
    try:
        rows += load(book.worksheet("Amazon"))
    except gspread.exceptions.WorksheetNotFound:
        pass
    print(f"product rows on the sheet: {len(rows)} ({Counter(r['tab'] for r in rows)})")

    by_sku = defaultdict(list)
    by_ident = defaultdict(list)
    for r in rows:
        if r["sku"]:
            by_sku[r["sku"]].append(r)
        if r["ident"]:
            by_ident[r["ident"]].append(r)

    present = [r for r in rows if r["sku"] in L]
    absent = sorted(L - set(by_sku))
    print(f"\nIN LIST and ON the sheet: {len(present)} row(s) / {len({r['sku'] for r in present})} SKU(s)")
    print(f"IN LIST but NOT on the sheet (already gone / never added): {len(absent)}")
    multi = [s for s in L if len(by_sku.get(s, [])) > 1]
    print(f"list SKUs on more than one sheet row: {len(multi)}")

    def live(r):
        return r["created"] == "TRUE"

    def live_in_stock(r):
        return live(r) and (r["stock"] or 0) > 0 and r["status"] == "ACTIVE"

    print("\nBy tab / Sync Status class / listing created:")
    cnt = Counter((r["tab"], sync_class(r["sync"]), "live" if live(r) else "not live") for r in present)
    for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]):
        print(f"  {v:5d}  {k[0]:7s} | {k[1]:28s} | {k[2]}")
    print(f"live on OnBuy (created=TRUE): {sum(1 for r in present if live(r))} | live and in stock: {sum(1 for r in present if live_in_stock(r))}")

    # Group analysis: does a row of the SAME supplier product survive?
    out = []
    kinds = Counter()
    for r in present:
        grp = by_ident.get(r["ident"], []) if r["ident"] else []
        others = [g for g in grp if (g["tab"], g["row"]) != (r["tab"], r["row"])]
        survivors = [g for g in others if g["sku"] not in L]
        if not r["ident"]:
            kind = "no supplier id on the link"
        elif not others:
            kind = "ONLY row for its supplier product (deleting removes the product)"
        elif survivors:
            kind = "another row of the same product survives"
        else:
            kind = "EVERY row of this supplier product is in the list (product disappears)"
        kinds[kind] += 1
        sv = survivors[0] if survivors else None
        sv_state = ("" if sv is None else "live+in stock" if live_in_stock(sv) else "live, OOS/inactive" if live(sv) else "not live")
        out.append([r["tab"], r["row"], r["sku"], sync_class(r["sync"]), r["created"], r["stock"], r["status"],
                    r["price"], r["lastchk"], r["ident"], len(grp), kind,
                    (f"{sv['tab']} row {sv['row']} SKU {sv['sku']}" if sv else ""), sv_state, r["title"], r["sync"][:120]])
    print("\nDoes a keeper survive? (rows in the list, by their supplier product)")
    for k, v in kinds.most_common():
        print(f"  {v:5d}  {k}")

    # Live duplicate-link rows that are NOT in the list
    dup_live_not_listed = [r for r in rows if sync_class(r["sync"]) in ("Failed: dup supplier link", "Failed: dup ASIN")
                           and live(r) and r["sku"] not in L]
    print(f"\nLIVE duplicate-link/ASIN rows on the sheet that are NOT in the list: {len(dup_live_not_listed)}")
    with open("list_check.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tab", "row", "sku", "sync_class", "created", "stock", "status", "price", "last_checked",
                    "supplier_id", "rows_sharing_supplier_id", "keeper_check", "surviving_row", "surviving_state",
                    "title", "sync_status"])
        w.writerows(out)
    with open("list_check_not_listed_live_dups.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tab", "row", "sku", "sync_class", "stock", "status", "price", "supplier_id", "title", "sync_status"])
        for r in dup_live_not_listed:
            w.writerow([r["tab"], r["row"], r["sku"], sync_class(r["sync"]), r["stock"], r["status"], r["price"],
                        r["ident"], r["title"], r["sync"][:120]])
    with open("list_check_absent.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(absent) + ("\n" if absent else ""))


if __name__ == "__main__":
    main()
