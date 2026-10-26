"""STORE SALE (user 2026-10-15): one click puts a sale percentage on every listing of the store, one click takes it off again - both through the API,
both within a minute (proven on 2026-10-15: on after 15 s, off after 60 s; the OnBuy dashboard starts a sale at once but takes 1-2 days to end one).

How a sale works on OnBuy (our own findings, 2026-10-10 + the test of 2026-10-15): a listing carries price + sale_price + sale_start_date /
sale_end_date ("YYYY-MM-DD HH:MM:SS"); while the window is open the shop sells at the sale price and shows "On Sale". PUT /v2/listings/by-sku takes the
three sale fields on their own (no price, no stock, so it never races with the sync, which only ever sends price / stock). There is no empty spelling
to END a sale: send the listing's own sale price with a window in the past - OnBuy drops it within about a minute. Read-backs lag by seconds.

THE MARGIN FLOOR (user 2026-10-15: "add the margin floor before I use it"): a sale price may never leave less than MIN_PROFIT_PERCENT profit on the
cost base, in the stores' own terms - price = (cost + shipping) x (1 + profit%) / (1 - fee%), fee = the sheet's Fee % cell (OnBuy's commission with
VAT, as the sync records it; DEFAULT_FEE_PERCENT when the cell is blank). floor = (cost + shipping) x (1 + MIN_PROFIT_PERCENT/100) / (1 - fee/100).
A listing whose sale price would fall under its floor is left out of the sale (FLOOR_MODE=skip, default) or put on sale at the floor instead
(FLOOR_MODE=cap, only when that still gives at least 1% off). A listing with no cost on any sheet is left out - no cost, no guarantee. Costs come
from the full sheet (both product tabs) and, when set, the semi sheet (SEMI_SHEET_NAME). MIN_PROFIT_PERCENT=off switches the floor off (not advised).

PRICE RANGES (user 2026-10-26, for the dashboard: "select the price ranges of products to apply sale on them"): PRICE_RANGES = the price ranges and the sale % of each -
JSON [[from, to, percent], ...] or the text "0-9.99:5; 20-:3" (from / to empty = open ended; percent 0 = keep that range out of the sale). Both ends are included, the
FIRST range that contains a listing's price decides, and a price outside every range gets no sale. With PRICE_RANGES set, PERCENT and EXCLUDE_PRICE_* are ignored. The ranges
are kept in store_sale.json (the nightly refresh applies the same rules) and the plan prints one RANGE line per range for the dashboard. The logs carry no { or } (the runners
mask them as ***): counts are printed as name=count.

PRICE EXCLUSION (GTV, user 2026-10-26: "Apply 3% sale on all GTV products except the 10-20 gbp range products"): EXCLUDE_PRICE_FROM / EXCLUDE_PRICE_TO keep
every listing whose price lies in that range (both ends included) out of the sale. The range is stored in store_sale.json, so the nightly refresh applies the
same rule; a listing that is kept out for any reason (its price moved into the range, under its floor ...) but still carries THIS sale (same last day) gets that
sale ended. A sale price that would not be below the listing price (listings under about GBP 0.34) is skipped too.

MODE=plan (default): sweep (or the nightly's shared sweep), census of the sales on the account, what apply would send - writes nothing.
MODE=test: ONE listing (TEST_SKU): floor check, put the sale on, read back until it shows, take it off, read back until it is gone - the timeline.
MODE=apply: PERCENT off every listing with a price (SCOPE=all | in_stock) above its floor, window yesterday 00:00:00 .. SALE_END 23:59:59; idempotent
(a listing already on exactly this sale is skipped); writes store_sale.json - the state the nightly sale guard respects and the refresh step reads.
MODE=cancel: end the sale on every listing that carries one (past window) and delete store_sale.json.
MODE=refresh (nightly, while store_sale.json is active): listings created since and listings whose price changed get the sale recomputed with the
same percentage and floor; an expired state does nothing (OnBuy drops an expired sale itself; the guard ends leftovers).
Guards: PERCENT 1..90, MAX_CHANGES (0 = no cap), sale price never below MIN_SALE_PRICE.
Env: MODE, PERCENT, SALE_END (YYYY-MM-DD, default today + 30 days), SCOPE, PRICE_RANGES, EXCLUDE_PRICE_FROM, EXCLUDE_PRICE_TO, TEST_SKU, MAX_CHANGES, CHUNK (500), LISTINGS_CACHE, STATE_FILE
(store_sale.json), SAMPLE (8), MIN_SALE_PRICE (0.01), MIN_PROFIT_PERCENT (10), FLOOR_MODE (skip | cap), DEFAULT_FEE_PERCENT (18), SHEET_NAME,
SEMI_SHEET_NAME, GOOGLE_CREDENTIALS. Prints SKUs, prices and counts - never a cost."""
import csv
import json
import os
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import listing_sweep
import listings_cache
import sale_guard
import sheet_tabs
from onbuy_client import OnBuyClient
from retry_utils import with_retry

