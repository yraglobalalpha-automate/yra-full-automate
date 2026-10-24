"""READ-ONLY (2026-10-24): what the AMAZON_OWN_OFFER_ONLY rule would do to this store - counts of the Amazon-sourced rows by the
seller Keepa last reported ("Amazon", "3rd party (Buy Box)", "3rd party (lowest New)", none) and by stock, with sample SKUs of the rows
that are live on a third-party offer only. Reads the sheet; no Keepa, no OnBuy, no writes. Never prints a cost."""
import json
import os
from collections import Counter, defaultdict

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import sheet_tabs
from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
SAMPLE = int(os.getenv("SAMPLE") or "15")


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        amz = book.worksheet(sheet_tabs.AMAZON_TAB)
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    total = Counter()
    live = Counter()
    samples = defaultdict(list)
    for ws in tabs:
        values = with_retry(lambda ws=ws: ws.get_all_values(), what=f"read {ws.title}", max_attempts=3)
        header = [str(h).strip() for h in values[0]]
        ix = {h: i for i, h in enumerate(header) if h}
        if "Amazon Seller" not in ix:
            print(f"tab {ws.title!r}: no 'Amazon Seller' column - skipped")
            continue

        def cell(r, col):
            i = ix.get(col)
            return str(r[i]).strip() if i is not None and i < len(r) else ""
        rows = 0
        for n, r in enumerate(values[1:], start=2):
            url = cell(r, "Supplier URL").lower()
            if "amazon." not in url and "amzn." not in url:
                continue
            rows += 1
            seller = cell(r, "Amazon Seller") or "(none recorded)"
            try:
                stock = float(cell(r, "Stock") or "0")
            except ValueError:
                stock = 0.0
            status = cell(r, "Sync Status")[:12]
            total[seller] += 1
            if stock > 0:
                live[seller] += 1
                if seller.startswith("3rd party") and len(samples[seller]) < SAMPLE:
                    samples[seller].append(f"{cell(r, 'SKU')} (row {n}, {status or 'no status'})")
        print(f"tab {ws.title!r}: {rows} Amazon-sourced rows")
    print("\nAmazon-sourced rows by the seller Keepa last reported (all / with stock above 0):")
    for seller, n in total.most_common():
        print(f"  {seller:28s} {n:6d} / {live.get(seller, 0):6d}")
    third = sum(v for k, v in live.items() if k.startswith("3rd party"))
    print(f"\nWould go to stock 0 under AMAZON_OWN_OFFER_ONLY=1 (live on a third-party offer only): {third}")
    for seller, skus in samples.items():
        print(f"  e.g. {seller}: " + "; ".join(skus))


if __name__ == "__main__":
    main()
