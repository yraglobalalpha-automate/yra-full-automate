"""Buy Box defense engine v2 (2026-08-21): API-driven, hands-free.

OnBuy support named GET /v2/listings/check-winning (per SKU: our price, the
Buy Box "lead" price and a winning flag), so the engine no longer needs the
dashboard export. Every run:
  1. pages GET /listings (sku, price, stock) - the live catalogue;
  2. asks check-winning for every in-stock, unprotected SKU in batches;
  3. classifies each contested page and reprices in PENCE to retake the
     recommended spot - never below the sourcing-margin floor;
  4. writes the contested picture to a "BuyBox" tab in the Full sheet.

Policy (user-approved 2026-08-19, mode A): this is the ONE place automation
may LOWER a price, and only when all of these hold:
  - OnBuy says another seller holds the Buy Box (winning=false) at a
    lead_price BELOW our current price;
  - the row is sheet-managed with a usable Cost Price (floor computable);
  - the new price (lead_price - UNDERCUT_PENCE) stays >= the floor.
If the winner is below our floor: HOLD (keep price, flag HELD) - never chase
into a loss. If we are already cheaper than the lead yet not winning, the
box is decided by something other than price (ratings/delivery) - no action,
flagged CHEAPER-NOT-WINNING, so we never bleed margin for nothing.
Rows without cost data are logged NO-COST and never touched.
Listings in protected_skus.txt (content incident) are skipped entirely.

Floor = (cost + shipping) x DEFENSE_MULT (default 1.35 = 20% fee + 15%
profit; defense-only tier). The main pipeline's standard bands are untouched.
DRY_RUN default on for manual runs; the daily schedule runs live. Pushes are
forced to dry when the store's ONBUY_API_PUSH_ENABLED is not true."""
import json
import os
import re
import time
from datetime import datetime, timezone

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import PermanentError, RateLimitError, with_retry

SHEET_NAME = "YRA_Full_Feed_Master"
LOG_TAB = "BuyBox"
UNDERCUT_PENCE = int(os.getenv("UNDERCUT_PENCE") or "1")
DEFENSE_MULT = float(os.getenv("DEFENSE_MULT") or "1.35")
CHECK_BATCH = int(os.getenv("CHECK_BATCH") or "500")  # OnBuy: max 1,000 SKUs/request, no separate rate limit (support, 2026-08-24)
PUSH_ENABLED = (os.getenv("ONBUY_API_PUSH_ENABLED") or "").strip().lower() == "true"
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "") or not PUSH_ENABLED


def _load_protected():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "protected_skus.txt")
    out = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    out.add(line)
    return out


PROTECTED_SKUS = _load_protected()


def floor_price(cost, shipping):
    base = cost + shipping
    if base <= 0:
        return None
    return round(base * DEFENSE_MULT, 2)


def to_f(v):
    try:
        f = float(str(v).replace(",", "").strip())
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def page_listings(onbuy):
    """{sku: (price, stock)} for every live listing (dedupe repeats)."""
    out = {}
    offset, limit = 0, 100
    while True:
        def _page(off=offset):
            r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                            params={"site_id": onbuy.site_id, "limit": limit, "offset": off}, timeout=60)
            r.raise_for_status()
            return r
        body = with_retry(_page, what=f"listings page {offset}", max_attempts=3).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            break
        for it in items:
            it = it or {}
            sku = str(it.get("sku") or "").strip()
            if sku and sku not in out:
                try:
                    stock = int(float(it.get("stock") or 0))
                except (TypeError, ValueError):
                    stock = 0
                out[sku] = (to_f(it.get("price")), stock)
        if len(items) < limit:
            break
        offset += limit
        time.sleep(0.3)
    return out


