"""One robust sweep of GET /v2/listings, shared by the sale-price probe and the sale-price guard (2026-10-10).

Same rules the nightly scans use: 429 / 5xx wait it out (120 s) instead of dying mid-sweep, and a short or empty page is re-fetched once and the
longer answer taken (a truncated page once made Makstore look like 3,499 listings instead of 8,853 - 2026-09-23). On top of that a short page does
not END the sweep - only two empty pages in a row do - and the short pages seen are returned, so a run can say whether the listing count looks
complete. Listings are de-duplicated by SKU (offset paging over a list that changes while it is read can repeat an entry).
"""
import time

from onbuy_client import BASE_URL
from retry_utils import with_retry

LIMIT = 100
MAX_PAGES = 500          # a safety net against a loop, far above any store (Arden ~15k listings = 150 pages)


def has_sale(rec):
    """True when the listing record carries a sale price above zero (what the dashboard shows as "On Sale: GBP x")."""
    v = (rec or {}).get("sale_price")
    try:
        return v is not None and str(v).strip() not in ("", "None", "0", "0.0", "0.00") and float(v) > 0
    except (TypeError, ValueError):
        return False


def fetch_page(onbuy, offset, limit=LIMIT, sleep=time.sleep):
    def _page(off=offset):
        for _try in range(6):
            r = onbuy._send("GET", f"{BASE_URL}/listings", what=f"listings page {off}",
                            params={"site_id": onbuy.site_id, "limit": limit, "offset": off}, timeout=60)
            if r.status_code in (429, 500, 502, 503) and _try < 5:
                print(f"listings page {off}: HTTP {r.status_code} - waiting 120s before retrying", flush=True)
                sleep(120)
                continue
            r.raise_for_status()
            return r
    body = with_retry(_page, what=f"listings page {offset}", max_attempts=3).json()
    items = body.get("results") if isinstance(body, dict) else body
    return (items if isinstance(items, list) else []), body


def sweep(onbuy, limit=LIMIT, sleep=time.sleep):
    """(listings, short_pages, meta, pages): every listing OnBuy returns (full records), the (offset, size) of each page that came back short
    after its re-fetch, the first page's non-result keys, and how many pages were read."""
    out, seen, short, empties, offset, pages, meta = [], set(), [], 0, 0, 0, None
    while pages < MAX_PAGES:
        items, body = fetch_page(onbuy, offset, limit, sleep)
        pages += 1
        if meta is None and isinstance(body, dict):
            meta = {k: v for k, v in body.items() if k != "results"}
        if len(items) < limit:
            again, _ = fetch_page(onbuy, offset, limit, sleep)       # a short / empty page may be a glitch, not the end
            pages += 1
            if len(again) > len(items):
                items = again
        if not items:
            empties += 1
            if empties >= 2:
                break
            offset += limit
            continue
        empties = 0
        if len(items) < limit:
            short.append((offset, len(items)))
        for it in items:
            it = it or {}
            sku = str(it.get("sku") or "").strip()
            if not sku or sku in seen:
                continue
            seen.add(sku)
            out.append(it)
        offset += limit
    return out, short, meta, pages
