"""READ-ONLY export for the image scan (user 2026-10-13: find the listings whose images show a 404 on OnBuy, send OnBuy support their SKUs + image URLs).

Writes image_scan_input.csv: every IN-STOCK sheet row (both product tabs) that has a live listing, with the listing's public product_url and the sheet's own
image links (Image URL + Additional Images) - the scan of the public pages runs elsewhere (OnBuy's site challenges GitHub's runners), and the ticket is
built from the sheet's image links exactly like the earlier tickets. One listing sweep + two tab reads; no writes anywhere. Env: SHEET_NAME, LISTINGS_CACHE."""
import csv
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import listing_sweep
import listings_cache
import sheet_tabs
from onbuy_client import OnBuyClient
from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
OUT = "image_scan_input.csv"


def sheet_rows(book):
    out = []
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
        ix = {k: h.index(k) for k in ("SKU", "Stock", "Title", "Image URL", "Additional Images", "Sync Status", "OnBuy Product Created") if k in h}
        for n, r in enumerate(values[1:], start=2):
            cell = lambda k: (str(r[ix[k]]).strip() if k in ix and ix[k] < len(r) else "")   # noqa: E731
            sku = cell("SKU").replace(",", "")
            if not sku or cell("Stock") in ("", "0"):
                continue
            out.append({"sku": sku, "tab": ws.title, "row": n, "title": cell("Title"), "stock": cell("Stock"), "image_url": cell("Image URL"),
                        "additional_images": cell("Additional Images"), "sync": cell("Sync Status")[:40], "created": cell("OnBuy Product Created")})
    return out


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    rows = sheet_rows(book)
    print(f"in-stock sheet rows: {len(rows)} ({sum(1 for r in rows if r['image_url'])} with an Image URL)")
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    raw = listings_cache.load() if os.getenv("LISTINGS_CACHE") else None
    if raw is None:
        raw, short, meta, pages = listing_sweep.sweep(onbuy)
        print(f"sweep: {len(raw)} listings in {pages} page(s), short pages {len(short)}")
    live = {str((it or {}).get("sku") or "").strip(): it for it in raw}
    n = 0
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sku", "tab", "row", "title", "stock", "onbuy_stock", "product_url", "opc", "created_at", "image_url", "additional_images"])
        for r in rows:
            it = live.get(r["sku"])
            if not it:
                continue
            w.writerow([r["sku"], r["tab"], r["row"], r["title"], r["stock"], it.get("stock"), it.get("product_url"), it.get("opc"),
                        str(it.get("created_at") or "")[:10], r["image_url"], r["additional_images"]])
            n += 1
    print(f"{n} in-stock rows with a live listing -> {OUT}")


if __name__ == "__main__":
    main()
