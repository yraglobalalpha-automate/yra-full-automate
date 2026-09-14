"""READ-ONLY: what the Amazon sync would do with the Amazon tab's rows.

Opens the tab named by SHEET_TAB (default "Amazon"), checks its header row
against everything generate_xml.py writes, fetches the selected rows
through Keepa and prints per row: the offer it would buy from, the price
and stock it would set, the barcodes Keepa knows (and whether the row's
SKU is one of them), images, category tree and freshness. Writes nothing to
the sheet, Supabase or OnBuy. ROWS limits the rows (e.g. "2-50").
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import keepa_client
from retry_utils import with_retry

SHEET_NAME = "YRA_Full_Feed_Master"
TAB = (os.getenv("SHEET_TAB") or "Amazon").strip()
ROWS = (os.getenv("ROWS") or "").strip()

# Columns the sync writes unconditionally (KeyError without them) ...
REQUIRED = ["SKU", "Supplier URL", "Title", "Status", "Last Checked Time", "Cost Price (£)",
            "Stock", "Selling Price (£)", "Description", "Image URL", "Additional Images",
            "Brand", "Last Updated", "Category"]
# ... and the ones it fills when present.
OPTIONAL = ["Category ID", "Price Check Flag", "Condition", "EAN", "Sync Status", "OPC",
            "OnBuy Product Created", "OnBuy Listing Active", "OnBuy Product ID",
            "Last OnBuy Sync", "Product URL", "Shipping Cost (£)", "Listing ID"]
AMAZON_EXTRA = ["ASIN", "Amazon Seller", "Amazon Availability", "Keepa Updated"]


def row_selector(spec):
    ranges = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        ranges.append((min(lo, hi), max(lo, hi)))
    return lambda n: (not ranges) or any(lo <= n <= hi for lo, hi in ranges)


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    print("Tabs:", [w.title for w in book.worksheets()], "(the eBay sync reads the FIRST tab)")
    tab = book.worksheet(TAB)
    headers = [str(h).strip() for h in tab.row_values(1)]
    present = {h for h in headers if h}
    missing = [h for h in REQUIRED if h not in present]
    print(f"\nTab '{TAB}': {len(headers)} header cells")
    print("  required, missing :", missing or "none - OK")
    print("  optional, missing :", [h for h in OPTIONAL if h not in present] or "none")
    print("  Amazon extras     :", {h: (h in present) for h in AMAZON_EXTRA})
    dup = sorted({h for h in headers if h and headers.count(h) > 1})
    if dup:
        print("  DUPLICATED headers:", dup)

    rows = tab.get_all_records()
    rows = [{str(k).strip(): v for k, v in r.items()} for r in rows]
    wanted = row_selector(ROWS)
    picked = []
    for idx, row in enumerate(rows):
        n = idx + 2
        url = str(row.get("Supplier URL") or "").strip()
        if not url or not wanted(n):
            continue
        picked.append((n, row, url, keepa_client.parse_asin(url)))
    print(f"\nRows with a Supplier URL{' in ' + ROWS if ROWS else ''}: {len(picked)} of {len(rows)} data rows")

    ebay_skus = set()
    try:
        first = book.sheet1
        fh = [str(h).strip() for h in first.row_values(1)]
        if "SKU" in fh and first.title != tab.title:
            ebay_skus = {str(v).replace(",", "").strip() for v in first.col_values(fh.index("SKU") + 1)[1:]}
    except Exception as exc:  # noqa: BLE001 - the check is advisory
        print("  (could not read the eBay tab's SKUs for the cross-tab check:", str(exc)[:80], ")")

    asins = [a for _n, _r, _u, a in picked if a]
    client = keepa_client.KeepaClient.from_env()
    products = client.fetch_products(asins) if asins else {}
    print(f"Keepa answered {len(products)} of {len(set(asins))} ASIN(s); tokens used {client.tokens_consumed}, "
          f"left {client.tokens_left}, refill {client.refill_rate}/min, Buy Box detail {'on' if client.use_buybox else 'off'}\n")

    for n, row, url, asin in picked:
        sku = str(row.get("SKU") or "").replace(",", "").strip()
        digits = "".join(ch for ch in sku if ch.isdigit())
        print(f"--- row {n}  SKU {sku or '(blank)'}  ASIN {asin or '(none in link: ' + url[:60] + ')'}")
        if not asin:
            continue
        available, d = keepa_client.get_amazon_data(asin, products)
        print(f"    available: {available} | seller: {d['amazon_seller'] or '-'} | availability: {d['amazon_availability']}")
        if available:
            print(f"    cost £{d['price']:.2f} -> stock {d['stock']} | brand: {d['brand'] or '-'} | title: {d['title'][:80]}")
            print(f"    images: {1 if d['main_image'] else 0} + {len(d['additional_images'])} | description {len(d['description'])} chars")
        # The SKU is the seller's own barcode (same rule as the eBay tab);
        # the manufacturer codes Keepa knows are shown for reference only.
        print(f"    manufacturer barcodes (Keepa): {d['eans'] or '-'}"
              + (" | SKU is one of them" if digits and digits in d['eans'] else ""))
        print(f"    category: {d['category_path'] or '-'} | type hint: {d['product_type'] or '-'} | Keepa updated: {d['keepa_updated'] or '-'}")
        if sku and sku in ebay_skus:
            print("    WARNING: this SKU is already used on the eBay tab - the sync will refuse it")
    print("\nDone - nothing was written.")


if __name__ == "__main__":
    main()