MODE = (os.getenv("MODE") or "plan").strip().lower()
PERCENT = (os.getenv("PERCENT") or "").strip()
SALE_END = (os.getenv("SALE_END") or "").strip()
SCOPE = (os.getenv("SCOPE") or "all").strip().lower()
TEST_SKU = (os.getenv("TEST_SKU") or "").strip()
MAX_CHANGES = int(os.getenv("MAX_CHANGES") or "0")
CHUNK = int(os.getenv("CHUNK") or "500")
STATE_FILE = (os.getenv("STATE_FILE") or "store_sale.json").strip()
SAMPLE = int(os.getenv("SAMPLE") or "8")
MIN_SALE_PRICE = float(os.getenv("MIN_SALE_PRICE") or "0.01")
MIN_PROFIT = (os.getenv("MIN_PROFIT_PERCENT") or "10").strip().lower()
FLOOR_MODE = (os.getenv("FLOOR_MODE") or "skip").strip().lower()
DEFAULT_FEE_PERCENT = float(os.getenv("DEFAULT_FEE_PERCENT") or "18")
EXCLUDE_FROM = (os.getenv("EXCLUDE_PRICE_FROM") or "").strip()
EXCLUDE_TO = (os.getenv("EXCLUDE_PRICE_TO") or "").strip()
PRICE_RANGES_TEXT = (os.getenv("PRICE_RANGES") or "").strip()
SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
SEMI_SHEET_NAME = (os.getenv("SEMI_SHEET_NAME", "") or "").strip()
PLAN_CSV = "store_sale_plan.csv"
COST_COLS = ("Cost Price (£)", "Shipping Cost (£)", "Fee %")


def to_float(v):
    try:
        return float(str(v).replace(",", "").replace("£", "").strip())
    except (TypeError, ValueError):
        return None


def percent_value(text):
    """The sale percentage as a number, 1..90, or None."""
    p = to_float(text)
    return p if p is not None and 1 <= p <= 90 else None


def min_profit_value(text):
    """The margin floor's profit percentage (0..500), or None when the floor is switched off ("off")."""
    if str(text).strip().lower() in ("off", "none", "no"):
        return None
    p = to_float(text)
    if p is None or p < 0 or p > 500:
        raise SystemExit(f"MIN_PROFIT_PERCENT must be a number from 0 to 500 or 'off', not {text!r}")
    return p


def sale_price_for(price, percent, floor=MIN_SALE_PRICE):
    """The sale price for a listing price at this percentage (2 decimals, never below the floor), or None without a usable price."""
    p = to_float(price)
    if p is None or p <= 0:
        return None
    return max(round(p * (1 - percent / 100.0) + 1e-9, 2), floor)


def exclude_range(low, high):
    """(low, high) - the listing prices kept out of the sale, both ends included - or None when neither is given. Two prices with low <= high or nothing."""
    blank = lambda v: v is None or str(v).strip() == ""      # noqa: E731
    if blank(low) and blank(high):
        return None
    lo, hi = to_float(low), to_float(high)
    if lo is None or hi is None or lo < 0 or hi < lo:
        raise SystemExit(f"EXCLUDE_PRICE_FROM / EXCLUDE_PRICE_TO must be two prices with from <= to (or both empty), not {low!r} / {high!r}")
    return lo, hi


def money(n):
    return f"{n:.2f}".rstrip("0").rstrip(".")


def pct_text(p):
    return f"{p:g}"


def label_range(lo, hi):
    if lo is None and hi is None:
        return "every price"
    if lo is None:
        return f"up to GBP {money(hi)}"
    if hi is None:
        return f"GBP {money(lo)} and above"
    return f"GBP {money(lo)} to GBP {money(hi)}"