def check_all(onbuy, skus):
    """check-winning in batches; halves a batch on a 4xx (size limit unknown)."""
    out = {}
    queue = [skus[i:i + CHECK_BATCH] for i in range(0, len(skus), CHECK_BATCH)]
    calls = 0
    while queue:
        chunk = queue.pop(0)
        try:
            res = onbuy.check_winning(chunk) or []
            calls += 1
        except RateLimitError:
            print("burst limit - waiting 90s")
            time.sleep(90)
            queue.insert(0, chunk)
            continue
        except PermanentError as exc:
            if len(chunk) > 10:
                half = len(chunk) // 2
                queue.insert(0, chunk[half:])
                queue.insert(0, chunk[:half])
                print(f"check-winning rejected a batch of {len(chunk)} ({str(exc)[:80]}) - splitting")
                continue
            print(f"check-winning failed for {len(chunk)} SKU(s): {str(exc)[:120]}")
            continue
        for r in res:
            r = r or {}
            sku = str(r.get("sku") or "").strip()
            if sku:
                out[sku] = (to_f(r.get("price")), to_f(r.get("lead_price")), bool(r.get("winning")))
        time.sleep(1.0)
    print(f"check-winning calls: {calls} | answers: {len(out)}")
    return out


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    ss = gspread.authorize(creds).open(SHEET_NAME)
    main_sheet = ss.sheet1
    cost_by_sku = {}
    main_rows = with_retry(lambda: main_sheet.get_all_records(), what="sheet read", max_attempts=3)
    # Cost map keys come from the SKU column's DISPLAYED text - numericise
    # strips leading zeros, and live SKUs carry them (see generate_xml.py's
    # matching overlay, 2026-08-27); a stripped key would never match the
    # live listing and the row would sit NO-COST despite a filled cost.
    _hdrs = [str(h).strip() for h in with_retry(lambda: main_sheet.row_values(1), what="headers", max_attempts=3)]
    _sku_display = with_retry(lambda: main_sheet.col_values(_hdrs.index("SKU") + 1),
                              what="sku display col", max_attempts=3)
    for _i, r in enumerate(main_rows):
        sku = str(_sku_display[_i + 1]).replace(",", "").strip() if _i + 1 < len(_sku_display) else str(r.get("SKU") or "").strip()
        if not sku:
            continue
        cost = to_f(r.get("Cost Price (£)"))
        ship = to_f(r.get("Shipping Cost (£)")) or 0.0
        if cost:
            cost_by_sku[sku] = (cost, ship)
    print(f"sheet rows with cost: {len(cost_by_sku)} | protected: {len(PROTECTED_SKUS)} | push enabled: {PUSH_ENABLED} | dry run: {DRY_RUN}")

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    listings = page_listings(onbuy)
    candidates = [s for s, (p, st) in listings.items() if st > 0 and p and s not in PROTECTED_SKUS]
    print(f"live listings: {len(listings)} | in-stock unprotected candidates: {len(candidates)}")
    win = check_all(onbuy, candidates)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    counts = {"winning": 0, "no data": 0, "cheaper-not-winning": 0, "no cost": 0, "reprice": 0, "held": 0}
    log_rows, repricers = [], []
    for sku in candidates:
        our_list, stock = listings[sku]
        our, lead, winning = win.get(sku, (None, None, None))
        our = our or our_list
        if winning is None or not our:
            counts["no data"] += 1
            continue
        if winning:
            counts["winning"] += 1
            continue
        if not lead:
            counts["no data"] += 1
            continue
        if lead >= our:
            counts["cheaper-not-winning"] += 1
            log_rows.append([sku, f"{our:.2f}", f"{lead:.2f}", "no", "CHEAPER-NOT-WINNING", "", "", now])
            continue
        if sku not in cost_by_sku:
            counts["no cost"] += 1
            log_rows.append([sku, f"{our:.2f}", f"{lead:.2f}", "no", "NO-COST", "", "", now])
            continue
        cost, ship = cost_by_sku[sku]
        floor = floor_price(cost, ship)
        if floor is None:
            counts["no cost"] += 1
            log_rows.append([sku, f"{our:.2f}", f"{lead:.2f}", "no", "NO-COST", "", "", now])
            continue
        target = round(lead - UNDERCUT_PENCE / 100.0, 2)
        if target >= floor:
            counts["reprice"] += 1
            log_rows.append([sku, f"{our:.2f}", f"{lead:.2f}", "no", "REPRICE", f"{target:.2f}", f"{floor:.2f}", now])
            repricers.append((sku, target, stock))
        else:
            counts["held"] += 1
            log_rows.append([sku, f"{our:.2f}", f"{lead:.2f}", "no", "HELD", "", f"{floor:.2f}", now])
    print("summary: " + " | ".join(f"{k}: {v}" for k, v in counts.items()))
    for sku, p, _ in repricers:
        print(f"  push {sku} -> {p:.2f}")

    pushed = failed = 0
    if repricers and not DRY_RUN:
        for c0 in range(0, len(repricers), 500):
            chunk = repricers[c0:c0 + 500]
            try:
                results = onbuy.update_listings_by_sku_batch(chunk)
            except RateLimitError:
                print(f"burst limit at {c0} - waiting 90s")
                time.sleep(90)
                results = onbuy.update_listings_by_sku_batch(chunk)
            errs = {str((it or {}).get("sku") or "").strip(): str((it or {}).get("error") or "").strip()
                    for it in results}
            for sku, _, _ in chunk:
                if errs.get(sku, "missing"):
                    failed += 1
                else:
                    pushed += 1
            time.sleep(1.0)
        print(f"pushed: {pushed} | failed: {failed}")
    elif repricers:
        print("DRY RUN - no prices pushed")

    # Contested picture -> "BuyBox" tab (replaced every run).
    try:
        try:
            tab = ss.worksheet(LOG_TAB)
        except gspread.WorksheetNotFound:
            tab = ss.add_worksheet(title=LOG_TAB, rows=max(200, len(log_rows) + 20), cols=10)
        header = [["SKU", "Our Price", "Buy Box Price", "Winning", "Action", "New Price", "Floor", "Decided At"],
                  [f"run {now}", f"candidates {len(candidates)}", f"winning {counts['winning']}",
                   f"reprice {counts['reprice']} (pushed {pushed})", f"held {counts['held']}",
                   f"cheaper-not-winning {counts['cheaper-not-winning']}", f"no cost {counts['no cost']}",
                   "DRY RUN" if DRY_RUN else "LIVE"]]
        with_retry(lambda: tab.clear(), what="buybox tab clear", max_attempts=3)
        body = header + log_rows
        for i in range(0, len(body), 500):
            chunk = body[i:i + 500]
            with_retry(lambda c=chunk, off=i: tab.update(f"A{off + 1}", c, value_input_option="RAW"),
                       what="buybox tab write", max_attempts=3)
        print(f"BuyBox tab written: {len(log_rows)} contested row(s)")
    except Exception as exc:
        print(f"BuyBox tab write failed: {exc}")


if __name__ == "__main__":
    main()
