"""READ-ONLY (2026-11-04): which sheet rows belong to the given brands (default Hisense and Homcom)?
Why: the user ordered every Hisense and Homcom product out of YRA (sheet and OnBuy). This finds them in Sheet1, Amazon and Germany.
A row is a BRAND row when its Brand cell names the brand, or when the Brand cell is empty/generic and the Title starts with the brand name.
A title that only MENTIONS the brand while another brand sits in the Brand cell (an accessory "for Hisense TV") is listed as TITLE-ONLY
and is NOT written to the SKU file - read that list before deleting anything.
Prints no cost cell and no supplier link (the Actions log is public). Writes brand_skus.txt (one SKU per line) and brand_rows.csv.
Env: SHEET_NAME, BRANDS (pipe separated, default Hisense|Homcom), SHOW (max rows printed per group, default 400)."""
import csv
import json
import os
import re
from collections import Counter, defaultdict

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import sheet_tabs
from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
BRANDS = [b.strip() for b in (os.getenv("BRANDS") or "Hisense|Homcom").split("|") if b.strip()]
SHOW = int(os.getenv("SHOW") or "400")
GERMANY_TAB = "Germany"
LEAD_WORDS = 3
NO_BRAND = {"", "-", "n/a", "na", "none", "null", "unbranded", "generic", "no brand", "does not apply", "not applicable"}
BRAND_RX = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(b) for b in BRANDS) + r")(?![A-Za-z0-9])", re.I)


ACCESSORY_WORDS = {"for", "fits", "fit", "with", "compatible", "suits", "suit", "to", "replacement", "spare"}


def title_leads_with_brand(title):
    """True when a brand name sits in the first three words of the title and no 'for / fits / compatible' word comes before it."""
    words = re.findall(r"[A-Za-z0-9®™]+", title or "")[:LEAD_WORDS]
    for i, w in enumerate(words):
        if BRAND_RX.fullmatch(w.strip("®™")):
            return not any(p.lower() in ACCESSORY_WORDS for p in words[:i])
    return False


def classify(brand_cell, title):
    """BRAND / TITLE-ONLY / '' for one row, plus the brand name that matched."""
    m = BRAND_RX.search(brand_cell or "")
    if m:
        return "BRAND", m.group(1).lower()
    if (brand_cell or "").strip().lower() in NO_BRAND and title_leads_with_brand(title):
        return "BRAND", BRAND_RX.search(title).group(1).lower()
    m = BRAND_RX.search(title or "")
    if m:
        return "TITLE-ONLY", m.group(1).lower()
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
    print(f"brands: {', '.join(BRANDS)} | title lead = first {LEAD_WORDS} words | tabs: {', '.join(t.title for t in tabs)}")

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
    no_cell = sum(1 for h in brand_rows if not h["brand"] or h["brand"].lower() in NO_BRAND)
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