def describe_exclude(exclude):
    return "none" if not exclude else f"GBP {exclude[0]:g} to GBP {exclude[1]:g} (both included)"


def _range_bound(v):
    if v is None or str(v).strip() == "":
        return None
    n = to_float(v)
    if n is None or n < 0:
        raise SystemExit(f"a price range bound must be a price of 0 or more (or empty), not {v!r}")
    return n


def normalize_ranges(items):
    """[(low, high, percent), ...] from a list of [low, high, percent]: low / high None = open ended, percent 0 or 1..90."""
    out = []
    for item in items or []:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            raise SystemExit(f"a price range is [from, to, percent], not {item!r}")
        lo, hi, pct = _range_bound(item[0]), _range_bound(item[1]), to_float(item[2])
        if pct is None or not (pct == 0 or 1 <= pct <= 90):
            raise SystemExit(f"a range's sale percentage is 0 (kept out) or 1 to 90, not {item[2]!r}")
        if lo is not None and hi is not None and hi < lo:
            raise SystemExit(f"a price range must have from <= to, not {item[0]!r} .. {item[1]!r}")
        out.append((lo, hi, pct))
    return out


_RANGE_TEXT = re.compile(r"^\s*([\d.,£ ]*)\s*-\s*([\d.,£ ]*)\s*:\s*([\d.]+)\s*%?\s*$")


def parse_ranges(text):
    """The PRICE_RANGES text -> [(low, high, percent), ...]: JSON [[from, to, percent], ...] or "from-to:percent; from-to:percent" (empty from / to = open)."""
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            items = json.loads(text)
        except ValueError as exc:
            raise SystemExit(f"PRICE_RANGES is not valid JSON: {exc}")
        return normalize_ranges(items)
    items = []
    for part in re.split(r"[;\n]+", text):
        if not part.strip():
            continue
        m = _RANGE_TEXT.match(part)
        if not m:
            raise SystemExit(f"price range {part.strip()!r} is not 'from-to:percent' (from / to may be empty)")
        items.append([m.group(1).replace(",", "").replace("£", "").strip(), m.group(2).replace(",", "").replace("£", "").strip(), m.group(3)])
    return normalize_ranges(items)


def range_index(price, ranges):
    """The index of the first range that contains the price (both ends included), or None."""
    for i, (lo, hi, _pct) in enumerate(ranges):
        if (lo is None or price >= lo) and (hi is None or price <= hi):
            return i
    return None


def describe_rules(percent, ranges, exclude):
    """The sale in words, for the logs, the state file and the dashboard."""
    if ranges:
        return "; ".join(f"{pct_text(p)}% off {label_range(lo, hi)}" if p else f"no sale on {label_range(lo, hi)}" for lo, hi, p in ranges)
    text = f"{pct_text(percent)}% off every listing" if percent is not None else "no percentage"
    return text + (f" except {label_range(*exclude)}" if exclude else "")


def settings_from_state(state, scope="all"):
    """What a refresh uses: the running sale's own percentage or price ranges, window, margin floor, scope and price exclusion - never today's environment.
    A state written before the price ranges existed (a percentage and an optional excluded range) reads exactly as it did."""
    ranges = normalize_ranges(state["price_ranges"]) if state.get("price_ranges") else []
    return {"percent": None if ranges else percent_value(state.get("percent")), "ranges": ranges,
            "start": state["sale_start_date"], "end": state["sale_end_date"],
            "min_profit": min_profit_value(state.get("min_profit_percent", MIN_PROFIT)),
            "floor_mode": state.get("floor_mode") if state.get("floor_mode") in ("skip", "cap") else "skip",
            "scope": state.get("scope") if state.get("scope") in ("all", "in_stock") else scope,
            "exclude": None if ranges else exclude_range(state.get("exclude_price_from"), state.get("exclude_price_to"))}


def floor_price(cost, shipping, fee_pct, min_profit):
    """The lowest price that still leaves min_profit % of (cost + shipping) after OnBuy's fee - the stores' own price formula at that profit."""
    base = (to_float(cost) or 0.0) + (to_float(shipping) or 0.0)
    if base <= 0:
        return None
    fee = min(max(to_float(fee_pct) if to_float(fee_pct) is not None else DEFAULT_FEE_PERCENT, 0.0), 95.0) / 100.0
    return round(base * (1 + min_profit / 100.0) / (1 - fee) + 1e-9, 2)


