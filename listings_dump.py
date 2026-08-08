"""One-off, READ-ONLY: dump this account's live OnBuy listings and this
store's own Supabase rows to CSV artifacts, so listings can be joined
against every store (full + semi) that shares the seller account.
Suspended listings are invisible to GET /v2/listings - absence from the
dump IS the suspension signal once joined against expected-active SKUs."""
import csv
import os

import requests

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry
from supabase_db import TABLE_NAME

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
KEY = os.getenv("SUPABASE_SERVICE_KEY") or ""


def dump_listings():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    with open("account_listings.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sku", "price", "stock", "created_at", "updated_at", "opc"])
        offset, limit, n = 0, 100, 0
        while True:
            def _page(off=offset):
                r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                                params={"site_id": onbuy.site_id, "limit": limit, "offset": off},
                                timeout=60)
                r.raise_for_status()
                return r
            body = with_retry(_page, what=f"listings page {offset}", max_attempts=3).json()
            items = body.get("results") if isinstance(body, dict) else body
            if not isinstance(items, list):
                break
            for it in items:
                it = it or {}
                w.writerow([it.get("sku"), it.get("price"), it.get("stock"),
                            it.get("created_at"), it.get("updated_at"), it.get("opc")])
                n += 1
            if len(items) < limit:
                break
            offset += limit
    print(f"account_listings.csv: {n} rows")


def dump_store_rows():
    if not SUPABASE_URL or not KEY:
        print("no Supabase config - store_rows.csv skipped")
        return
    endpoint = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}"
    headers = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
    with open("store_rows.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sku", "status", "sync_status", "selling_price", "stock", "opc"])
        offset, page, n = 0, 1000, 0
        while True:
            r = requests.get(endpoint, headers=headers, params={
                "select": 'SKU,Status,"Sync Status","Selling Price (£)",Stock,OPC',
                "offset": str(offset), "limit": str(page)}, timeout=30)
            r.raise_for_status()
            rows = r.json()
            for row in rows:
                w.writerow([row.get("SKU"), row.get("Status"), row.get("Sync Status"),
                            row.get("Selling Price (£)"), row.get("Stock"), row.get("OPC")])
                n += 1
            if len(rows) < page:
                break
            offset += page
    print(f"store_rows.csv: {n} rows")


def try_suspended_filters():
    """Does GET /v2/listings support a suspended/status filter? Try the
    plausible parameter spellings; a filter is honored if the first page
    differs from the unfiltered first page."""
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        return
    base = {"site_id": onbuy.site_id, "limit": 25, "offset": 0}
    r = onbuy._send("GET", f"{BASE_URL}/listings", what="baseline page", params=base, timeout=60)
    baseline = [(i or {}).get("sku") for i in (r.json().get("results") or [])]
    candidates = [
        {"filter[status]": "suspended"},
        {"status": "suspended"},
        {"filter[listing_status]": "suspended"},
        {"filter[state]": "suspended"},
    ]
    for extra in candidates:
        try:
            r = onbuy._send("GET", f"{BASE_URL}/listings", what="filter test",
                            params={**base, **extra}, timeout=60)
            body = r.json()
            items = body.get("results") if isinstance(body, dict) else body
            skus = [(i or {}).get("sku") for i in (items or [])]
            honored = skus != baseline
            print(f"filter {extra}: {len(skus or [])} items, differs_from_baseline={honored}")
            if honored and skus:
                first = (items or [{}])[0] or {}
                print(f"  first item: sku={first.get('sku')} price={first.get('price')!r} "
                      f"stock={first.get('stock')!r}")
        except Exception as exc:
            print(f"filter {extra}: error {exc}")


if __name__ == "__main__":
    dump_listings()
    dump_store_rows()
    try_suspended_filters()
