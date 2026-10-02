"""READ-ONLY: print OnBuy's own listing record (OPC, name, price, stock,
suspension fields) for a list of SKUs, found by sweeping GET /v2/listings.
Built 2026-10-01 to put exact OPCs/reasons into a support ticket about
suspended listings the API refuses to delete. Writes nothing.

Env: SKUS (comma-separated).
"""
import json
import os
import time

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry

WANT = {s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()}
ZL = {s.lstrip("0") or "0" for s in WANT}


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    found, offset, limit, seen = {}, 0, 100, 0
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
            sku = str(it.get("sku") or "").strip()
            seen += 1
            if sku in WANT or (sku.lstrip("0") or "0") in ZL:
                found[sku] = it
        if len(items) < limit:
            break
        offset += limit
    print(f"swept {seen} listings; matched {len(found)} of {len(WANT)} requested SKUs")
    for sku, it in found.items():
        print("LISTING|" + json.dumps(it, ensure_ascii=False, default=str))
    for s in sorted(WANT):
        if s not in found and (s.lstrip("0") or "0") not in {k.lstrip("0") or "0" for k in found}:
            print(f"NOT IN SWEEP: {s}")


if __name__ == "__main__":
    main()
