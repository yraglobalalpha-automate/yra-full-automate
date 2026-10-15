"""READ-ONLY (user 2026-10-15: "Make the ghost listings ticket for OnBuy"): the facts OnBuy support needs about the GHOST listings - SKUs that appear in
the account's listing sweep (GET /v2/listings, paged) but that OnBuy's by-SKU endpoints refuse ("SKU does not exist" on every PUT /v2/listings/by-sku,
nothing on GET /v2/listings?filter[sku]=). They carry no price, so nothing of ours can ever update, move or delete them.

For every SKU in SKUS_FILE (one per line): the listing record from the sweep (OPC, listing id, name, price, stock, created, updated, template), the
sheet row that owns it (tab, row, Sync Status, sheet stock, title) and, for the first LOOKUPS of them, a filtered GET by SKU to show the refusal.
Writes ghost_listings.csv (the ticket attachment + our reference). Env: SKUS_FILE, SHEET_NAME, LISTINGS_CACHE, LOOKUPS (default 15). No writes to OnBuy."""
import csv
import json
import os
import time

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import listing_sweep
import listings_cache
import sheet_tabs
from onbuy_client import OnBuyClient
from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
SKUS_FILE = (os.getenv("SKUS_FILE") or "cleanup/ghost_listings_2026-10-15.txt").strip()
LOOKUPS = int(os.getenv("LOOKUPS") or "15")
OUT = "ghost_listings.csv"
FIELDS = ["sku", "opc", "product_listing_id", "name", "price", "stock", "sale_price", "created_at", "updated_at", "delivery_template_id", "product_url",
          "in_sweep", "by_sku_lookup", "sheet_tab", "sheet_row", "sync_status", "sheet_stock", "sheet_title"]


def read_skus(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            s = line.split("#")[0].strip()
            if s and s not in out:
                out.append(s)
    return out


def sheet_index(book):
    """{sku: (tab, row, sync status, stock, title)} over the product tabs."""
    idx = {}
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        amz = book.worksheet(sheet_tabs.AMAZON_TAB)
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    for ws in tabs:
        values = with_retry(ws.get_all_values, what=f"read {ws.title}", max_attempts=3)
        if not values:
            continue
        h = [str(x).strip() for x in values[0]]
        col = {k: h.index(k) for k in ("SKU", "Sync Status", "Stock", "Title") if k in h}
        if "SKU" not in col:
            continue
        for n, r in enumerate(values[1:], start=2):
            cell = lambda k: (str(r[col[k]]).strip() if k in col and col[k] < len(r) else "")   # noqa: E731
            sku = cell("SKU").replace(",", "")
            if sku and sku not in idx:
                idx[sku] = (ws.title, n, cell("Sync Status")[:60], cell("Stock"), cell("Title")[:90])
    return idx


def row_for(sku, rec, sheet, lookup):
    rec = rec or {}
    tab, row, sync, stock, title = sheet.get(sku, ("", "", "", "", ""))
    return {"sku": sku, "opc": rec.get("opc", ""), "product_listing_id": rec.get("product_listing_id", ""), "name": str(rec.get("name") or "")[:120],
            "price": rec.get("price", ""), "stock": rec.get("stock", ""), "sale_price": rec.get("sale_price", ""), "created_at": rec.get("created_at", ""),
            "updated_at": rec.get("updated_at", ""), "delivery_template_id": rec.get("delivery_template_id", ""), "product_url": rec.get("product_url", ""),
            "in_sweep": "yes" if rec else "no", "by_sku_lookup": lookup, "sheet_tab": tab, "sheet_row": row, "sync_status": sync, "sheet_stock": stock,
            "sheet_title": title}


def main():
    skus = read_skus(SKUS_FILE)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    sheet = sheet_index(book)
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    raw = listings_cache.load() if os.getenv("LISTINGS_CACHE") else None
    if raw is None:
        raw, short, meta, pages = listing_sweep.sweep(onbuy)
        print(f"sweep: {len(raw)} listings in {pages} page(s), short pages {len(short)}, metadata {(meta or {}).get('metadata')}")
    live = {str((it or {}).get("sku") or "").strip(): it for it in raw}
    rows = []
    looked = 0
    for sku in skus:
        rec = live.get(sku)
        lookup = ""
        if looked < LOOKUPS:
            looked += 1
            try:
                hit = onbuy.get_listing(sku)
                lookup = "found" if hit else "NOT FOUND (no listing answers to this SKU)"
            except Exception as exc:  # noqa: BLE001
                lookup = f"error: {str(exc)[:60]}"
            time.sleep(0.5)
        rows.append(row_for(sku, rec, sheet, lookup))
    with open(OUT, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    in_sweep = sum(1 for r in rows if r["in_sweep"] == "yes")
    priced = sum(1 for r in rows if str(r["price"]).strip() not in ("", "None"))
    on_sheet = sum(1 for r in rows if r["sheet_tab"])
    print(f"{len(skus)} ghost SKUs: {in_sweep} in the sweep, {priced} with a price, {on_sheet} on the sheet | by-SKU lookups: "
          f"{sum(1 for r in rows if r['by_sku_lookup'].startswith('NOT'))} not found / {sum(1 for r in rows if r['by_sku_lookup'] == 'found')} found of {looked}")
    created = sorted(str(r["created_at"])[:10] for r in rows if r["created_at"])
    if created:
        print(f"created between {created[0]} and {created[-1]}")
    for r in rows[:5]:
        print(f"  {r['sku']} | OPC {r['opc']} | listing {r['product_listing_id']} | price {r['price']} stock {r['stock']} | {r['sheet_tab']} row {r['sheet_row']} {r['sync_status']} | {r['name'][:60]}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
