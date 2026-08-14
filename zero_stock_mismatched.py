"""One-off (2026-08-14): two wrong-item orders in 24h while the 210
content repairs sit in OnBuy's pending queue. Emergency lever: set stock
to 0 on EVERY currently-mismatched listing (shift + collision) via the
by-SKU listings endpoint, which acts instantly and bypasses the product
queue. Nothing is deleted; price and content stay untouched. Stock comes
back automatically: shift rows are restored by normal rotation once their
queued content fix lands; collision rows stay for the user's manual
delist+relist worklist (re-run this pass if rotation re-pushes stock
before that's done). DRY_RUN default on. Prints one parseable line per
target for the user's review sheet."""
import json
import logging
import os
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import RateLimitError, with_retry

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def similar(a, b):
    ta, tb = set(norm(a).split()), set(norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    listings = {}
    offset, limit = 0, 100
    while True:
        def _page(off=offset):
            r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                            params={"site_id": onbuy.site_id, "limit": limit, "offset": off},
                            timeout=60)
            r.raise_for_status()
            return r
        body = with_retry(_page, what=f"listings page {offset}", max_attempts=3).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            break
        for it in items:
            it = it or {}
            sku = str(it.get("sku") or "").strip()
            if sku:
                listings[sku] = (str(it.get("name") or "").strip(),
                                 str(it.get("stock") or "").strip())
        if len(items) < limit:
            break
        offset += limit
    log.info("live listings: %d", len(listings))

    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = gspread.authorize(creds).open("YRA_Full_Feed_Master").sheet1
    rows = sheet.get_all_records()

    targets = []
    for i, r in enumerate(rows):
        sku = str(r.get("SKU") or "").strip()
        if not sku or sku not in listings:
            continue
        title = str(r.get("Title") or "").strip()
        lname, lstock = listings[sku]
        if not title or similar(lname, title) >= 0.5:
            continue
        # Already at zero (a previous pass got it, or it was empty anyway):
        # nothing to protect - skipping makes every pass pure progress, so
        # the chain converges instead of redoing the same head each run.
        try:
            if int(float(lstock or 0)) == 0:
                continue
        except (TypeError, ValueError):
            pass
        above = str(rows[i - 1].get("Title") or "").strip() if i else ""
        kind = "shift" if above and similar(lname, above) >= 0.5 else "collision"
        try:
            price = float(r.get("Selling Price (£)") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            price = 0.01
        targets.append((i + 2, sku, kind, lname, title, price, lstock))
    log.info("mismatched listings to zero: %d", len(targets))
    for t in targets:
        log.info("TARGET|%d|%s|%s|%.2f|%s|%s|%s",
                 t[0], t[1], t[2], t[5], t[6], t[3][:70], t[4][:70])
    if DRY_RUN:
        log.info("DRY RUN - no stock changed")
        return

    zeroed = failed = 0
    for rownum, sku, kind, _, _, price, _ in targets:
        try:
            onbuy.update_listing(sku=sku, price=price, stock=0)
            zeroed += 1
            log.info("ZEROED %s (%s, row %d)", sku, kind, rownum)
        except RateLimitError as exc:
            log.warning("rate limited (%s) - stopping; re-dispatch to continue", exc)
            break
        except Exception as exc:
            failed += 1
            log.warning("ZERO %s failed - %s", sku, str(exc)[:150])
    log.info("DONE: %d zeroed, %d failed, %d untouched", zeroed, failed,
             len(targets) - zeroed - failed)


if __name__ == "__main__":
    main()
