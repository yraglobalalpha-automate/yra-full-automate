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

Floor = (cost + shipping) x DEFENSE_MULT (default 1.4375 = a 15% profit that
SURVIVES the 20% fee, which OnBuy charges on the selling price - see
pricing.py, 2026-09-01; defense-only tier, thinner than the main bands on
purpose). The main pipeline's standard bands are untouched.
DRY_RUN default on for manual runs; the daily schedule runs live. Pushes are
forced to dry when the store's ONBUY_API_PUSH_ENABLED is not true."""
import json
import os
import re
import time
from datetime import datetime, timezone

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import sheet_tabs

import fees
import pricing
from onbuy_client import BASE_URL, OnBuyClient
import sku_aliases
from retry_utils import PermanentError, RateLimitError, with_retry

SHEET_NAME = "YRA_Full_Feed_Master"
LOG_TAB = "BuyBox"
UNDERCUT_PENCE = int(os.getenv("UNDERCUT_PENCE") or "1")
# The floor keeps DEFENSE_PROFIT_PERCENT of the SELLING price after the
# commission (user 2026-09-28) - the old 15%-of-cost floor left only
# ~11% of the sale and read as a loss-level margin on the dashboard.
DEFENSE_PROFIT_PERCENT = float(os.getenv("DEFENSE_PROFIT_PERCENT") or "15")
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