def window(end_date, today=None):
    """(sale_start_date, sale_end_date): yesterday 00:00:00 (a start in the past = on at once) to end_date 23:59:59."""
    today = today or date.today()
    end = end_date or (today + timedelta(days=30)).isoformat()
    datetime.strptime(end, "%Y-%m-%d")                    # a wrong date fails here, loudly
    return f"{(today - timedelta(days=1)).isoformat()} 00:00:00", f"{end} 23:59:59"


def carries_this_sale(rec, end):
    """True when the listing carries a sale that ends on this sale's last day - the one this store sale put there."""
    rec = rec or {}
    return listing_sweep.has_sale(rec) and str(rec.get("sale_end_date") or "")[:10] == end[:10]


def sale_item(sku, sale, start, end):
    return {"sku": str(sku).strip(), "boost_marketing_commission": 0, "sale_price": f"{sale:.2f}", "sale_start_date": start, "sale_end_date": end}


def ending_item(sku, sale):
    """The update that ends a sale: the sale price with the past window (the spelling OnBuy accepted on 2026-10-10)."""
    return {"sku": str(sku).strip(), "boost_marketing_commission": 0, "sale_price": f"{max(sale or 0.01, 0.01):.2f}",
            "sale_start_date": sale_guard.PAST_START, "sale_end_date": sale_guard.PAST_END}


def on_this_sale(rec, sale, end):
    """True when the listing already carries exactly this sale (same sale price, same end day)."""
    rec = rec or {}
    cur = to_float(rec.get("sale_price"))
    return cur is not None and abs(cur - sale) < 0.005 and str(rec.get("sale_end_date") or "")[:10] == end[:10]


def costs_from_values(values, into=None):
    """{sku: (cost, shipping, fee %)} from one tab's values (header row first); a blank Fee % cell = DEFAULT_FEE_PERCENT."""
    into = {} if into is None else into
    if not values:
        return into
    h = [str(x).strip() for x in values[0]]
    if "SKU" not in h or COST_COLS[0] not in h:
        return into
    i_s, i_c = h.index("SKU"), h.index(COST_COLS[0])
    i_sh = h.index(COST_COLS[1]) if COST_COLS[1] in h else None
    i_f = h.index(COST_COLS[2]) if COST_COLS[2] in h else None
    for r in values[1:]:
        sku = str(r[i_s]).replace(",", "").strip() if i_s < len(r) else ""
        if not sku or sku in into:
            continue
        cost = to_float(r[i_c]) if i_c < len(r) else None
        ship = (to_float(r[i_sh]) if i_sh is not None and i_sh < len(r) else None) or 0.0
        fee = to_float(r[i_f]) if i_f is not None and i_f < len(r) else None
        if cost is not None and cost > 0:
            into[sku] = (cost, ship, DEFAULT_FEE_PERCENT if fee is None else fee)
    return into


def sheet_costs(client, sheet_name=SHEET_NAME, semi_name=SEMI_SHEET_NAME):
    """Costs from the full sheet's product tabs and, when named, the semi sheet's first tab (the full sheet's row wins)."""
    costs = {}
    book = with_retry(lambda: client.open(sheet_name), what="sheet open", max_attempts=3)
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        amz = book.worksheet(sheet_tabs.AMAZON_TAB)
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    for ws in tabs:
        costs_from_values(with_retry(ws.get_all_values, what=f"read {ws.title}", max_attempts=3), costs)
    n_full = len(costs)
    if semi_name:
        try:
            semi = with_retry(lambda: client.open(semi_name), what="semi sheet open", max_attempts=3)
            costs_from_values(with_retry(semi.sheet1.get_all_values, what="read semi sheet", max_attempts=3), costs)
        except Exception as exc:  # noqa: BLE001 - the semi sheet is optional; without it its listings simply have no cost (= left out)
            print(f"WARNING: semi sheet {semi_name!r} not read ({str(exc)[:120]}) - its listings have no cost this run")
    print(f"costs: {n_full} SKUs from {sheet_name!r}" + (f" + {len(costs) - n_full} from {semi_name!r}" if semi_name else ""))
    return costs


SALE_ACTIONS = ("sale on", "sale changed", "capped at the floor", "already on this sale")


