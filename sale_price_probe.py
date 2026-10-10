"""READ-ONLY (2026-10-10): which OnBuy listings carry a SALE price ("On Sale: GBP x" on the dashboard), and what do their records say?

The user's screenshot: Makstore listing 0000089208441-zzz-40 shows price 67.99 and "On Sale: 65.11" on the OnBuy dashboard (an Amazon discount at the
sourcing that ended up as an OnBuy sale). Our sync only ever sends price / stock / boost. This prints, for the SKUs named, the listing record
exactly as OnBuy answers a filtered GET (every sale_* field), and with SWEEP=1 sweeps all listings and counts the ones with a sale price set -
whether the sweep returns on-sale listings at all is part of the question. Selling prices only (public on OnBuy); no cost, no margin.
Writes nothing.

Env: SKUS (comma-separated), SWEEP (1 = sweep every listing), SAMPLE (how many on-sale rows to print, default 25).
"""
import json
import os
import time
from collections import Counter

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry

SKUS = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
SWEEP = (os.getenv("SWEEP") or "").strip().lower() in ("1", "yes", "true")
SAMPLE = int(os.getenv("SAMPLE") or "25")
SALE_KEYS = ("price", "stock", "sale_price", "sale_start_date", "sale_end_date", "created_at", "updated_at", "product_listing_id", "opc", "boost_marketing_commission")


def has_sale(it):
    v = it.get("sale_price")
    try:
        return v is not None and str(v).strip() not in ("", "None", "0", "0.0", "0.00") and float(v) > 0
    except (TypeError, ValueError):
        return False


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    for sku in SKUS:
        r = onbuy._send("GET", f"{BASE_URL}/listings", what="listing by sku",
                        params={"site_id": onbuy.site_id, "limit": 5, "offset": 0, "filter[sku]": sku}, timeout=60)
        items = (r.json().get("results") if r.status_code == 200 else None) or []
        hit = [i for i in items if str(i.get("sku") or "").strip() == sku]
        if not hit:
            print(f"SKU {sku}: no listing answered the filtered GET (HTTP {r.status_code}, {len(items)} item(s))")
            continue
        h = hit[0]
        print(f"SKU {sku}: " + json.dumps({k: h.get(k) for k in SALE_KEYS}, default=str))
        print(f"   all keys: {sorted(h.keys())}")
    if not SWEEP:
        return
    seen, on_sale, offset, limit = 0, [], 0, 100
    known = set(SKUS)
    found_known = set()
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
            seen += 1
            if str(it.get("sku") or "").strip() in known:
                found_known.add(str(it.get("sku")).strip())
            if has_sale(it):
                on_sale.append(it)
        if len(items) < limit:
            break
        offset += limit
    print(f"\nSWEEP: {seen} listings read | with a sale price set: {len(on_sale)} | the named SKUs found in the sweep: {sorted(found_known)}")
    if on_sale:
        def ratio(it):
            try:
                return float(it["sale_price"]) / float(it["price"])
            except (TypeError, ValueError, ZeroDivisionError):
                return None
        rs = [x for x in (ratio(i) for i in on_sale) if x]
        if rs:
            rs.sort()
            print(f"sale price / price: median {rs[len(rs) // 2]:.3f}, min {rs[0]:.3f}, max {rs[-1]:.3f}")
        print("sale start dates:", dict(Counter(str(i.get("sale_start_date"))[:10] for i in on_sale).most_common(6)))
        print("sale end dates:  ", dict(Counter(str(i.get("sale_end_date"))[:10] for i in on_sale).most_common(6)))
        print("created months:  ", dict(Counter(str(i.get("created_at"))[:7] for i in on_sale).most_common(8)))
        for it in on_sale[:SAMPLE]:
            print("ONSALE|" + json.dumps({k: it.get(k) for k in ("sku", "price", "sale_price", "sale_start_date", "sale_end_date", "created_at", "stock")}, default=str))


if __name__ == "__main__":
    main()
