"""One robust sweep of GET /v2/listings, shared by the sale-price probe and the sale-price guard (2026-10-10).

Same rules the nightly scans use: 429 / 5xx wait it out (120 s) instead of dying mid-sweep, and a short or empty page is re-fetched once and the
longer answer taken (a truncated page once made Makstore look like 3,499 listings instead of 8,853 - 2026-09-23). On top of that a short page does
not END the sweep - only two empty pages in a row do - and the short pages seen are returned, so a run can say whether the listing count looks
complete. Listings are de-duplicated by SKU (offset paging over a list that changes while it is read can repeat an entry).
"""
import json
import time
from collections import Counter

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


DUMP_KEYS = ("sku", "opc", "product_listing_id", "price", "stock", "sale_price", "sale_start_date", "sale_end_date", "created_at", "updated_at")


def dump(listings, path):
    """Write the sweep's selling-price facts (no names, no costs) to a JSON file for offline analysis (the workflow uploads it as an artifact)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([{k: (it or {}).get(k) for k in DUMP_KEYS} for it in listings], fh)


def census_lines(listings, short, meta):
    """Printable facts about a sweep: is it complete-looking, and which listings carry a sale (dates, ratios, creation months)."""
    on_sale = [it for it in listings if has_sale(it)]
    nosale = [it for it in listings if not has_sale(it)]
    month = lambda it: str((it or {}).get("created_at"))[:7]       # noqa: E731
    day = lambda it: str((it or {}).get("created_at"))[:10]        # noqa: E731
    total = (meta or {}).get("total_rows") if isinstance(meta, dict) else None
    coverage = (f"sweep coverage: {len(listings)} of the account's {total} listings ({len(listings) * 100.0 / total:.1f}%)"
                if isinstance(total, int) and total > 0 else "sweep coverage: the answer carries no total_rows")
    lines = [f"{len(listings)} distinct listings, {len(on_sale)} with a sale price; first page metadata: {meta}", coverage,
             f"short pages (offset, items): {short[:40]}{' ...' if len(short) > 40 else ''} ({len(short)} in all)",
             f"created months, ALL listings: {dict(sorted(Counter(month(i) for i in listings).items()))}",
             f"created months, with a sale:  {dict(sorted(Counter(month(i) for i in on_sale).items()))}",
             f"created days of listings WITHOUT a sale (newest 12): {dict(sorted(Counter(day(i) for i in nosale).items(), reverse=True)[:12])}",
             f"sale start dates: {dict(Counter(str(i.get('sale_start_date'))[:10] for i in on_sale).most_common(8))}",
             f"sale end dates:   {dict(Counter(str(i.get('sale_end_date'))[:10] for i in on_sale).most_common(8))}",
             f"stock zero among listings without / with a sale: {sum(1 for i in nosale if str(i.get('stock')) == '0')} / {sum(1 for i in on_sale if str(i.get('stock')) == '0')}"]
    ratios = []
    for it in on_sale:
        try:
            ratios.append(float(it["sale_price"]) / float(it["price"]))
        except (TypeError, ValueError, ZeroDivisionError, KeyError):
            pass
    if ratios:
        ratios.sort()
        bands = Counter("<0.7" if x < 0.7 else "0.7-0.9" if x < 0.9 else "0.9-0.99" if x < 0.99 else "0.99-1.0" if x <= 1.0 else ">1.0" for x in ratios)
        lines.append(f"sale price / price: median {ratios[len(ratios) // 2]:.3f}, min {ratios[0]:.3f}, max {ratios[-1]:.3f}; bands {dict(bands)}")
    return lines