def plan_apply(listings, percent, start, end, scope="all", costs=None, min_profit=None, floor_mode="skip", exclude=None, ranges=None):
    """-> (items to send, rows for the plan CSV, counters). costs=None or min_profit=None = no floor. With `ranges` ([(low, high, percent), ...]) each listing takes the
    percentage of the first range that contains its price (both ends included): percent 0 keeps it out ("skip: price in the excluded range"), a price outside every range
    gets nothing ("skip: outside the selected price ranges"). Without ranges every listing takes `percent`, except one inside `exclude` = (low, high). A listing without
    a numeric price, with a sale price that would not be below its price, out of stock under scope=in_stock, without a cost (floor on), under its floor (skip) or already
    on exactly this sale sends nothing; floor_mode=cap puts a listing under its floor on sale AT the floor when that still gives >= 1% off. A listing that is kept out
    but still carries THIS sale (same last day) is sent the ending update. Rows end with the percentage used and the index of the range ("" = none)."""
    items, rows, c = [], [], Counter()
    floor_on = costs is not None and min_profit is not None
    for rec in listings:
        rec = rec or {}
        sku = str(rec.get("sku") or "").strip()
        if not sku:
            continue
        price = to_float(rec.get("price"))
        usable = price is not None and price > 0
        idx = range_index(price, ranges) if (ranges and usable) else None
        pct = (ranges[idx][2] if idx is not None else None) if ranges else percent
        sale = sale_price_for(price, pct) if (usable and pct) else None
        stock = int(to_float(rec.get("stock")) or 0)
        cost = costs.get(sku) if floor_on else None
        floor = floor_price(cost[0], cost[1], cost[2], min_profit) if cost else None
        action = None
        if not usable:
            action = "skip: no price"
        elif ranges and idx is None:
            action = "skip: outside the selected price ranges"
        elif (ranges and not pct) or (not ranges and exclude and exclude[0] <= price <= exclude[1]):
            action = "skip: price in the excluded range"
        elif sale is None:
            action = "skip: no sale percentage"
        elif sale >= price:
            action = "skip: too cheap for a sale"
        elif scope == "in_stock" and stock <= 0:
            action = "skip: out of stock"
        elif floor_on and not cost:
            action = "skip: no cost on the sheet"
        elif floor is not None and sale < floor:
            if floor_mode == "cap" and floor <= round(price * 0.99, 2):
                sale = floor
                action = "capped at the floor"
            else:
                action = "skip: below the margin floor"
        if action is None or action == "capped at the floor":
            if on_this_sale(rec, sale, end):
                action = "already on this sale"
            else:
                if action is None:
                    action = "sale on" if not listing_sweep.has_sale(rec) else "sale changed"
                items.append(sale_item(sku, sale, start, end))
        elif carries_this_sale(rec, end):
            items.append(ending_item(sku, to_float(rec.get("sale_price"))))
            action += " (sale ended)"
        c[action] += 1
        rows.append([sku, rec.get("price"), stock, rec.get("sale_price"), "" if sale is None or action.startswith("skip") else f"{sale:.2f}",
                     "" if not cost else f"{cost[0] + cost[1]:.2f}", "" if not cost else cost[2], "" if floor is None else f"{floor:.2f}", action,
                     "" if not pct else pct_text(pct), "" if idx is None else str(idx)])
    return items, rows, c


def summarize_ranges(rows, ranges):
    """-> (one dict per range, listings outside every range): how many listings fall in each range and what happens to them, from the plan rows."""
    out = []
    for i, (lo, hi, pct) in enumerate(ranges):
        mine = [r for r in rows if r[10] == str(i)]
        on = [r for r in mine if r[8] in SALE_ACTIONS]
        no_cost = sum(1 for r in mine if r[8].startswith("skip: no cost"))
        below = sum(1 for r in mine if r[8].startswith("skip: below the margin floor"))
        out.append({"index": i + 1, "low": lo, "high": hi, "percent": pct, "listings": len(mine), "on_sale": len(on), "no_cost": no_cost, "below_floor": below,
                    "other": len(mine) - len(on) - no_cost - below, "ended": sum(1 for r in mine if r[8].endswith("(sale ended)")),
                    "discount": round(sum(to_float(r[1]) - to_float(r[4]) for r in on), 2)})
    outside = sum(1 for r in rows if r[8].startswith("skip: outside the selected price ranges"))
    return out, outside


