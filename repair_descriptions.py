"""One-off: re-push clean descriptions to OnBuy for every created
product. eBay seller-template furniture (Eselt banners, payment/returns
boilerplate) leaked into descriptions before the 2026-08-01 sanitize fix
and is baked into the products OnBuy created. This re-sanitizes each
row's stored description with the CURRENT cleaner and PUTs only the
description field via the batch products-update endpoint, keyed by OPC.

Every real-OPC product is pushed (not just marker-matched ones) because
sheets refetch clean text on rotation while OnBuy keeps the old junk -
marker detection would miss those. Over-inclusion costs a handful of
batched calls. DRY_RUN=1 (default) reports; MAX_PRODUCTS caps a run.
"""
import logging
import os
import sys
import time

import requests

from onbuy_client import BASE_URL, OnBuyClient
from sanitize import sanitize_description

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("onbuy_sync")

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or ""
TABLE = "YRA_Full_Feed_Master"

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
MAX_PRODUCTS = int(os.getenv("MAX_PRODUCTS") or "1000")
CHUNK = int(os.getenv("CHUNK") or "50")

_JUNK_MARKERS = ("eselt", "ebay", "send us a message", "seller profile",
                 "30 calendar days", "next working day shipping",
                 # retail description templates (2026-08-20 sweep)
                 "buy it direct", "appliances direct", "laptops direct",
                 "shop all", "your browser does not support", "huge discounts",
                 "sister brands", "safe & secure shopping", "want it sooner")

# Marked-only mode (default ON for the 2026-08-20 fleet sweep): only rows
# whose RAW description carries a junk marker are pushed - template junk
# originates in the SOURCE listing and persists there, so the marker on the
# refetched sheet copy reliably identifies products whose OnBuy copy needs
# the re-push. Keeps the products-queue load proportional to real damage.
SELECT_MARKED = (os.getenv("SELECT_MARKED") or "1").strip().lower() not in ("0", "no", "false")


def fetch_all_rows():
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


def main():
    onbuy = OnBuyClient()
    rows = fetch_all_rows()
    logger.info("Supabase rows: %d", len(rows))

    updates, skipped_empty, had_junk = [], 0, 0
    for r in rows:
        opc = str(r.get("OPC") or "").strip()
        if not opc or opc.upper() == "PENDING":
            continue
        raw = str(r.get("Description") or "")
        clean = sanitize_description(raw)
        if not clean.strip():
            skipped_empty += 1
            continue
        marked = any(m in raw.lower() for m in _JUNK_MARKERS)
        if marked:
            had_junk += 1
        if SELECT_MARKED and not marked:
            continue
        updates.append({"opc": opc, "description": clean,
                        "_sku": str(r.get("SKU") or "")})

    logger.info("Products to re-push: %d (%d visibly contained template junk; "
                "%d skipped with empty descriptions; cap %d)",
                len(updates), had_junk, skipped_empty, MAX_PRODUCTS)
    updates = updates[:MAX_PRODUCTS]
    if DRY_RUN:
        for u in updates[:5]:
            logger.info("  sample: %s (%s) -> %.90s", u["_sku"], u["opc"], u["description"])
        logger.info("DRY RUN - nothing pushed")
        return

    ok = errors = 0
    error_samples = []
    for i in range(0, len(updates), CHUNK):
        chunk = [{"opc": u["opc"], "description": u["description"]}
                 for u in updates[i:i + CHUNK]]
        def _do(payload={"site_id": onbuy.site_id, "seller_id": onbuy.seller_id,
                         "products": chunk}):
            resp = onbuy._send("PUT", f"{BASE_URL}/products",
                               what="products update batch", json=payload, timeout=60)
            resp.raise_for_status()
            return resp.json()
        from retry_utils import with_retry
        body = with_retry(_do, what="products update batch", max_attempts=3)
        results = body.get("results") if isinstance(body, dict) else None
        if isinstance(results, list):
            for item in results:
                err = (item or {}).get("error") or (item or {}).get("message")
                if err and not (item or {}).get("success", True):
                    errors += 1
                    if len(error_samples) < 8:
                        error_samples.append(f"{(item or {}).get('opc')}: {str(err)[:80]}")
                else:
                    ok += 1
        else:
            ok += len(chunk)
        logger.info("chunk %d-%d pushed", i + 1, i + len(chunk))
        time.sleep(2)

    logger.info("DONE: %d description updates accepted, %d per-item errors", ok, errors)
    for e in error_samples:
        logger.info("  error sample: %s", e)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
