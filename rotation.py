"""Re-check order for the sync batch: age x risk weight (2026-10-01).

Why this replaced the "low-stock lane" (2026-09-22): the lane gave 70% of
every batch to rows with 1-5 units left, ordered by stock level first. At
YRA that meant the 585 rows with 1-4 units were re-checked on EVERY run
(~half the daily eBay budget) while the 3,922 rows sitting at stock = 5 -
the pipeline's default when eBay shows no exact count - fell out of the
lane's tail and shared the leftover slots: median age 55h, worst 125h, and
a wrong order landed on a product that went out of stock days earlier.

A row is now picked when age_hours x weight is highest, so a row's re-check
FREQUENCY is proportional to its weight and nothing starves: a stale
default-stock row eventually outranks a freshly checked 1-unit row. The
weights encode how dangerous staleness is: few units left = riskier, a row
already out of stock cannot oversell, and a row waiting on a human that is
not live on OnBuy cannot sell at all. A LIVE flagged row keeps a normal
weight - it is on sale, and a frozen duplicate can no longer be zeroed by
anything else. Rows older than MAX_AGE_HOURS jump the queue (a hard cap on
staleness whatever the weights say), and a never-checked row outranks all.

Pure functions (no Sheet/OnBuy access) so the ordering is unit-tested.
"""
import os

# Relative re-check frequency by the sheet's Stock cell.
WEIGHT_BY_STOCK = {1: 2.0, 2: 1.8, 3: 1.6, 4: 1.4, 5: 1.0}
WEIGHT_STOCK_6_TO_10 = 0.85
WEIGHT_STOCK_OVER_10 = 0.7
WEIGHT_OUT_OF_STOCK = 0.3      # cannot oversell; still re-checked to catch a restock
WEIGHT_NEEDS_HUMAN = 0.25      # flagged and not live: only a person can move it on
MAX_AGE_HOURS = float(os.getenv("ROTATION_MAX_AGE_HOURS") or "72")

_HUMAN_PREFIXES = ("Failed", "BRAND BLOCKED", "Skipped")
_GUARANTEE_BONUS = 1e9  # lifts a row past every weighted score; oldest first among them


def sheet_stock(row):
    """The Stock cell as an int, -1 when blank/unreadable (never fetched)."""
    try:
        return int(float(str(row.get("Stock") if row.get("Stock") is not None else "").replace(",", "").strip()))
    except (TypeError, ValueError):
        return -1


def is_live(row):
    return str(row.get("OnBuy Product Created") or "").strip().upper() == "TRUE"


def needs_human(row):
    return str(row.get("Sync Status") or "").strip().startswith(_HUMAN_PREFIXES)


def row_weight(row):
    if needs_human(row) and not is_live(row):
        return WEIGHT_NEEDS_HUMAN
    stock = sheet_stock(row)
    if stock < 0:
        return 1.0                       # never fetched: its (huge) age decides
    if stock == 0:
        return WEIGHT_OUT_OF_STOCK
    if stock <= 5:
        return WEIGHT_BY_STOCK[stock]
    return WEIGHT_STOCK_6_TO_10 if stock <= 10 else WEIGHT_STOCK_OVER_10


def rotation_order(processable, now, parse_time, max_age_hours=None):
    """[(idx, row), ...] -> the same tuples, most urgent first.

    `now` and every "Last Checked Time" are naive Pakistan wall clock;
    `parse_time` maps a blank/invalid cell to a date far in the past, which
    makes a never-checked row the oldest of all."""
    cap = MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    scored = []
    for pos, item in enumerate(processable):
        row = item[1]
        age = max(0.0, (now - parse_time(row.get("Last Checked Time", ""))).total_seconds() / 3600.0)
        score = age * row_weight(row)
        if cap and age > cap and sheet_stock(row) > 0 and not (needs_human(row) and not is_live(row)):
            score += _GUARANTEE_BONUS + age
        scored.append((score, pos, item))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in scored]


def split_no_sku(processable):
    """([(idx, row)] that have a SKU, [(idx, row)] that do not).

    The sync loop skips a row with no SKU only AFTER fetching it from the
    supplier and never stamps its Last Checked Time, so such a row kept the
    oldest slot of every batch and cost an eBay call per run (Arden
    2026-10-01: 182 rows = 23% of each batch). Under an age-based order it
    would rank first for ever. The SKU is entered by hand, so these rows wait
    outside the batch until one is."""
    kept, dropped = [], []
    for item in processable:
        (kept if str(item[1].get("SKU") or "").strip() else dropped).append(item)
    return kept, dropped


def describe(processable):
    """Counts for the run log: (live in stock, out of stock, waiting on a human)."""
    live_in_stock = sum(1 for _i, r in processable if is_live(r) and sheet_stock(r) > 0)
    oos = sum(1 for _i, r in processable if sheet_stock(r) == 0)
    human = sum(1 for _i, r in processable if needs_human(r) and not is_live(r))
    return live_in_stock, oos, human
