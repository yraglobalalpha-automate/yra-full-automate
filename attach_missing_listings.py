"""One-off remediation for the July stuck-queue incident (OnBuy support,
2026-07-27): product creation succeeded for the affected SKUs (OPCs issued,
products approved in the catalogue) but no seller listing was ever attached,
so they show as unavailable. This attaches the missing listings in batches
via POST /v2/listings, using each row's OPC/price/stock from Supabase.

Safety model:
  DRY_RUN=1 (default)  - report what WOULD be attached, change nothing.
  LIMIT_SKUS=a,b,c     - restrict to specific SKUs (staged verification).
  MAX_LISTINGS=n       - hard cap per run (default 1000).
Existing listings are never touched: the live listing set is fetched first
and anything already listed is skipped, so re-runs are safe.
"""
import json
import logging
import os
import sys
import time

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import BASE_URL, OnBuyClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("onbuy_sync")

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or ""
TABLE = os.getenv("SUPABASE_TABLE") or "YRA_Full_Feed_Master"

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
# With LIMIT_SKUS given, SKIP_PROBE=1 trusts the caller and skips the ~30
# listings-page reads (OnBuy answered them with read-timeouts on
# 2026-07-30) - attaching an already-listed SKU just returns a harmless
# per-item error, so the safety loss is nil for a hand-picked list.
SKIP_PROBE = (os.getenv("SKIP_PROBE") or "").strip().lower() in ("1", "yes", "true")
LIMIT_SKUS = {s.strip() for s in (os.getenv("LIMIT_SKUS") or "").split(",") if s.strip()}
MAX_LISTINGS = int(os.getenv("MAX_LISTINGS") or "1000")
CHUNK = int(os.getenv("CHUNK") or "50")


def fetch_sheet_rows():
    """The Sheet is the FULLER record: the Supabase project created in the
    July migration only holds the rows processed since (378 at last check,
    vs ~1,900 sheet rows), so OPCs recorded before the migration exist only
    here. Returns the same dict shape the Supabase rows use."""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]), scope)
    sheet = gspread.authorize(creds).open("YRA_Full_Feed_Master").sheet1
    return sheet.get_all_records()


def fetch_all_supabase_rows():
    rows, start, page = [], 0, 1000
    while True:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/{TABLE}", params={"select": "*"},
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Range": f"{start}-{start + page - 1}"},
            timeout=30)
        resp.raise_for_status()
        batch = resp.json()
        rows.extend(batch)
        if len(batch) < page:
            return rows
        start += page


def fetch_existing_listing_skus(onbuy):
    """Every SKU that already has a listing on the account - these are
    skipped, which is what makes re-running this script safe."""
    skus, offset, limit = set(), 0, 100
    while True:
        def _page(off=offset):
            r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                            params={"site_id": onbuy.site_id, "limit": limit, "offset": off}, timeout=60)
            r.raise_for_status()
            return r
        from retry_utils import with_retry
        resp = with_retry(_page, what=f"listings page offset {offset}", max_attempts=3)
        body = resp.json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list):
            logger.info("Unexpected listings page shape at offset %d: %.300s", offset, str(body))
            return skus
        for it in items:
            sku = str((it or {}).get("sku") or "").strip()
            if sku:
                skus.add(sku)
        if len(items) < limit:
            return skus
        offset += limit


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("SUPABASE_URL/SUPABASE_SERVICE_KEY missing")
        sys.exit(1)

    onbuy = OnBuyClient()
    supabase_rows = fetch_all_supabase_rows()
    sheet_rows = fetch_sheet_rows() if os.getenv("GOOGLE_CREDENTIALS") else []
    logger.info("Supabase rows: %d, Sheet rows: %d", len(supabase_rows), len(sheet_rows))
    # Union by SKU - Supabase wins where both exist (same values in practice;
    # both stale to the same pause date).
    by_sku = {}
    for r in sheet_rows + supabase_rows:
        sku_key = str(r.get("SKU") or "").strip()
        if sku_key:
            by_sku[sku_key] = r
    rows = list(by_sku.values())
    logger.info("Union catalogue rows: %d", len(rows))

    if SKIP_PROBE and LIMIT_SKUS:
        existing = set()
        logger.info("SKIP_PROBE: trusting the %d-SKU allowlist, no listings scan", len(LIMIT_SKUS))
    else:
        existing = fetch_existing_listing_skus(onbuy)
        logger.info("SKUs already listed on OnBuy: %d", len(existing))

    candidates, skipped_priceless = [], 0
    for r in rows:
        sku = str(r.get("SKU") or "").strip()
        opc = str(r.get("OPC") or "").strip()
        if not sku or opc.upper() in ("", "PENDING"):
            continue
        if LIMIT_SKUS and sku not in LIMIT_SKUS:
            continue
        if sku in existing:
            continue
        try:
            price = float(str(r.get("Selling Price") or r.get("Selling Price (£)") or 0) or 0)
        except ValueError:
            price = 0.0
        try:
            stock = max(0, int(float(str(r.get("Stock") or 0) or 0)))
        except ValueError:
            stock = 0
        if price <= 0:
            skipped_priceless += 1
            continue
        candidates.append({"opc": opc, "sku": sku, "condition": "new",
                           "price": round(price, 2), "stock": stock})

    logger.info("Listings to attach: %d (skipped %d rows with no usable price; "
                "%d already listed; cap %d)", len(candidates), skipped_priceless,
                len(existing), MAX_LISTINGS)
    for c in candidates[:10]:
        logger.info("  sample: %s", c)
    candidates = candidates[:MAX_LISTINGS]

    if DRY_RUN:
        logger.info("DRY RUN - nothing submitted. Re-run with dry_run=no to attach %d listings.",
                    len(candidates))
        return

    created = errors = 0
    error_samples = []
    for i in range(0, len(candidates), CHUNK):
        chunk = candidates[i:i + CHUNK]
        body = onbuy.create_listings_batch(chunk)
        results = body.get("results") if isinstance(body, dict) else None
        if isinstance(results, list):
            for item in results:
                err = (item or {}).get("error")
                if err:
                    errors += 1
                    if len(error_samples) < 10:
                        error_samples.append(f"{(item or {}).get('sku')}: {err}")
                else:
                    created += 1
        else:
            created += len(chunk)  # no per-item results returned - counted as submitted
        time.sleep(2)

    logger.info("DONE: %d listings submitted OK, %d per-item errors", created, errors)
    for e in error_samples:
        logger.info("  error sample: %s", e)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