def floor_price(cost, shipping, rule=None):
    """The lowest price that still leaves DEFENSE_PROFIT_PERCENT of the
    SELLING price after the commission - the category's real tier when
    the store runs in FEE_MODE=category, else the flat rate (with the
    1.5-point uplift either way)."""
    base = cost + shipping
    if base <= 0:
        return None
    p = pricing.price_for_margin_of_price(base, DEFENSE_PROFIT_PERCENT, rule)
    return round(p, 2) if p else None


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
            # Wait out OnBuy GET-quota 429s / transient 5xx instead of
            # dying mid-sweep (same hardening as the content tools).
            for _try in range(6):
                r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                                params={"site_id": onbuy.site_id, "limit": limit, "offset": off}, timeout=60)
                if r.status_code in (429, 500, 502, 503) and _try < 5:
                    print(f"listings page: HTTP {r.status_code} - waiting 120s before retrying")
                    time.sleep(120)
                    continue
                r.raise_for_status()
                return r
        body = with_retry(_page, what=f"listings page {offset}", max_attempts=3).json()
        items = body.get("results") if isinstance(body, dict) else body
        if isinstance(items, list) and 0 <= len(items) < limit:
            # A short (or empty) page can be a transient OnBuy glitch, not
            # the end of the list: one truncated sweep undercounted
            # Makstore's live listings 3,499 vs 8,853 (2026-09-23) and
            # briefly looked like a mass deletion. Re-fetch once and take
            # the longer answer; a real final page repeats itself.
            _b2 = with_retry(_page, what=f"listings page {offset} (end confirm)", max_attempts=3).json()
            _i2 = _b2.get("results") if isinstance(_b2, dict) else _b2
            if isinstance(_i2, list) and len(_i2) > len(items):
                items = _i2
        if not isinstance(items, list) or not items:
            break
        for it in items:
            it = it or {}
            sku = sku_aliases.to_true(it.get("sku"))
            if sku and sku not in out:
                try:
                    stock = int(float(it.get("stock") or 0))
                except (TypeError, ValueError):
                    stock = 0
                out[sku] = (to_f(it.get("price")), stock)
        if len(items) < limit and not onbuy.more_listings(offset, limit):
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
    main_sheet = sheet_tabs.product_sheet(ss)
    cost_by_sku = {}
    sell_by_sku = {}
    # Restore-on-contest-end (2026-09-28, Arden wrong-price order
    # 890197849991): a defended price stayed at the floor after its
    # contest ended - the nightly audit had excluded it while the OLD
    # tab still listed it, so the floor survived up to two more days.
    # The defense now restores leavers itself: previous-tab SKUs that
    # are no longer contested go back to the sheet's selling price.
    prev_contested = set()
    try:
        _old_tab = ss.worksheet(LOG_TAB)
        prev_contested = {str(v).replace(",", "").strip()
                          for v in with_retry(lambda: _old_tab.col_values(1),
                                              what="old buybox tab", max_attempts=3)[2:]}
        prev_contested.discard("")
    except gspread.WorksheetNotFound:
        pass
    main_rows = with_retry(lambda: main_sheet.get_all_records(), what="sheet read", max_attempts=3)
    # Cost map keys come from the SKU column's DISPLAYED text - numericise
    # strips leading zeros, and live SKUs carry them (see generate_xml.py's
    # matching overlay, 2026-08-27); a stripped key would never match the
    # live listing and the row would sit NO-COST despite a filled cost.
    _hdrs = [str(h).strip() for h in with_retry(lambda: main_sheet.row_values(1), what="headers", max_attempts=3)]
    # Bracketed consistent read (2026-08-31): column read BEFORE and AFTER
    # the records read must match - the two-reads-after guard missed edits
    # landing between the records read and the first column read.
    for _stab in range(3):
        _sku_display = with_retry(lambda: main_sheet.col_values(_hdrs.index("SKU") + 1),
                                  what="sku display col", max_attempts=3)
        main_rows = with_retry(lambda: main_sheet.get_all_records(), what="sheet read", max_attempts=3)
        _sku_display_2 = with_retry(lambda: main_sheet.col_values(_hdrs.index("SKU") + 1),
                                    what="sku display recheck", max_attempts=3)
        if _sku_display_2 == _sku_display:
            break
        print("Sheet changed during the read - re-reading for a consistent snapshot")
    else:
        print("Sheet still being edited after 3 re-reads - aborting this run")
        raise SystemExit(1)
    for _i, r in enumerate(main_rows):
        sku = str(_sku_display[_i + 1]).replace(",", "").strip() if _i + 1 < len(_sku_display) else str(r.get("SKU") or "").strip()
        if not sku:
            continue
        cost = to_f(r.get("Cost Price (£)"))
        ship = to_f(r.get("Shipping Cost (£)")) or 0.0
        if cost:
            cost_by_sku[sku] = (cost, ship, fees.rule_for_category_path(r.get("Category")))
        sell = to_f(r.get("Selling Price (£)"))
        if sell:
            sell_by_sku[sku] = sell
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
    no_data_skus = set()
    for sku in candidates:
        our_list, stock = listings[sku]
        our, lead, winning = win.get(sku, (None, None, None))
        our = our or our_list
        if winning is None or not our:
            counts["no data"] += 1
            no_data_skus.add(sku)
            continue
        if winning:
            counts["winning"] += 1
            continue
        if not lead:
            counts["no data"] += 1
            no_data_skus.add(sku)
            continue
        if lead >= our:
            counts["cheaper-not-winning"] += 1
            log_rows.append([sku, f"{our:.2f}", f"{lead:.2f}", "no", "CHEAPER-NOT-WINNING", "", "", now])
            continue
        if sku not in cost_by_sku:
            counts["no cost"] += 1
            log_rows.append([sku, f"{our:.2f}", f"{lead:.2f}", "no", "NO-COST", "", "", now])
            continue
        cost, ship, rule = cost_by_sku[sku]
        floor = floor_price(cost, ship, rule)
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
            # A below-floor contest is ABANDONED, not merely held (user
            # 2026-09-28): the Buy Box is never chased under the margin
            # bar, so a live price already sitting under the floor goes
            # back to the sheet price (or the floor when the sheet has
            # none) instead of quietly selling on at the old level.
            if our < floor - 0.011:
                _back = sell_by_sku.get(sku) or floor
                if _back > our + 0.011:
                    repricers.append((sku, round(_back, 2), stock))
                    log_rows.append([sku, f"{our:.2f}", f"{lead:.2f}", "no", "HELD-RESTORED", f"{_back:.2f}", f"{floor:.2f}", now])
                else:
                    log_rows.append([sku, f"{our:.2f}", f"{lead:.2f}", "no", "HELD", "", f"{floor:.2f}", now])
            else:
                log_rows.append([sku, f"{our:.2f}", f"{lead:.2f}", "no", "HELD", "", f"{floor:.2f}", now])
    # A SKU on the previous tab that is no longer contested (and not
    # merely unknown this run) goes back to the sheet price now, not
    # at the next audit. If a competitor undercuts again, the next
    # defense run re-lowers it - same equilibrium, hours sooner.
    contested_now = {r[0] for r in log_rows}
    restorers = []
    for sku in sorted(prev_contested - contested_now - set(PROTECTED_SKUS) - no_data_skus):
        if sku not in listings or sku not in sell_by_sku:
            continue
        live_p, stock = listings[sku]
        target = round(sell_by_sku[sku], 2)
        if live_p and target > live_p + 0.011:
            restorers.append((sku, target, stock))
    counts["restored"] = len(restorers)
    print("summary: " + " | ".join(f"{k}: {v}" for k, v in counts.items()))
    for sku, p, _ in repricers:
        print(f"  push {sku} -> {p:.2f}")
    for sku, p, _ in restorers:
        print(f"  restore {sku} -> {p:.2f} (contest ended)")

    pushed = failed = 0
    pushers = repricers + restorers
    if pushers and not DRY_RUN:
        for c0 in range(0, len(pushers), 500):
            chunk = pushers[c0:c0 + 500]
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
    elif pushers:
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
