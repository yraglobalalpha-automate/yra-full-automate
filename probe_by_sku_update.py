"""One-off (2026-08-14): OnBuy support asked for example SKUs where a
Product Listings: Update by SKU attempt fails for suspended listings.
Attempt a real by-SKU price/stock update per SKU using the row's own
current values from the sheet, and log the verbatim outcome for the
ticket reply. Delete with the rest of the incident tooling."""
import json
import logging
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import OnBuyClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

SHEET_NAME = "YRA_Full_Feed_Master"
SKUS = [s.strip() for s in (os.getenv("PROBE_SKUS") or "").split(",") if s.strip()]


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = gspread.authorize(creds).open(SHEET_NAME).sheet1
    rows = {str(r.get("SKU") or "").strip(): r for r in sheet.get_all_records()}
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    for sku in SKUS:
        r = rows.get(sku)
        if not r:
            log.info("PROBE %s: not in sheet - skipped", sku)
            continue
        try:
            price = float(r.get("Selling Price (\u00a3)") or 0)
            stock = int(float(r.get("Stock") or 0)) or 1
            if price <= 0:
                log.info("PROBE %s: no price on row - skipped", sku)
                continue
            result = onbuy.update_listing(sku=sku, price=price, stock=stock)
            log.info("PROBE %s: SUCCEEDED (%s)", sku, result)
        except Exception as exc:
            log.info("PROBE %s: rejected - %s", sku, exc)


if __name__ == "__main__":
    main()
