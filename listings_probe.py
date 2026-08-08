"""One-off, READ-ONLY: page the account's live OnBuy listings and report
the price/stock distribution - specifically how many listings are EMPTY
(no price / no stock), and whether the empty set correlates with recently
created products (the 2026-08-04+ cohort). Changes nothing."""
import json
import os
from collections import Counter

import requests as _requests  # noqa: F401  (retry_utils imports expect it)

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry
from supabase_db import TABLE_NAME

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
KEY = os.getenv("SUPABASE_SERVICE_KEY") or ""


def supabase_recent_skus():
    """SKU -> Last OnBuy Sync for rows with a real OPC (created products)."""
    out = {}
    if not SUPABASE_URL or not KEY:
        return out
    endpoint = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}"
    headers = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
    offset, page = 0, 1000
    while True:
        r = _requests.get(endpoint, headers=headers, params={
            "select": 'SKU,OPC,"Last OnBuy Sync"',
            "offset": str(offset), "limit": str(page)}, timeout=30)
        r.raise_for_status()
        rows = r.json()
        for row in rows:
            opc = str(row.get("OPC") or "").strip().upper()
            if opc and opc != "PENDING":
                out[str(row.get("SKU") or "").strip()] = str(row.get("Last OnBuy Sync") or "")
        if len(rows) < page:
            return out
        offset += page


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")

    listings = []
    offset, limit = 0, 100
    while True:
        def _page(off=offset):
            r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                            params={"site_id": onbuy.site_id, "limit": limit, "offset": off},
                            timeout=60)
            r.raise_for_status()
            return r
        body = with_retry(_page, what=f"listings page offset {offset}", max_attempts=3).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list):
            print(f"unexpected page shape at {offset}: {str(body)[:200]}")
            break
        listings.extend(items)
        if len(items) < limit:
            break
        offset += limit

    print(f"live listings: {len(listings)}")
    if listings:
        print("first item keys:", sorted((listings[0] or {}).keys()))

    def is_empty(it):
        price = it.get("price")
        stock = it.get("stock")
        no_price = price in (None, "", "0.00", "0", 0, 0.0)
        no_stock = stock in (None, "", 0, "0")
        return no_price, no_stock

    empty_both, empty_price, empty_stock = [], 0, 0
    for it in listings:
        np_, ns = is_empty(it or {})
        empty_price += np_
        empty_stock += ns
        if np_ and ns:
            empty_both.append(str((it or {}).get("sku") or ""))
    print(f"no price: {empty_price} | no stock: {empty_stock} | BOTH empty: {len(empty_both)}")

    recent = supabase_recent_skus()
    print(f"supabase rows with real OPC: {len(recent)}")
    tally = Counter()
    for sku in empty_both:
        last = recent.get(sku, "")
        day = last[:10] if last else "no-supabase-date"
        tally[day] += 1
    print("--- BOTH-empty listings by Last OnBuy Sync date ---")
    for day, n in sorted(tally.items()):
        print(f"  {day}: {n}")
    print("--- sample BOTH-empty SKUs (10) ---")
    for sku in empty_both[:10]:
        print(f"  {sku}  last_sync={recent.get(sku, '?')}")
    print("--- sample HEALTHY listing (first with price+stock) ---")
    for it in listings:
        np_, ns = is_empty(it or {})
        if not np_ and not ns:
            print(" ", json.dumps(it)[:220])
            break


if __name__ == "__main__":
    main()