def range_lines(summary, outside):
    """The RANGE lines of a plan (the dashboard reads them): index | range | percent or "kept out" | counts | discount."""
    lines = []
    for s in summary:
        what = f"{pct_text(s['percent'])}%" if s["percent"] else "kept out"
        lines.append(f"RANGE {s['index']} | {label_range(s['low'], s['high'])} | {what} | {s['listings']} listings | {s['on_sale']} on sale | {s['no_cost']} no cost | "
                     f"{s['below_floor']} below the floor | {s['other']} other | GBP {s['discount']:,.2f} off | {s['ended']} sale(s) ended")
    lines.append(f"RANGE other | outside every selected range | {outside} listings")
    return lines


def count_text(counter):
    """name=count; name=count - the runners mask { and } in logs, so no dict is ever printed."""
    return "; ".join(f"{k}={v}" for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))) or "none"



def total_discount(rows):
    """(listings on sale after the plan, the sum of price - sale price over them in GBP): the rows that carry a wanted sale price."""
    on = [r for r in rows if r[4] != "" and to_float(r[1]) is not None]
    return len(on), round(sum(to_float(r[1]) - to_float(r[4]) for r in on), 2)


def plan_cancel(listings):
    """-> (items ending every sale on the account, rows for the plan CSV)."""
    items, rows = [], []
    for rec in listings:
        rec = rec or {}
        sku = str(rec.get("sku") or "").strip()
        if sku and listing_sweep.has_sale(rec):
            items.append(ending_item(sku, to_float(rec.get("sale_price"))))
            rows.append([sku, rec.get("price"), rec.get("stock"), rec.get("sale_price"), "", "", "", "", "sale off"])
    return items, rows


def load_state(path=STATE_FILE):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def state_active(state, today=None):
    today = (today or date.today()).isoformat()
    return bool(state) and str(state.get("sale_end_date") or "")[:10] >= today


def save_state(state, path=STATE_FILE):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)


def clear_state(path=STATE_FILE):
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def write_plan(rows, path=PLAN_CSV):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sku", "price", "stock", "sale_price_now", "sale_price_wanted", "cost_base", "fee_pct", "floor_price", "action", "percent", "range"])
        w.writerows([r + [""] * (11 - len(r)) for r in rows])



def send(onbuy, items, chunk=CHUNK):
    """PUT the items in chunks -> (ok, failed, Counter of error texts)."""
    ok = failed = 0
    errors = Counter()
    for i in range(0, len(items), chunk):
        part = items[i:i + chunk]
        results, err = sale_guard.put_chunk(onbuy, part)
        if results is None:
            failed += len(part)
            errors[str(err)[:80]] += len(part)
            print(f"chunk {i // chunk + 1}: request failed - {err}", flush=True)
            continue
        answered = {str((r or {}).get("sku") or "").strip(): (r or {}) for r in results}
        bad = 0
        for it in part:
            r = answered.get(it["sku"])
            e = str((r or {}).get("error") or "").strip() if r is not None else "no answer"
            if e:
                bad += 1
                errors[e[:80]] += 1
            else:
                ok += 1
        failed += bad
        print(f"chunk {i // chunk + 1}: {len(part) - bad} ok of {len(part)}", flush=True)
        time.sleep(2)
    return ok, failed, errors


def read_back(onbuy, sku):
    rec = onbuy.get_listing(sku) or {}
    return {k: rec.get(k) for k in ("price", "stock", "sale_price", "sale_start_date", "sale_end_date", "updated_at")}


def wait_until(onbuy, sku, test, waits, label):
    """Read the listing back after each wait until test(rec) holds; prints the timeline, returns the seconds it took or None."""
    total = 0
    for w in waits:
        time.sleep(w)
        total += w
        rec = onbuy.get_listing(sku) or {}
        state = {k: rec.get(k) for k in ("price", "sale_price", "sale_start_date", "sale_end_date", "updated_at")}
        print(f"  {label}: read-back after {total}s: {json.dumps(state, default=str)}", flush=True)
        if test(rec):
            return total
    return None


def google_client():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds)


