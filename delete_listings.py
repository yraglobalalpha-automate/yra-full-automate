"""By-SKU listing deletion (2026-08-15). Deletes the given SKUs' listings
from OnBuy via DELETE /v2/listings/by-sku - the API equivalent of the
dashboard delete the user has been doing by hand for wrong-content
listings. SAFETY: runs ONLY on an explicitly supplied SKU list (no
default, no scan) and DRY_RUN is on unless dispatched with dry_run=no.
For recreate flows, run reset_deleted_rows on the same SKUs AFTER the
deletion - deletion alone leaves the rows update-only (Synced+OPC), and
resetting before deletion would re-attach creates to the old products."""
import logging
import os
import time

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import RateLimitError, raise_for_status, with_retry

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

SKUS = [s.strip() for s in (os.getenv("DELETE_SKUS") or "").split(",") if s.strip()]
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")


def main():
    if not SKUS:
        raise SystemExit("DELETE_SKUS is empty - this tool never runs without an explicit list")
    log.info("SKUs to delete: %d", len(SKUS))
    for s in SKUS[:10]:
        log.info("  %s", s)
    if DRY_RUN:
        log.info("DRY RUN - nothing deleted")
        return
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    deleted = failed = 0
    idx = 0
    while idx < len(SKUS):
        sku = SKUS[idx]
        try:
            def _do(sku=sku):
                resp = onbuy._send(
                    "DELETE", f"{BASE_URL}/listings/by-sku",
                    what=f"onbuy delete_listing({sku})",
                    json={"site_id": onbuy.site_id, "skus": [sku]},
                )
                log.info("DELETE %s raw response [%s]: %s", sku, resp.status_code, resp.text[:500])
                raise_for_status(resp, what=f"onbuy delete_listing({sku})")
                return resp
            with_retry(_do, what=f"onbuy delete_listing({sku})", max_attempts=3)
            deleted += 1
            log.info("DELETED %s", sku)
            idx += 1
        except RateLimitError:
            log.warning("burst limit at %d/%d - waiting 90s and continuing", idx, len(SKUS))
            time.sleep(90)
            continue
        except Exception as exc:
            failed += 1
            log.warning("DELETE %s failed - %s", sku, str(exc)[:200])
            idx += 1
        time.sleep(1.0)
    log.info("DONE: %d deleted, %d failed", deleted, failed)


if __name__ == "__main__":
    main()
