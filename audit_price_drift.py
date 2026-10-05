"""Price-drift audit (2026-09-17, wrong-priced order incident): compare
every live OnBuy listing against the sheet's Selling Price / Stock and
report drift - the dangerous class is a listing priced BELOW the sheet
(sells at a loss). Each drifted listing's updated_at is printed so the
writer can be identified. FIX=1 pushes the sheet's price and stock back
onto the drifted listings (batched by-SKU updates, 500 per call);
without it this is a pure report. Product tabs only (first + Amazon).
"""
import logging
import os
import time

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import sheet_tabs
import json

from onbuy_client import BASE_URL, OnBuyClient
import held_skus
import listings_cache
from retry_utils import with_retry

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

SHEET_NAME = "YRA_Full_Feed_Master"
FIX = (os.getenv("FIX") or "0").strip().lower() in ("1", "yes", "true")
TOL = float(os.getenv("TOLERANCE") or "0.02")


def sheet_rows():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        amz = book.worksheet("Amazon")
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    out = {}
    for tab in tabs:
        for idx, row in enumerate(tab.get_all_records()):
            sku = str(row.get("SKU") or "").replace(",", "").strip()
            if not sku:
                continue
            try:
                price = float(str(row.get("Selling Price (£)") or "").replace(",", "") or 0)
            except (TypeError, ValueError):
                price = 0.0
            try:
                stock = int(float(str(row.get("Stock") or "").replace(",", "") or 0))
            except (TypeError, ValueError):
                stock = 0
            out.setdefault(sku, (price, stock, tab.title, idx + 2))
    defended = set()
    try:
        bb = book.worksheet("BuyBox")
        # Row 1 is the header and row 2 the run-summary meta row -
        # contested SKUs start at row 3 (buybox_defense.py's writer).
        defended = {str(v).replace(",", "").strip() for v in bb.col_values(1)[2:]}
        defended.discard("")
    except gspread.exceptions.WorksheetNotFound:
        pass
    except Exception as exc:  # noqa: BLE001 - advisory, never fatal
        log.warning("BuyBox tab read failed (%s) - defended-SKU exclusion off", str(exc)[:100])
    return out, defended


