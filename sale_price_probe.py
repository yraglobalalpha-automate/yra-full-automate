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
from collections import Counter

from listing_sweep import has_sale, sweep
from onbuy_client import BASE_URL, OnBuyClient

SKUS = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
SWEEP = (os.getenv("SWEEP") or "").strip().lower() in ("1", "yes", "true")
SAMPLE = int(os.getenv("SAMPLE") or "25")
DUMP = (os.getenv("DUMP") or "").strip()
SALE_KEYS = ("price", "stock", "sale_price", "sale_start_date", "sale_end_date", "created_at", "updated_at", "product_listing_id", "opc", "boost_marketing_commission")
DUMP_KEYS = ("sku", "opc", "product_listing_id", "price", "stock", "sale_price", "sale_start_date", "sale_end_date", "created_at", "updated_at")


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
    print(f"\nSWEEP: {pages} page(s), {len(listings)} distinct listings read | with a sale price set: {len(on_sale)} | the named SKUs found in the sweep: {found_known}")
    print(f"first page metadata: {meta}")
    print(f"short pages (offset, items): {short[:40]}{' ...' if len(short) > 40 else ''} ({len(short)} in all)")
    print("created months, ALL listings:   ", dict(sorted(Counter(str(i.get('created_at'))[:7] for i in listings).items())))
    print("created months, with a sale:    ", dict(sorted(Counter(str(i.get('created_at'))[:7] for i in on_sale).items())))
    print("sale start dates:", dict(Counter(str(i.get("sale_start_date"))[:10] for i in on_sale).most_common(8)))
    print("sale end dates:  ", dict(Counter(str(i.get("sale_end_date"))[:10] for i in on_sale).most_common(8)))
    nosale = [it for it in listings if not has_sale(it)]
    print("created dates of listings WITHOUT a sale (newest 12 days):", dict(sorted(Counter(str(i.get('created_at'))[:10] for i in nosale).items(), reverse=True)[:12]))
    print("stock zero among listings without / with a sale:", sum(1 for i in nosale if str(i.get("stock")) == "0"), "/", sum(1 for i in on_sale if str(i.get("stock")) == "0"))

    def ratio(it):
        try:
            return float(it["sale_price"]) / float(it["price"])
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    rs = sorted(x for x in (ratio(i) for i in on_sale) if x)
    if rs:
        print(f"sale price / price: median {rs[len(rs) // 2]:.3f}, min {rs[0]:.3f}, max {rs[-1]:.3f}")
        print("   ratio bands:", dict(Counter(("<0.7" if x < 0.7 else "0.7-0.9" if x < 0.9 else "0.9-0.99" if x < 0.99 else "0.99-1.0" if x <= 1.0 else ">1.0") for x in rs)))
    for it in on_sale[:SAMPLE]:
        print("ONSALE|" + json.dumps({k: it.get(k) for k in ("sku", "price", "sale_price", "sale_start_date", "sale_end_date", "created_at", "stock")}, default=str))
    if DUMP:
        with open(DUMP, "w", encoding="utf-8") as fh:
            json.dump([{k: it.get(k) for k in DUMP_KEYS} for it in listings], fh)
        print(f"dump written: {DUMP} ({len(listings)} listings)")


if __name__ == "__main__":
    main()
