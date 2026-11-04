"""READ-ONLY (2026-11-04): which sheet rows belong to the blocked brands (brand_denylist.txt: Hisense and Homcom) or to the brands given?
Why: the user ordered every Hisense and Homcom product out of YRA (sheet and OnBuy), and never listed again. This finds them in Sheet1, Amazon
and Germany with the SAME rules the sync applies (brand_denylist.py): a row is a BRAND row when its Brand cell names the brand, or when the
Brand cell is empty/generic and the brand is in the first three words of the Title. A title that only MENTIONS the brand while another brand
sits in the Brand cell (an accessory "for Hisense TV") is listed as TITLE-ONLY and is NOT written to the SKU file - read that list before
deleting anything. After the block is live a clean store prints 0 BRAND rows (a new one shows up flagged "Blocked brand: ..." with Stock 0).
Prints no cost cell and no supplier link (the Actions log is public). Writes brand_skus.txt (one SKU per line) and brand_rows.csv.
Env: SHEET_NAME, BRANDS (brands separated by |; empty = the store's blocklist), SHOW (max rows printed per group, default 400)."""
import csv
import json
import os
from collections import Counter, defaultdict

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import brand_denylist
import sheet_tabs
from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
BRANDS = [b.strip() for b in (os.getenv("BRANDS") or "").split("|") if b.strip()]
SHOW = int(os.getenv("SHOW") or "400")
GERMANY_TAB = "Germany"
LISTED = brand_denylist.compile_names([(b, [b]) for b in BRANDS]) if BRANDS else brand_denylist.names()


def classify(brand_cell, title):
    """('BRAND' | 'TITLE-ONLY' | '', blocked brand name) for one row - the sync's own rule (brand_denylist.blocked_name)."""
    name = brand_denylist.blocked_name(brand_cell, title, LISTED)
    if name:
        return "BRAND", name.lower()
    name = brand_denylist.mentions(title, LISTED)
    if name:
        return "TITLE-ONLY", name.lower()
    return "", ""


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    tabs = [sheet_tabs.product_sheet(book)]
    for name in (sheet_tabs.AMAZON_TAB, GERMANY_TAB):
        try:
            tabs.append(book.worksheet(name))
        except Exception as exc:  # noqa: BLE001 - a book without that tab
            print(f"(no {name} tab: {str(exc)[:80]})")
    print(f"brands: {', '.join(n for n, _ in LISTED) or '(none)'} | title lead = first {brand_denylist.LEAD_WORDS} words | tabs: {', '.join(t.title for t in tabs)}")

    hits = []                                   # dicts, one per matching row
    scanned = Counter()
    brand_values = Counter()                    # the Brand cells that matched (spelling census)
    rows_per_sku = defaultdict(list)            # SKU -> [tab row] over Sheet1 + Amazon (what the removal rules look at)
    for ws in tabs:
        values = with_retry(lambda ws=ws: ws.get_all_values(), what=f"read {ws.title}", max_attempts=3)
        if not values:
            continue
        header = [str(h).strip() for h in values[0]]
        ix = {h: i for i, h in enumerate(header) if h}

        def cell(r, k):
            return str(r[ix[k]]).strip() if k in ix and ix[k] < len(r) else ""
        for n, r in enumerate(values[1:], start=2):
            sku = cell(r, "SKU")
            if not sku:
                continue
            scanned[ws.title] += 1
            if ws.title != GERMANY_TAB:
                rows_per_sku[sku].append(f"{ws.title} {n}")
            title = cell(r, "Title") or cell(r, "Product Name") or cell(r, "Name")
            brand = cell(r, "Brand")
            kind, which = classify(brand, title)
            if not kind:
                continue
            if kind == "BRAND" and brand:
                brand_values[brand] += 1
            hits.append({"tab": ws.title, "row": n, "sku": sku, "kind": kind, "which": which, "brand": brand,
                         "stock": cell(r, "Stock"), "price": cell(r, "Selling Price (£)") or cell(r, "Selling Price (€)"),
                         "status": cell(r, "Status"), "created": cell(r, "OnBuy Product Created"), "opc": cell(r, "OPC"),
                         "sync": cell(r, "Sync Status")[:60], "title": title[:70]})

    print("rows scanned: " + ", ".join(f"{t} {scanned[t]}" for t in scanned))
    brand_rows = [h for h in hits if h["kind"] == "BRAND"]
    title_only = [h for h in hits if h["kind"] == "TITLE-ONLY"]

    def in_stock(h):
        try:
            return float(h["stock"].replace(",", "")) > 0
        except ValueError:
            return False

    print(f"\nBRAND ROWS: {len(brand_rows)} ({len({h['sku'] for h in brand_rows})} distinct SKUs)")
    for tab in [t.title for t in tabs]:
        mine = [h for h in brand_rows if h["tab"] == tab]
        if not mine:
            continue
        per = Counter(h["which"] for h in mine)
        print(f"   {tab:8s}: {len(mine):5d} rows | " + ", ".join(f"{k} {v}" for k, v in per.most_common())
              + f" | in stock {sum(1 for h in mine if in_stock(h))} | product created {sum(1 for h in mine if h['created'].upper() == 'TRUE')}"
              + f" | with OPC {sum(1 for h in mine if h['opc'])}")
    print("   Brand cell spellings: " + "; ".join(f"{b!r}: {k}" for b, k in brand_values.most_common(12)))
    no_cell = sum(1 for h in brand_rows if not h["brand"] or h["brand"].lower() in brand_denylist.NO_BRAND)
    print(f"   rows found by the title lead only (Brand cell empty/generic): {no_cell}")

    multi = sorted({h["sku"] for h in brand_rows if h["tab"] != GERMANY_TAB and len(rows_per_sku[h["sku"]]) > 1})
    print(f"\nSKUs on MORE THAN ONE Sheet1/Amazon row (the removal rules hold these back): {len(multi)}")
    for s in multi[:30]:
        print(f"   {s}: " + ", ".join(rows_per_sku[s]))

    print(f"\nTITLE-ONLY rows (another brand in the Brand cell, NOT removed): {len(title_only)}")
    for h in title_only[:SHOW]:
        print(f"   {h['sku']} | {h['tab']} row {h['row']} | brand {h['brand'][:30] or '-'} | stock {h['stock']} | {h['title']}")

    print(f"\nBRAND ROWS (first {SHOW}):  SKU | tab row | brand cell | stock | price | created | OPC | title")
    for h in brand_rows[:SHOW]:
        print(f"   {h['sku']} | {h['tab']} row {h['row']} | {h['brand'][:24] or '-'} | stock {h['stock']} | {h['price']} | {h['created'] or '-'} | {h['opc'] or '-'} | {h['title']}")

    skus = list(dict.fromkeys(h["sku"] for h in brand_rows))
    with open("brand_skus.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(skus) + ("\n" if skus else ""))
    with open("brand_rows.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["tab", "row", "sku", "kind", "which", "brand", "stock", "price", "status", "created", "opc", "sync", "title"])
        w.writeheader()
        w.writerows(hits)
    print(f"\nwrote brand_skus.txt ({len(skus)} SKUs) and brand_rows.csv ({len(hits)} rows incl. title-only)")


if __name__ == "__main__":
    main()