def live_listings(onbuy):
    out = {}
    # One shared sweep per nightly job (see listings_cache.py).
    raw_items = listings_cache.load()
    fetched = raw_items is None
    if fetched:
        raw_items = []
    off = 0
    while fetched:
        def _pg(o=off):
            # Wait out OnBuy GET-quota 429s / transient 5xx instead of
            # dying mid-sweep (Arden nightly, 2026-09-20).
            for _try in range(6):
                r = onbuy._send("GET", f"{BASE_URL}/listings", what=f"listings page {o}",
                                params={"site_id": onbuy.site_id, "limit": 100, "offset": o}, timeout=60)
                if r.status_code in (429, 500, 502, 503) and _try < 5:
                    print(f"listings page: HTTP {r.status_code} - waiting 120s before retrying")
                    time.sleep(120)
                    continue
                r.raise_for_status()
                return r
        body = with_retry(_pg, what=f"listings page {off}", max_attempts=3).json()
        items = body.get("results") if isinstance(body, dict) else body
        if isinstance(items, list) and 0 <= len(items) < 100:
            # Short/empty page = possible transient glitch, not the end
            # (Makstore truncated sweep, 2026-09-23) - re-fetch once.
            _b2 = with_retry(_pg, what=f"listings page {off} (end confirm)", max_attempts=3).json()
            _i2 = _b2.get("results") if isinstance(_b2, dict) else _b2
            if isinstance(_i2, list) and len(_i2) > len(items):
                items = _i2
        if not isinstance(items, list) or not items:
            break
        raw_items.extend(items)
        if len(items) < 100:
            break
        off += 100
    if fetched:
        listings_cache.save(raw_items)
    for it in raw_items:
        it = it or {}
        sku = str(it.get("sku") or "").strip()
        if not sku:
            continue
        try:
            price = float(it.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        try:
            stock = int(it.get("stock") or 0)
        except (TypeError, ValueError):
            stock = 0
        out[sku] = (price, stock, str(it.get("updated_at") or ""))
    return out


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    sheet, defended = sheet_rows()
    live = live_listings(onbuy)
    log.info("sheet rows with SKU: %d | live listings: %d", len(sheet), len(live))

    # Listings that show ANOTHER product (hold_at_zero_skus.txt + what tonight's zero step found) never get the sheet's price/stock:
    # they are held at stock 0 (2026-10-05: YRA 993578879973 was re-stocked by this very audit after the zero step and sold - a wrong
    # order). held_zero = those still showing stock; `live` no longer contains any held listing.
    held_zero, live = held_skus.split_held(live, sheet, held_skus.held_set())
    log.info("HELD at stock 0 (OnBuy shows another product): %d still showing stock", len(held_zero))
    for sku, price, ls, tab, rn in held_zero[:40]:
        log.info("  HELD %s [%s row %d]: live stock %d, price %.2f", sku, tab, rn, ls, price)

    under, over, stock_off, under_def = [], [], [], []
    for sku, (lp, lstock, upd) in live.items():
        if sku not in sheet:
            continue
        sp, sstock, tab, rown = sheet[sku]
        if sp <= 0:
            continue
        if lp < sp - TOL:
            # A SKU on the BuyBox tab is priced below the sheet ON
            # PURPOSE (active Buy Box defense, floor-protected - see
            # buybox_defense.py). Fixing it would undo the defense,
            # so it is reported separately and never touched.
            if sku in defended:
                under_def.append((sku, sp, lp, sstock, lstock, upd, tab, rown))
            else:
                under.append((sku, sp, lp, sstock, lstock, upd, tab, rown))
        elif lp > sp + TOL:
            over.append((sku, sp, lp, sstock, lstock, upd, tab, rown))
        elif lstock != sstock:
            stock_off.append((sku, sp, lp, sstock, lstock, upd, tab, rown))

    log.info("UNDERPRICED on OnBuy (listing below sheet): %d", len(under))
    for sku, sp, lp, ss, ls, upd, tab, rn in sorted(under, key=lambda x: x[1] - x[2], reverse=True)[:40]:
        log.info("  %s [%s row %d]: sheet %.2f vs LIVE %.2f (stock %d/%d) updated_at=%s",
                 sku, tab, rn, sp, lp, ss, ls, upd)
    log.info("below sheet but Buy Box-defended (intentional, never fixed): %d", len(under_def))
    log.info("overpriced on OnBuy: %d", len(over))
    for sku, sp, lp, ss, ls, upd, tab, rn in over[:10]:
        log.info("  %s [%s row %d]: sheet %.2f vs LIVE %.2f updated_at=%s", sku, tab, rn, sp, lp, upd)
    log.info("price OK but stock drifted: %d", len(stock_off))
    # The dangerous class (2026-10-02, wrong order YRA SKU 175117835194): the sheet says 0 and OnBuy still
    # sells it. The oversell guard re-zeroes these inside every sync/backfill - this nightly count is its
    # report card (it should read 0); each line shows when OnBuy last touched the listing.
    risky = [t for t in under + over + stock_off + under_def if t[3] == 0 and t[4] > 0]
    log.info("SELLABLE ON ONBUY BUT SHEET SAYS OUT OF STOCK: %d", len(risky))
    for sku, sp, lp, ss, ls, upd, tab, rn in risky[:40]:
        log.info("  RISK %s [%s row %d]: sheet stock 0 vs LIVE stock %d, price %.2f, updated_at=%s", sku, tab, rn, ls, lp, upd)

    if not FIX:
        log.info("REPORT ONLY - set FIX=1 to push the sheet's price/stock onto the drifted listings")
        return
    targets = under + over + stock_off
    if not targets and not held_zero:
        log.info("nothing to fix")
        return
    log.info("FIX: pushing sheet price/stock onto %d listing(s), taking the stock off %d held listing(s)",
             len(targets), len(held_zero))
    payload = [(sku, sp, ss) for sku, sp, lp, ss, ls, upd, tab, rn in targets]
    payload += [(sku, price, 0) for sku, price, ls, tab, rn in held_zero]
    fixed = failed = 0
    for c in range(0, len(payload), 500):
        chunk = payload[c:c + 500]
        results = onbuy.update_listings_by_sku_batch(chunk)
        outcome = {str((r or {}).get("sku") or "").strip(): str((r or {}).get("error") or "").strip()
                   for r in results or []}
        for sku, _p, _s in chunk:
            err = outcome.get(sku, "no answer")
            if err:
                failed += 1
                log.warning("  FIX failed %s: %s", sku, err[:120])
            else:
                fixed += 1
    log.info("FIX done: %d corrected, %d failed", fixed, failed)


if __name__ == "__main__":
    main()
