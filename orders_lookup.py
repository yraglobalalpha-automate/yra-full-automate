"""READ-ONLY: find the OnBuy orders that contain a SKU (SKU env) among the
most recent orders, and print ONLY order id / status / dates / product lines -
never buyer details. Pages GET /v2/orders newest first and stops when the
orders get older than DAYS days (default 14) or after MAX_PAGES pages.

Built 2026-10-02 to date a "wrong order" (a sale of a product already out of
stock at its source) against the sync's own timeline. Writes nothing."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

from onbuy_client import BASE_URL, OnBuyClient

SKU = (os.getenv("SKU") or "").strip()
DAYS = int(os.getenv("DAYS") or "14")
MAX_PAGES = int(os.getenv("MAX_PAGES") or "30")
SAFE_ORDER_KEYS = ("order_id", "id", "status", "order_status", "date", "created", "created_at", "order_date",
                   "modified", "updated_at", "date_modified", "total", "currency", "currency_code", "site_id",
                   "delivery_service", "dispatched_at", "cancelled_at", "refunded_at")
SAFE_PRODUCT_KEYS = ("sku", "name", "opc", "quantity", "unit_price", "price", "status", "product_name",
                     "product_listing_id", "dispatched_at")


def walk(node):
    """Every dict nested anywhere inside node."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def order_time(o):
    for k in ("date", "created", "created_at", "order_date"):
        v = o.get(k)
        if isinstance(v, str) and v[:4].isdigit():
            try:
                return datetime.strptime(v[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
    return None


def main():
    if not SKU:
        raise SystemExit("SKU required")
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=DAYS)
    seen = hits = 0
    oldest = None
    offset = 0
    for page in range(MAX_PAGES):
        params = {"site_id": onbuy.site_id, "limit": 100, "offset": offset, "sort[created]": "desc"}
        resp = onbuy._send("GET", f"{BASE_URL}/orders", what="orders page", params=params, timeout=90)
        if resp.status_code == 429:
            print("rate limited - waiting 90s")
            time.sleep(90)
            continue
        if resp.status_code != 200:
            print(f"HTTP {resp.status_code}: {resp.text[:300]}")
            if page == 0:
                # a second try without the sort parameter
                params.pop("sort[created]")
                resp = onbuy._send("GET", f"{BASE_URL}/orders", what="orders page", params=params, timeout=90)
                print(f"retry without sort -> HTTP {resp.status_code}: {resp.text[:200] if resp.status_code != 200 else 'ok'}")
                if resp.status_code != 200:
                    return
            else:
                return
        body = resp.json()
        results = body.get("results") if isinstance(body, dict) else body
        if not isinstance(results, list) or not results:
            break
        if page == 0:
            print("order keys:", sorted(results[0].keys()) if isinstance(results[0], dict) else type(results[0]))
            meta = body.get("metadata") if isinstance(body, dict) else None
            if meta:
                print("metadata:", json.dumps(meta)[:200])
        for o in results:
            seen += 1
            t = order_time(o)
            if t and (oldest is None or t < oldest):
                oldest = t
            skus = {str(d.get("sku")).strip() for d in walk(o) if d.get("sku") is not None}
            if os.getenv("LIST_ALL"):
                print(f"ORDER-LINE {o.get('order_id')} | {o.get('status')} | {o.get('date')} | SKUs {sorted(skus)}")
            if SKU in skus or SKU.lstrip("0") in {s.lstrip("0") for s in skus}:
                hits += 1
                print("---- ORDER CONTAINING THE SKU")
                _paid = ", ".join(k for k in ("paypal_capture_id", "stripe_transaction_id") if o.get(k))
                print("  paid via (which payment id is set):", _paid or "(neither)")
                print("  dispatched flag:", o.get("dispatched"), "| shipped_at:", o.get("shipped_at"))
                for k in SAFE_ORDER_KEYS:
                    if k in o and not isinstance(o[k], (dict, list)):
                        print(f"  {k}: {o[k]}")
                for d in walk(o):
                    if d.get("sku") is not None and str(d.get("sku")).strip().lstrip("0") == SKU.lstrip("0"):
                        print("  product line keys:", sorted(d.keys()))
                        for k in SAFE_PRODUCT_KEYS:
                            if k in d and not isinstance(d[k], (dict, list)):
                                print(f"  product.{k}: {d[k]}")
        print(f"page {page + 1}: scanned {seen} order(s) so far, oldest {oldest}")
        if oldest and oldest < cutoff:
            break
        if len(results) < 100:
            break
        offset += 100
        time.sleep(1.0)
    print(f"DONE: scanned {seen} order(s), {hits} contained SKU {SKU}; oldest order seen {oldest}")


if __name__ == "__main__":
    main()
