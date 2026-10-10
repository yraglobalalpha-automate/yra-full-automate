"""READ-ONLY (2026-10-09): every live OnBuy listing of this account with its creation / update time, as a CSV artifact.

Used to answer "which listings are new" (the ones whose product pictures are still loading / pages 404) - GET /v2/listings is the
only listing read there is, and the dump costs one GET per 100 listings. Writes nothing to the Sheet, Supabase or OnBuy.
"""
import csv
import time

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    rows, offset, limit = [], 0, 100
    while True:
        def _page(off=offset):
            for _try in range(6):
                r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                                params={"site_id": onbuy.site_id, "limit": limit, "offset": off}, timeout=60)
                if r.status_code in (429, 500, 502, 503) and _try < 5:
                    time.sleep(60)
                    continue
                r.raise_for_status()
                return r
        body = with_retry(_page, what=f"listings page {offset}", max_attempts=3).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            break
        for it in items:
            it = it or {}
            rows.append([it.get("sku"), it.get("opc") or it.get("product_encoded_id"), it.get("product_listing_id"), it.get("price"),
                         it.get("stock"), it.get("created_at"), it.get("updated_at"), str(it.get("name") or "")[:120], it.get("product_url")])
        if len(items) < limit and not onbuy.more_listings(offset, limit):
            break
        offset += limit
    with open("listings_snapshot.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sku", "opc", "listing_id", "price", "stock", "created_at", "updated_at", "name", "product_url"])
        w.writerows(rows)
    print(f"listings written: {len(rows)}")


if __name__ == "__main__":
    main()