def main():
    percent = percent_value(PERCENT)
    min_profit = min_profit_value(MIN_PROFIT)
    floor_mode = FLOOR_MODE if FLOOR_MODE in ("skip", "cap") else "skip"
    state = load_state()
    scope = SCOPE
    exclude = exclude_range(EXCLUDE_FROM, EXCLUDE_TO)
    ranges = parse_ranges(PRICE_RANGES_TEXT)
    if ranges:
        percent, exclude = None, None                     # the ranges carry every percentage; PERCENT and the single excluded range are ignored
        if not any(r[2] > 0 for r in ranges):
            raise SystemExit("PRICE_RANGES has no range with a sale percentage above 0 - nothing to put on sale")
    if MODE in ("apply", "test") and percent is None and not ranges:
        raise SystemExit("give PERCENT (1 to 90) for every listing, or PRICE_RANGES with a percentage for each price range")
    if MODE == "refresh":
        if not state_active(state):
            print("no active store sale (store_sale.json absent or expired) - nothing to refresh")
            return
        cfg = settings_from_state(state, scope)
        percent, ranges, start, end, min_profit = cfg["percent"], cfg["ranges"], cfg["start"], cfg["end"], cfg["min_profit"]
        floor_mode, scope, exclude = cfg["floor_mode"], cfg["scope"], cfg["exclude"]
    else:
        start, end = window(SALE_END)
    rules = describe_rules(percent, ranges, exclude) if (ranges or percent is not None) else "-"
    print(f"MODE {MODE} | sale {rules} | window {start} .. {end} | scope {scope} | margin floor "
          f"{'OFF' if min_profit is None else f'{min_profit}% profit on cost + shipping after the fee, {floor_mode}'} | state: "
          + (f"ACTIVE {state.get('summary') or str(state.get('percent')) + '%'} until {str(state.get('sale_end_date'))[:10]}" if state_active(state)
             else ("expired" if state else "none")))
    costs = None
    if min_profit is not None and MODE in ("plan", "test", "apply", "refresh") and (percent is not None or ranges):
        if not os.getenv("GOOGLE_CREDENTIALS"):
            raise SystemExit("the margin floor needs the sheet (GOOGLE_CREDENTIALS) - set MIN_PROFIT_PERCENT=off to run without it (not advised)")
        costs = sheet_costs(google_client())
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")

    if MODE == "test":
        if not TEST_SKU:
            raise SystemExit("test needs TEST_SKU")
        before = onbuy.get_listing(TEST_SKU)
        if not before:
            raise SystemExit(f"{TEST_SKU}: no listing")
        items, rows, c = plan_apply([before], percent, start, end, "all", costs, min_profit, floor_mode, exclude, ranges)
        row = rows[0]
        print(f"{TEST_SKU}: before {json.dumps(read_back(onbuy, TEST_SKU), default=str)} | cost base {row[5] or '-'} fee {row[6] or '-'}% floor {row[7] or '-'} "
              f"-> {row[8]}" + (f" at {row[4]}" if row[4] else ""))
        if not items or not any(it["sale_end_date"] == end for it in items):
            print("RESULT: nothing sent - this listing would not be put on sale (see the reason above)")
            return
        sale = to_float(items[0]["sale_price"])
        t0 = time.time()
        ok, failed, errors = send(onbuy, items)
        print(f"SALE ON sent: {ok} ok, {failed} failed | errors {count_text(errors)} ({time.time() - t0:.0f}s)")
        shown = wait_until(onbuy, TEST_SKU, lambda r: on_this_sale(r, sale, end), (15, 15, 30, 60, 60, 120), "sale on")
        print(f"RESULT sale on: {'visible after ' + str(shown) + 's' if shown else 'NOT visible within 5 min'}")
        t1 = time.time()
        ok, failed, errors = send(onbuy, [ending_item(TEST_SKU, sale)])
        print(f"SALE OFF sent: {ok} ok, {failed} failed | errors {count_text(errors)} ({time.time() - t1:.0f}s)")
        gone = wait_until(onbuy, TEST_SKU, lambda r: not listing_sweep.has_sale(r), (30, 30, 60, 60, 120, 120), "sale off")
        print(f"RESULT sale off: {'gone after ' + str(gone) + 's' if gone else 'STILL THERE after 7 min'}")
        print(f"{TEST_SKU}: after {json.dumps(read_back(onbuy, TEST_SKU), default=str)}")
        return

    raw = listings_cache.load() if os.getenv("LISTINGS_CACHE") else None
    if raw is None:
        listings, short, meta, pages = listing_sweep.sweep(onbuy)
        print(f"sweep: {len(listings)} listings in {pages} page(s), short pages {len(short)}, metadata {(meta or {}).get('metadata')}")
    else:
        listings = raw
        print(f"shared sweep: {len(listings)} listings")
    on_sale = [r for r in listings if listing_sweep.has_sale(r)]
    ends = "; ".join(f"{d}={n}" for d, n in Counter(str(r.get("sale_end_date"))[:10] for r in on_sale).most_common(4)) or "-"
    print(f"sales on the account now: {len(on_sale)} of {len(listings)} listings | end dates {ends}")

    if MODE == "cancel":
        items, rows = plan_cancel(listings)
        write_plan(rows)
        print(f"TO END: {len(items)} sale(s)")
        if MAX_CHANGES and len(items) > MAX_CHANGES:
            raise SystemExit(f"{len(items)} exceed MAX_CHANGES={MAX_CHANGES} - refusing")
        ok, failed, errors = send(onbuy, items) if items else (0, 0, Counter())
        removed = clear_state()
        print(f"CANCELLED: {ok} ok, {failed} failed | errors {count_text(Counter(dict(errors.most_common(5))))} | store_sale.json {'removed' if removed else 'was not there'}")
        if items:
            sample = [it["sku"] for it in items[:SAMPLE]]
            time.sleep(90)
            gone = sum(1 for s in sample if not listing_sweep.has_sale(onbuy.get_listing(s) or {}))
            print(f"read-back after 90s: {gone} of {len(sample)} already show no sale (OnBuy drops an ended sale within a minute or two)")
        return

    if MODE in ("plan", "apply", "refresh"):
        if percent is None and not ranges:
            print("no PERCENT or PRICE_RANGES given - census only")
            write_plan([[r.get("sku"), r.get("price"), r.get("stock"), r.get("sale_price"), "", "", "", "", "on sale now"] for r in on_sale])
            print("PLAN ONLY - nothing changed.")
            return
        items, rows, c = plan_apply(listings, percent, start, end, scope, costs, min_profit, floor_mode, exclude, ranges)
        write_plan(rows)
        n_sale, discount = total_discount(rows)
        print(f"PLAN {rules} | {len(items)} update(s) in {-(-len(items) // CHUNK)} request(s)")
        print(f"ACTIONS {count_text(c)}")
        if ranges:
            for line in range_lines(*summarize_ranges(rows, ranges)):
                print(line)
        print(f"after this run {n_sale} of {len(listings)} listings carry the sale, GBP {discount:,.2f} off in total (one unit of each)")
        if MODE == "plan":
            print("PLAN ONLY - nothing changed.")
            return
        if MAX_CHANGES and len(items) > MAX_CHANGES:
            raise SystemExit(f"{len(items)} updates exceed MAX_CHANGES={MAX_CHANGES} - refusing")
        if MODE == "apply":
            save_state({"percent": percent if percent is not None else max(r[2] for r in ranges), "summary": rules, "scope": scope,
                        "price_ranges": [list(r) for r in ranges] or None, "sale_start_date": start, "sale_end_date": end,
                        "exclude_price_from": exclude[0] if exclude else None, "exclude_price_to": exclude[1] if exclude else None,
                        "min_profit_percent": "off" if min_profit is None else min_profit, "floor_mode": floor_mode,
                        "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"), "listings": n_sale, "updates": len(items)})
            print(f"store_sale.json written ({rules} until {end[:10]}, floor {'off' if min_profit is None else str(min_profit) + '% ' + floor_mode})")
        if not items:
            print("nothing to send - every listing already carries this sale (or is kept out by the rules)")
            return
        ok, failed, errors = send(onbuy, items)
        print(f"APPLIED: {ok} ok, {failed} failed | errors {count_text(Counter(dict(errors.most_common(5))))}")
        wanted = {it["sku"]: to_float(it["sale_price"]) for it in [i for i in items if i["sale_end_date"] == end][:SAMPLE]}
        time.sleep(60)
        shown = sum(1 for s, v in wanted.items() if on_this_sale(onbuy.get_listing(s) or {}, v, end))
        print(f"read-back after 60s: {shown} of {len(wanted)} already show the sale (OnBuy applies updates within about a minute)")
        return
    raise SystemExit(f"unknown MODE {MODE!r}")



if __name__ == "__main__":
    main()
