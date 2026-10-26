"""Take the stock off the NAMED listings (2026-10-07) - the cheap way: one filtered GET and one PUT per SKU.

zero_stock_mismatched.py sweeps every live listing of the account (~130 requests at Arden's size, over half of OnBuy's 240-an-hour
quota) just to find the mismatched ones; when the SKUs are already known - a listing that shows another product and has just sold -
this reads each listing on its own, and only if it still shows stock sends a by-SKU update with stock 0 and the listing's OWN price
(price is never touched). The listing is then re-read once to confirm.

Env: SKUS (comma-separated, exact SKUs), DRY_RUN (default 1 = read and report, change nothing). Pair it with protected_skus.txt /
hold_at_zero_skus.txt, otherwise the next sync pushes the sheet's stock straight back.
"""
import logging
import os
import time

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import raise_for_status, with_retry

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
WANT = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
READ_TRIES = int(os.getenv("READ_TRIES") or "6")
READ_WAIT = float(os.getenv("READ_WAIT") or "20")


def read_listing(onbuy, sku):
    """The one live listing of a SKU through the filtered GET, or None."""
    def _do():
        r = onbuy._send("GET", f"{BASE_URL}/listings", what=f"listing {sku}",
                        params={"site_id": onbuy.site_id, "limit": 5, "offset": 0, "filter[sku]": sku}, timeout=60)
        raise_for_status(r, what=f"listing {sku}")
        return r
    body = with_retry(_do, what=f"listing {sku}", max_attempts=3).json()
    items = (body.get("results") if isinstance(body, dict) else body) or []
    hit = [i for i in items if str((i or {}).get("sku") or "").strip() == sku]
    if len(hit) > 1:
        raise RuntimeError(f"{len(hit)} listings answer to SKU {sku} - refusing to guess")
    return hit[0] if hit else None


def stock_of(listing):
    try:
        return int(float(str((listing or {}).get("stock") or 0)))
    except (TypeError, ValueError):
        return None


def price_of(listing):
    try:
        return float(str((listing or {}).get("price") or 0))
    except (TypeError, ValueError):
        return 0.0


def main():
    if not WANT:
        raise SystemExit("SKUS required")
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    failed = 0
    for sku in WANT:
        listing = read_listing(onbuy, sku)
        if not listing:
            log.warning("%s: no live listing found - nothing to zero", sku)
            failed += 1
            continue
        stock, price = stock_of(listing), price_of(listing)
        log.info("%s: %r | stock %s | price %.2f | OPC %s", sku, str(listing.get("name"))[:80], stock, price,
                 listing.get("product_encoded_id") or listing.get("opc"))
        if stock == 0:
            log.info("%s: already at stock 0", sku)
            continue
        if stock is None or price <= 0:
            log.warning("%s: unreadable stock/price - left alone", sku)
            failed += 1
            continue
        if DRY_RUN:
            log.info("%s: DRY RUN - would set stock 0 at price %.2f", sku, price)
            continue
        onbuy.update_listing(sku=sku, price=price, stock=0)
        log.info("%s: stock 0 sent (OnBuy's answer echoes it)", sku)
        # OnBuy's reads lag its writes by a minute or more: look again for a while before calling it a problem
        after = None
        for attempt in range(1, READ_TRIES + 1):
            time.sleep(READ_WAIT)
            after = read_listing(onbuy, sku)
            log.info("%s: re-read %d -> stock %s price %.2f", sku, attempt, stock_of(after), price_of(after))
            if stock_of(after) == 0:
                break
        if stock_of(after) != 0:
            log.warning("%s: the listing still READS stock %s after %d looks - the update was accepted, check again later", sku,
                        stock_of(after), READ_TRIES)
            failed += 1
    log.info("DONE%s: %d problem(s)", " (dry run)" if DRY_RUN else "", failed)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
