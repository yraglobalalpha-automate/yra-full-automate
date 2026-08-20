"""Buy Box defense engine (2026-08-19, YRA rollout).

Reads the "Competition" tab (the dashboard's Export Listings CSV imported
as-is: sku, price, stock, opc, gtin, suspended_reason, lead_listing_price,
winning_price, winning_status) and the Full sheet's cost data, then
reprices losing listings in PENCE to retake the recommended spot - never
below the sourcing-margin floor.

Policy (user-approved 2026-08-19): this is the ONE place automation may
LOWER a price, and only when all of these hold:
  - the Competition tab shows another seller winning our page
    (winning_status blank/0) at a winning_price below our current price;
  - the row is sheet-managed with a usable Cost Price (floor computable);
  - the new price (winning_price - UNDERCUT_PENCE) stays >= the floor.
If the winner is below our floor: HOLD at floor (set price to floor if
we're above it) and mark HELD - never chase into a loss.
Rows without cost data are logged NO-COST and never touched.

Floor = (cost + shipping) x DEFENSE_MULT (default 1.35 = 20% fee + 15%
profit; user-approved defense-only tier 2026-08-19). The main pipeline's
standard bands are unaffected.

Writes a decision log back into the Competition tab (columns J+: Action,
New Price, Floor, Decided At) and pushes price changes via the batched
by-SKU endpoint. DRY_RUN default on."""
import json
import os
import time
from datetime import datetime, timezone

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import OnBuyClient
from retry_utils import RateLimitError, with_retry

SHEET_NAME = "YRA_Full_Feed_Master"
COMP_TAB = "Competition"
UNDERCUT_PENCE = int(os.getenv("UNDERCUT_PENCE") or "1")
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")


DEFENSE_MULT = float(os.getenv("DEFENSE_MULT") or "1.35")


def floor_price(cost, shipping):
    """Defense-only floor (user-approved 2026-08-19): 20% OnBuy fee + 15%
    profit = x1.35 over cost+shipping. Applies ONLY inside this engine and
    ONLY on pages we are losing - the main pipeline's standard bands are
    untouched, so everyday prices keep their full margins."""
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


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    ss = gspread.authorize(creds).open(SHEET_NAME)
    comp = None
    try:
        comp = ss.worksheet(COMP_TAB)
    except gspread.WorksheetNotFound:
        # Imports often land as "SheetN" - auto-detect any tab whose header
        # row matches the dashboard export format.
        for w in ss.worksheets():
            hdr = [str(h).strip().lower() for h in w.row_values(1)]
            if "winning_status" in hdr and "sku" in hdr:
                comp = w
                print(f"using tab '{w.title}' (export headers detected)")
                break
    if comp is None:
        tabs = [w.title for w in ss.worksheets()]
        raise SystemExit(f"No tab with export headers found - existing tabs: {tabs}")
    main_sheet = ss.sheet1

    cost_by_sku = {}
    for r in main_sheet.get_all_records():
        sku = str(r.get("SKU") or "").strip()
        if not sku:
            continue
        cost = to_f(r.get("Cost Price (£)"))
        ship = to_f(r.get("Shipping Cost (£)")) or 0.0
        if cost:
            cost_by_sku[sku] = (cost, ship)

    rows = comp.get_all_records()
    print(f"competition rows: {len(rows)} | sheet rows with cost: {len(cost_by_sku)}")

    stat_counts = {}
    wp_pop = pr_pop = 0
    for r in rows:
        if not str(r.get("sku") or "").strip():
            continue
        k = str(r.get("winning_status") or "").strip() or "(blank)"
        stat_counts[k] = stat_counts.get(k, 0) + 1
        if to_f(r.get("winning_price")):
            wp_pop += 1
        if to_f(r.get("price")):
            pr_pop += 1
    print(f"export health: winning_status={stat_counts} | winning_price populated={wp_pop} | our price populated={pr_pop}")

    whatif_held = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    decisions = []   # (comp_rownum, action, new_price, floor)
    repricers = []   # (sku, new_price, stock)
    held = no_cost = winning = reprice = 0
    for i, r in enumerate(rows):
        rownum = i + 2
        sku = str(r.get("sku") or "").strip()
        if not sku:
            continue
        status = str(r.get("winning_status") or "").strip()
        our = to_f(r.get("price"))
        win = to_f(r.get("winning_price"))
        stock = int(to_f(r.get("stock")) or 0)
        if status == "1" or not our or not win or win >= our:
            winning += 1
            continue
        if sku not in cost_by_sku:
            no_cost += 1
            decisions.append((rownum, "NO-COST", "", ""))
            continue
        cost, ship = cost_by_sku[sku]
        floor = floor_price(cost, ship)
        if floor is None:
            no_cost += 1
            decisions.append((rownum, "NO-COST", "", ""))
            continue
        target = round(win - UNDERCUT_PENCE / 100.0, 2)
        if target >= floor:
            reprice += 1
            decisions.append((rownum, "REPRICE", f"{target:.2f}", f"{floor:.2f}"))
            repricers.append((sku, target, stock))
        else:
            # Mode A (user decision 2026-08-19): when the winner is below
            # our floor we do NOT sacrifice margin for a position we still
            # would not win - keep the current price and flag the row.
            held += 1
            decisions.append((rownum, "HELD", "", f"{floor:.2f}"))
            whatif_held.append((sku, our, win, cost, ship))

    print(f"winning/no-action: {winning} | reprice: {reprice} | held (floor): {held} | no cost basis: {no_cost}")
    whatif = to_f(os.getenv("WHATIF_MULT") or "")
    if whatif and whatif_held:
        # How many currently-held pages become winnable if the floor were
        # (cost+shipping) x WHATIF_MULT (e.g. 1.35 = 20% fee + 15% profit)?
        winnable = [(s, o, w, round((c + sh) * whatif, 2)) for s, o, w, c, sh in whatif_held
                    if round(w - UNDERCUT_PENCE / 100.0, 2) >= round((c + sh) * whatif, 2)]
        print(f"WHAT-IF x{whatif}: {len(winnable)} of {len(whatif_held)} held page(s) become winnable")
        for s, o, w, f2 in winnable[:10]:
            print(f"  WHATIF {s}: ours={o:.2f} buybox={w:.2f} floor@x{whatif}={f2:.2f}")
    for sku, p, _ in repricers[:8]:
        print(f"  push {sku} -> {p:.2f}")
    if DRY_RUN:
        print("DRY RUN - no prices pushed, no log written")
        return

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    pushed = failed = 0
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

    header_updates = [{"range": "J1", "values": [["Action", "New Price", "Floor", "Decided At"]]}]
    log_updates = [{"range": f"J{rn}:M{rn}", "values": [[a, np, fl, now]]}
                   for rn, a, np, fl in decisions]
    for i in range(0, len(log_updates), 200):
        batch = header_updates + log_updates[i:i + 200] if i == 0 else log_updates[i:i + 200]
        with_retry(lambda b=batch: comp.batch_update([dict(u) for u in b]),
                   what="defense log write", max_attempts=3)
    print(f"decision log written: {len(log_updates)} row(s)")


if __name__ == "__main__":
    main()
