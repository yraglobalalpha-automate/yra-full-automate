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
SKIP_FIRST = int(os.getenv("SKIP_FIRST") or "0")
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


# When set, SKU->OPC comes from paging GET /listings (live truth): adopted/
# CSV-era rows never had an OPC written to the sheet or mirror, so keying on
# stored OPCs misses most of the catalogue (YRA: 1,359 of ~8k live, 2026-08-23).
USE_LISTINGS_OPC = (os.getenv("USE_LISTINGS_OPC") or "0").strip().lower() in ("1", "yes", "true")
# Restrict to named SKUs (staged verification: the rows the team flagged
# go first, and the marker gate does not apply to a row someone named).
LIMIT_SKUS = {s.strip() for s in (os.getenv("LIMIT_SKUS") or "").split(",") if s.strip()}


def listings_opc_map(onbuy):
    out, offset, limit = {}, 0, 100
    while True:
        def _page(off=offset):
            r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                            params={"site_id": onbuy.site_id, "limit": limit, "offset": off}, timeout=60)
            # The project's classifier, not requests': r.raise_for_status()
            # raises requests.HTTPError, which with_retry has no clause for,
            # so the retry below could never actually fire on the 500s it
            # was written for (2026-08-24 comment, still true until now).
            raise_for_status(r, what="listings page")
            return r
        # OnBuy's listings endpoint 500s intermittently mid-pagination
        # (killed 3 of 7 re-push pages, 2026-08-24) - retry each page.
        body = with_retry(_page, what=f"listings page {offset}", max_attempts=4).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            break
        for it in items:
            it = it or {}
            sku = str(it.get("sku") or "").strip()
            opc = str(it.get("opc") or "").strip()
            if sku and opc and sku not in out:
                out[sku] = opc
        if len(items) < limit:
            break
        offset += limit
        time.sleep(0.3)
    logger.info("listings OPC map: %d live listings", len(out))
    return out


def fetch_all_rows():
    rows, start, page = [], 0, 1000
    while True:
        resp = requests.get(
            f"{SUPABASE_URL}/rest/v1/{TABLE}", params={"select": "*", "order": "SKU"},
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

    _opc_map = listings_opc_map(onbuy) if USE_LISTINGS_OPC else {}
    updates, skipped_empty, had_junk = [], 0, 0
    for r in rows:
        opc = str(r.get("OPC") or "").strip()
        if USE_LISTINGS_OPC:
            opc = _opc_map.get(str(r.get("SKU") or "").strip(), "") or opc
        if not opc or opc.upper() == "PENDING":
            continue
        raw = str(r.get("Description") or "")
        clean = sanitize_description(raw)
        if not clean.strip():
            skipped_empty += 1
            continue
        sku = str(r.get("SKU") or "").strip()
        if LIMIT_SKUS and sku not in LIMIT_SKUS:
            continue
        marked = any(m in raw.lower() for m in _JUNK_MARKERS)
        if marked:
            had_junk += 1
        if SELECT_MARKED and not marked and not LIMIT_SKUS:
            continue
        updates.append({"opc": opc, "description": clean,
                        "_sku": str(r.get("SKU") or "")})

    logger.info("Products to re-push: %d (%d visibly contained template junk; "
                "%d skipped with empty descriptions; cap %d)",
                len(updates), had_junk, skipped_empty, MAX_PRODUCTS)
    updates = updates[SKIP_FIRST:SKIP_FIRST + MAX_PRODUCTS]
    if DRY_RUN:
        for u in updates[:5]:
            logger.info("  sample: %s (%s) -> %.90s", u["_sku"], u["opc"], u["description"])
        logger.info("DRY RUN - nothing pushed")
        return

    ok = errors = 0
    error_samples = []
    poison = []

    def _push(products):
        """One batch. Uses the project's classifier, NOT requests'
        raise_for_status: the latter raises requests.HTTPError, which
        with_retry does not recognise, so a single 500 from the platform
        used to abort the whole run and lose every remaining chunk
        (2026-09-07, chunk at offset 4500)."""
        def _do():
            resp = onbuy._send("PUT", f"{BASE_URL}/products",
                               what="products update batch",
                               json={"site_id": onbuy.site_id, "seller_id": onbuy.seller_id,
                                     "products": products},
                               timeout=60)
            raise_for_status(resp, what="products update batch")
            return resp.json()
        return with_retry(_do, what="products update batch", max_attempts=3)

    def _tally(body, products):
        nonlocal ok, errors
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
            ok += len(products)

    def _push_split(products, depth=0):
        """Retry a batch the platform keeps rejecting by halving it, so one
        product that makes their endpoint 500 costs only itself instead of
        the 50 it travelled with. At size 1 the OPC is recorded and skipped."""
        try:
            _tally(_push(products), products)
            return
        except (TransientError, PermanentError) as exc:
            if len(products) == 1:
                poison.append(products[0]["opc"])
                logger.warning("OPC %s rejected on its own (%s) - skipped",
                               products[0]["opc"], str(exc)[:120])
                return
            half = len(products) // 2
            logger.warning("batch of %d rejected (%s) - splitting", len(products), str(exc)[:100])
            time.sleep(2)
            _push_split(products[:half], depth + 1)
            time.sleep(2)
            _push_split(products[half:], depth + 1)

    for i in range(0, len(updates), CHUNK):
        chunk = [{"opc": u["opc"], "description": u["description"]}
                 for u in updates[i:i + CHUNK]]
        _push_split(chunk)
        logger.info("chunk %d-%d pushed", i + 1, i + len(chunk))
        time.sleep(2)

    logger.info("DONE: %d description updates accepted, %d per-item errors, "
                "%d skipped as unpushable", ok, errors, len(poison))
    for e in error_samples:
        logger.info("  error sample: %s", e)
    if poison:
        logger.info("  OPCs the platform refused individually: %s", ", ".join(poison[:40]))
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
