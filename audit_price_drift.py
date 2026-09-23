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
import json

from onbuy_client import BASE_URL, OnBuyClient
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
    tabs = [book.sheet1]
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
    off = 0
    while True:
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
        for it in items:
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
        if len(items) < 100:
            break
        off += 100
    return out


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    sheet, defended = sheet_rows()
    live = live_listings(onbuy)
    log.info("sheet rows with SKU: %d | live listings: %d", len(sheet), len(live))

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

    if not FIX:
        log.info("REPORT ONLY - set FIX=1 to push the sheet's price/stock onto the drifted listings")
        return
    targets = under + over + stock_off
    if not targets:
        log.info("nothing to fix")
        return
    log.info("FIX: pushing sheet price/stock onto %d listing(s)", len(targets))
    payload = [(sku, sp, ss) for sku, sp, lp, ss, ls, upd, tab, rn in targets]
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
