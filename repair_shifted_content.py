"""One-off (2026-08-14): repair the CSV-era one-row content shift inside
OnBuy's catalogue (232 live listings show a neighbouring row's
title/description/images - order 941214867068 was the live example; our
sheet is verified correct). For every live listing whose OnBuy name does
not match its sheet row's Title, re-submit the product with the row's OWN
content via create_product: uid dedupe + force_update updates the existing
catalogue entry in place (the proven repair_descriptions mechanism), and
since creation IGNORES embedded price/stock by design, the live offer's
price and stock are untouched. Rows must carry Title + Category ID +
image to qualify. DRY_RUN default on; REPAIR_LIMIT caps a run (canary=3).
Changes are queued by OnBuy and take a while to show - rescan later."""
import json
import logging
import os
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import RateLimitError, with_retry

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
REPAIR_LIMIT = int(os.getenv("REPAIR_LIMIT") or "0")  # 0 = no cap


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def similar(a, b):
    ta, tb = set(norm(a).split()), set(norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    listings = {}
    offset, limit = 0, 100
    while True:
        def _page(off=offset):
            r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                            params={"site_id": onbuy.site_id, "limit": limit, "offset": off},
                            timeout=60)
            r.raise_for_status()
            return r
        body = with_retry(_page, what=f"listings page {offset}", max_attempts=3).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            break
        for it in items:
            it = it or {}
            sku = str(it.get("sku") or "").strip()
            if sku:
                listings[sku] = str(it.get("name") or "").strip()
        if len(items) < limit:
            break
        offset += limit
    log.info("live listings: %d", len(listings))

    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = gspread.authorize(creds).open("YRA_Full_Feed_Master").sheet1
    rows = sheet.get_all_records()

    targets, skipped_incomplete = [], 0
    for r in rows:
        sku = str(r.get("SKU") or "").strip()
        if not sku or sku not in listings:
            continue
        title = str(r.get("Title") or "").strip()
        if not title or similar(listings[sku], title) >= 0.5:
            continue
        cat_id = str(r.get("Category ID") or "").strip()
        image = str(r.get("Image URL") or "").strip()
        if not cat_id or not image:
            skipped_incomplete += 1
            continue
        targets.append((sku, r, listings[sku]))
    log.info("mismatched + repairable: %d (skipped incomplete: %d)",
             len(targets), skipped_incomplete)
    if REPAIR_LIMIT:
        targets = targets[:REPAIR_LIMIT]
        log.info("REPAIR_LIMIT: repairing first %d", len(targets))
    for sku, _, lname in targets[:10]:
        log.info("  target %s (onbuy shows: %s)", sku, lname[:60])
    if DRY_RUN:
        log.info("DRY RUN - nothing submitted")
        return

    repaired = failed = 0
    for sku, r, _ in targets:
        digits = re.sub(r"\D", "", sku)
        ean = str(r.get("EAN") or "").strip() or digits
        extra = [u.strip() for u in str(r.get("Additional Images") or "").split(",") if u.strip()]
        try:
            price = float(r.get("Selling Price (£)") or 0) or 0.01
            stock = int(float(r.get("Stock") or 0))
            onbuy.create_product(
                sku=sku, ean=ean,
                title=str(r.get("Title") or "").strip(),
                description=str(r.get("Description") or ""),
                brand=str(r.get("Brand") or "").strip() or "Unbranded",
                category_id=int(float(r.get("Category ID"))),
                price=price,
                main_image=str(r.get("Image URL") or "").strip(),
                additional_images=extra,
                stock=stock,
            )
            repaired += 1
            log.info("REPAIR %s: submitted OK", sku)
        except RateLimitError as exc:
            log.warning("rate limited (%s) - stopping this run; re-dispatch to continue", exc)
            break
        except Exception as exc:
            failed += 1
            log.warning("REPAIR %s: failed - %s", sku, str(exc)[:200])
    log.info("DONE: %d submitted, %d failed (content applies after OnBuy's queue processes)",
             repaired, failed)


if __name__ == "__main__":
    main()
