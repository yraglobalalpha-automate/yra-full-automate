"""READ-ONLY (2026-10-05): what does OnBuy's queue history say about these SKUs?

A product submission (create / repair re-submit) only returns "accepted into the async queue"; its real outcome - success, or the
error that made OnBuy refuse it - lives in GET /v2/queues, newest first, matched by uid (= our SKU). This pages that history and
prints, per requested SKU, every entry it can still see (status, error message, OPC, dates) plus how far back the history reaches.

Env: SKUS (comma-separated, leading zeros ignored), MAX_PAGES (default 120 pages of 50 = 6,000 entries = 120 GETs of the
240-an-hour quota). Writes nothing anywhere.
"""
import json
import os
from collections import Counter

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import RateLimitError

WANT = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
KEY = {s.lstrip("0") or s for s in WANT}
MAX_PAGES = int(os.getenv("MAX_PAGES") or "120")
# LISTING_SKU: also read this ONE listing (name, stock, price) with a filtered GET - 1-3 requests instead of a 60-page sweep.
LISTING_SKU = (os.getenv("LISTING_SKU") or "").strip()
# PRODUCT_OPCS / PRODUCT_UIDS: also read these catalogue PRODUCTS (name, uid, codes) - which product a listing is attached to.
PRODUCT_OPCS = [s.strip() for s in (os.getenv("PRODUCT_OPCS") or "").split(",") if s.strip()]
PRODUCT_UIDS = [s.strip() for s in (os.getenv("PRODUCT_UIDS") or "").split(",") if s.strip()]
PAGE = 50
DATE_KEYS = ("created_at", "date_created", "created", "date", "updated_at", "processed_at")


def when(entry):
    for k in DATE_KEYS:
        if entry.get(k):
            return str(entry[k])
    return "?"


def probe_listing(onbuy, sku):
    """Which filter spelling does GET /v2/listings honour for one SKU? A filter is honoured when the answer is the SKU alone;
    an ignored filter just returns the first listings of the account (SKU not among them)."""
    base = {"site_id": onbuy.site_id, "limit": 5, "offset": 0}
    for extra in ({"filter[sku]": sku}, {"sku": sku}, {"filter[sku]": sku.lstrip("0") or sku}):
        try:
            r = onbuy._send("GET", f"{BASE_URL}/listings", what="listing filter test", params={**base, **extra}, timeout=60)
            if r.status_code == 429:
                print("listing filter: RATE LIMITED - try again later")
                return
            body = r.json()
            items = (body.get("results") if isinstance(body, dict) else body) or []
            hit = [i for i in items if str((i or {}).get("sku") or "").strip() == sku]
            print(f"listing filter {extra}: HTTP {r.status_code}, {len(items)} item(s) returned, SKU present: {bool(hit)}")
            for i in hit[:1]:
                keep = {k: i.get(k) for k in ("sku", "name", "price", "stock", "product_encoded_id", "updated_at", "created_at")}
                print("LISTING|" + json.dumps(keep, ensure_ascii=False, default=str))
            if hit and len(items) <= 2:
                print("-> this filter is honoured")
                return
        except Exception as exc:  # noqa: BLE001 - read-only diagnostic
            print(f"listing filter {extra}: error {str(exc)[:150]}")


def probe_products(onbuy, opcs, uids):
    """READ-ONLY: GET /v2/products?search=<OPC or barcode> (the endpoint insists on `search`; 'opc'/'uid' are not valid filters)."""
    for value in list(opcs) + list(uids):
        try:
            r = onbuy._send("GET", f"{BASE_URL}/products", what="product search",
                            params={"site_id": onbuy.site_id, "search": value, "limit": 10}, timeout=60)
            if r.status_code == 429:
                print("products: RATE LIMITED - try again later")
                return
            if r.status_code != 200:
                print(f"products search={value}: HTTP {r.status_code} body: {r.text[:300]}")
                continue
            body = r.json()
            items = (body.get("results") if isinstance(body, dict) else body) or []
            print(f"products search={value}: {len(items)} item(s)")
            if items:
                print("  product keys:", sorted((items[0] or {}).keys()))
            for i in items[:6]:
                keep = {k: (str(i.get(k))[:140] if k in ("description", "name", "product_name") else i.get(k))
                        for k in ("opc", "uid", "name", "product_name", "brand_name", "brand", "product_codes", "category_id", "created_at")
                        if k in i}
                print("PRODUCT|" + json.dumps(keep, ensure_ascii=False, default=str)[:700])
        except Exception as exc:  # noqa: BLE001 - read-only diagnostic
            print(f"products search={value}: error {str(exc)[:150]}")


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    for one in [s.strip() for s in LISTING_SKU.split(",") if s.strip()]:
        probe_listing(onbuy, one)
    if PRODUCT_OPCS or PRODUCT_UIDS:
        probe_products(onbuy, PRODUCT_OPCS, PRODUCT_UIDS)
    seen, oldest, hits, shown_keys = 0, None, [], False
    statuses, levels, failed_samples, key_sets = Counter(), Counter(), [], Counter()
    for page in range(MAX_PAGES):
        try:
            result = onbuy.list_queue(limit=PAGE, offset=page * PAGE)
        except RateLimitError as exc:
            print(f"RATE LIMITED at page {page} ({exc}) - reporting what was read so far")
            break
        entries = result.get("results", []) if isinstance(result, dict) else []
        if not entries:
            break
        if not shown_keys:
            print("entry keys:", sorted(entries[0].keys()))
            shown_keys = True
        for e in entries:
            seen += 1
            oldest = when(e)
            statuses[str(e.get("status"))] += 1
            levels[json.dumps(e.get("permitted_write_levels"), sort_keys=True, default=str)] += 1
            key_sets[tuple(sorted(e.keys()))] += 1
            if str(e.get("status")).lower() not in ("success", "pending") and len(failed_samples) < 3:
                failed_samples.append({k: (str(v)[:200]) for k, v in e.items()})
            uid = str(e.get("uid") or "").strip()
            if (uid.lstrip("0") or uid) in KEY:
                hits.append(e)
                print(f"HIT on page {page}: {when(e)} | {json.dumps({k: e.get(k) for k in ('uid', 'status', 'opc', 'queue_id')}, default=str)}")
        if len(entries) < PAGE:
            break
    print(f"queue entries seen: {seen} | the history reaches back to {oldest}")
    print("statuses:", dict(statuses.most_common(8)))
    print("permitted_write_levels:", dict(levels.most_common(8)))
    print("entry key sets:", [(list(k), n) for k, n in key_sets.most_common(4)])
    for sample in failed_samples:
        print("NOT-SUCCESS SAMPLE:", json.dumps(sample, ensure_ascii=False)[:700])
    print(f"entries for the requested SKUs: {len(hits)}")
    for e in sorted(hits, key=when):
        keep = {k: e.get(k) for k in ("uid", "status", "opc", "error_message", "queue_id", "product_url") if k in e}
        print(f"ENTRY {when(e)} | " + json.dumps(keep, ensure_ascii=False, default=str)[:600])
    for s in WANT:
        if not any((str(h.get("uid") or "").lstrip("0") or "") == (s.lstrip("0") or s) for h in hits):
            print(f"NO ENTRY IN THE VISIBLE HISTORY: {s}")


if __name__ == "__main__":
    main()
