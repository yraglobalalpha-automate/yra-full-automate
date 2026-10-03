"""READ-ONLY (2026-10-03): which of this account's listings have NO picture on OnBuy?

GET /v2/listings carries every listing's image_url. A product whose images OnBuy never processed shows OnBuy's own
placeholder (…/files/default/product/thumb/default.jpg) there, and its product page stays on the loading / 404 state.
This sweeps every listing, prints COUNTS only (placeholder vs real picture, by day created, in stock or not) and
writes into OUT_DIR:

  no_image_listings.csv   SKU, OPC, Created, Updated, Stock, Name   (the listings that show the placeholder)

Env: SKU_FILE (optional) - a text file of SKUs (one per line, leading zeros ignored); the tally also says how many of
them are among the placeholder listings, which are on OnBuy with a real picture, and which are not on OnBuy at all.
Writes nothing to the sheet, Supabase or OnBuy.
"""
import csv
import os
import time
from collections import Counter

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry

SKU_FILE = (os.getenv("SKU_FILE") or "").strip()
OUT_DIR = os.getenv("OUT_DIR") or "out"
PAGE = 100


def norm(sku):
    s = str(sku or "").replace(",", "").strip()
    return s.lstrip("0") or s


def has_placeholder_image(url):
    """True when a listing's image_url is OnBuy's default picture (or nothing at all) - i.e. no product image."""
    u = str(url or "").strip().lower()
    return (not u) or "/default/product/" in u or u.endswith("/default.jpg")


def tally(listings):
    """(placeholder listings, {created day: count}, real-picture count) from GET /v2/listings records."""
    none = [it for it in listings if has_placeholder_image(it.get("image_url"))]
    by_day = Counter(str(it.get("created_at") or "")[:10] or "?" for it in none)
    return none, dict(sorted(by_day.items())), len(listings) - len(none)


def sweep(onbuy):
    out, offset = [], 0
    while True:
        def _page(off=offset):
            for _try in range(6):
                r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                                params={"site_id": onbuy.site_id, "limit": PAGE, "offset": off}, timeout=60)
                if r.status_code in (429, 500, 502, 503) and _try < 5:
                    time.sleep(60)
                    continue
                r.raise_for_status()
                return r
        body = with_retry(_page, what=f"listings page {offset}", max_attempts=3).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            break
        out += [it or {} for it in items]
        if len(items) < PAGE:
            break
        offset += PAGE
    return out


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    listings = sweep(onbuy)
    none, by_day, real = tally(listings)
    print(f"swept {len(listings)} listings | with a real picture: {real} | showing OnBuy's placeholder: {len(none)}")
    print(f"placeholder listings by day created: {by_day}")
    in_stock = sum(1 for it in none if (it.get("stock") or 0) > 0)
    print(f"placeholder listings in stock: {in_stock} | out of stock: {len(none) - in_stock}")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "no_image_listings.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["SKU", "OPC", "Created", "Updated", "Stock", "Name"])
        for it in sorted(none, key=lambda x: str(x.get("created_at") or "")):
            w.writerow([it.get("sku"), it.get("opc"), it.get("created_at"), it.get("updated_at"),
                        it.get("stock"), str(it.get("name") or "")[:120]])
    if SKU_FILE:
        with open(SKU_FILE, encoding="utf-8") as fh:
            wanted = {norm(line) for line in fh if line.strip()}
        by_sku = {norm(it.get("sku")): it for it in listings}
        on_ob = [k for k in wanted if k in by_sku]
        stuck = [k for k in on_ob if has_placeholder_image(by_sku[k].get("image_url"))]
        print(f"SKU list: {len(wanted)} | on OnBuy: {len(on_ob)} | showing the placeholder: {len(stuck)} | "
              f"on OnBuy WITH a real picture: {len(on_ob) - len(stuck)} | not on OnBuy: {len(wanted) - len(on_ob)}")
        for k in sorted(set(on_ob) - set(stuck))[:30]:
            print("  real picture:", k)
        print(f"placeholder listings NOT in the SKU list: {len(none) - len(stuck)}")


if __name__ == "__main__":
    main()
