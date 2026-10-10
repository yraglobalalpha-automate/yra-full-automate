"""READ-ONLY (2026-10-10): which OnBuy listings carry a SALE price ("On Sale: GBP x" on the dashboard), and what do their records say?

The user's screenshot: Makstore listing 0000089208441-zzz-40 shows price 67.99 and "On Sale: 65.11" on the OnBuy dashboard (an Amazon discount at the
sourcing that ended up as an OnBuy sale). Our sync only ever sends price / stock / boost. This prints, for the SKUs named, the listing record
exactly as OnBuy answers a filtered GET (every sale_* field), and with SWEEP=1 sweeps all listings and counts the ones with a sale price set.
Selling prices only (public on OnBuy); no cost, no margin. Writes nothing (DUMP=<path> writes a local JSON file of the sweep for the workflow to upload).

Sweep rule (listing_sweep.py, changed 2026-10-10): a page shorter than the limit does NOT end the sweep - only two empty pages in a row do. The first
sweep of the day stopped at 9,199 listings and missed the April-created listings the user named; the short pages seen are printed, so a run says whether
the listing count looks complete.

Env: SKUS (comma-separated), SWEEP (1 = sweep every listing), SAMPLE (how many on-sale rows to print, default 25), DUMP (path of the JSON dump).
"""
import json
import os

from listing_sweep import census_lines, dump, has_sale, sweep
from onbuy_client import BASE_URL, OnBuyClient

SKUS = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
SWEEP = (os.getenv("SWEEP") or "").strip().lower() in ("1", "yes", "true")
SAMPLE = int(os.getenv("SAMPLE") or "25")
DUMP = (os.getenv("DUMP") or "").strip()
SALE_KEYS = ("price", "stock", "sale_price", "sale_start_date", "sale_end_date", "created_at", "updated_at", "product_listing_id", "opc", "boost_marketing_commission")


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    for n, sku in enumerate(SKUS):
        r = onbuy._send("GET", f"{BASE_URL}/listings", what="listing by sku",
                        params={"site_id": onbuy.site_id, "limit": 5, "offset": 0, "filter[sku]": sku}, timeout=60)
        body = r.json() if r.status_code == 200 else {}
        if n == 0 and isinstance(body, dict):
            print("response keys:", {k: (f"[{len(v)} item(s)]" if k == "results" else v) for k, v in body.items()})
        items = (body.get("results") if isinstance(body, dict) else None) or []
        hit = [i for i in items if str(i.get("sku") or "").strip() == sku]
        if not hit:
            print(f"SKU {sku}: no listing answered the filtered GET (HTTP {r.status_code}, {len(items)} item(s))")
            continue
        h = hit[0]
        print(f"SKU {sku}: " + json.dumps({k: h.get(k) for k in SALE_KEYS}, default=str))
        print(f"   all keys: {sorted(h.keys())}")
    if not SWEEP:
        return
    listings, short, meta, pages = sweep(onbuy)
    on_sale = [it for it in listings if has_sale(it)]
    known = set(SKUS)
    found_known = sorted(str(it.get("sku") or "").strip() for it in listings if str(it.get("sku") or "").strip() in known)
    print(f"SWEEP: {pages} request(s), the named SKUs found in the sweep: {found_known}")
    for line in census_lines(listings, short, meta):
        print(line)
    for it in on_sale[:SAMPLE]:
        print("ONSALE|" + json.dumps({k: it.get(k) for k in ("sku", "price", "sale_price", "sale_start_date", "sale_end_date", "created_at", "stock")}, default=str))
    if DUMP:
        dump(listings, DUMP)
        print(f"dump written: {DUMP} ({len(listings)} listings)")


if __name__ == "__main__":
    main()
