"""The dynamic Amazon-sync schedule (2026-11-02, user: "make it dynamic - the hourly Amazon runs of each account depend on the number
of products added: more runs when the products are few, divide accordingly when they are many").

What it is built on (all measured in our logs): Keepa answers at 20 tokens a minute per key (28,800 a day), one refresh costs about
1.25 tokens (1.17 for the product call plus the real-price lookups), a key banks 60 minutes of refill (1,200 tokens), and a sync takes
the STALEST rows first (rotation.py) - so "refresh S products per run" walks the tab oldest-first and S is the only knob a run needs.

The rule, per account:
  share    = the key's tokens a day x 85% (headroom for lookups and bursts) / the accounts sharing that key
  capacity = share / 1.25                       products it can refresh a day
  cycles   = min(12, capacity / N)              passes over the whole tab a day (12 = every two hours is the fastest worth having)
  daily    = cycles x N                         products refreshed a day
  runs     = the smallest of 1, 2, 3, 4, 6, 12 that carries `daily` at no more than 2,275 products a run (the run-size cap)
  slice    = daily / runs                       products per run
A small tab therefore runs often (up to every two hours, each run the whole tab); a big tab is cut into slices; and when the share cannot
cover even one pass a day the plan says so instead of pretending.

`decide` is the other half: runs are launched by a state test (time since the last planned run against the interval), not by a fixed
clock, because GitHub delivers only about 4 of 12 scheduled ticks a day - a dropped tick is simply caught up by the next one."""
import math
from datetime import datetime, timezone

TOKENS_PER_PRODUCT = 1.25
UTILIZATION = 0.85
CYCLES_MAX = 12.0
SLICE_MAX = 2275
ALLOWED_RUNS = (1, 2, 3, 4, 6, 12)
LEGACY_RUNS = 6                      # the cadence used when the plan cannot be computed (or AMAZON_DYNAMIC=0): every 4 hours
TOLERANCE_H = 0.4                    # a tick up to 24 minutes before the interval ends still counts as due
GHOST_H = 12.0                       # a run "in progress" or waiting longer than this is a GitHub ghost, not a sync
FAIL_RETRY_H = 1.5                   # a FAILED last sync is retried after this long at the latest (its rows were left untouched)
OVERDUE_FACTOR = 2.0                 # a store that waited this many intervals (+2 h) for a busy key-mate goes anyway
PLANNED_MARK = "· plan"              # the dispatcher's runs carry this in their title (run-name in run_amazon.yml)
WAITING = ("in_progress", "queued", "pending", "waiting", "requested")


def plan(n, tokens_per_day=28800, stores_on_key=2, cycles_max=CYCLES_MAX, slice_max=SLICE_MAX,
         tokens_per_product=TOKENS_PER_PRODUCT, utilization=UTILIZATION, allowed=ALLOWED_RUNS):
    """The schedule for one account: {n, share, capacity, cycles, daily, runs, interval_h, slice, tokens_day, limited_by, warnings}."""
    n = max(0, int(n or 0))
    tokens_per_day = float(tokens_per_day or 28800)
    stores_on_key = max(1, int(stores_on_key or 1))
    share = tokens_per_day * utilization / stores_on_key
    capacity = share / tokens_per_product
    out = {"n": n, "key_tokens_day": round(tokens_per_day), "stores_on_key": stores_on_key, "share": round(share),
           "capacity": int(capacity), "warnings": []}
    if n == 0:
        out.update(cycles=0.0, daily=0, runs=0, interval_h=None, slice=0, tokens_day=0, limited_by="no products")
        return out

    affordable = capacity / n
    cycles = min(float(cycles_max), affordable)
    limited_by = "cadence" if affordable >= cycles_max else "budget"
    target = cycles * n
    per_run_cap = max(1, min(n, int(slice_max)))
    need = max(1, math.ceil(target / per_run_cap - 1e-9))
    runs = next((k for k in sorted(allowed) if k >= need), max(allowed))
    size = math.ceil(target / runs - 1e-9)
    if size > per_run_cap:                                   # even the busiest allowed cadence cannot carry the target at this run size
        size = per_run_cap
        limited_by = "run size"
        out["warnings"].append(f"the run-size cap ({per_run_cap:,} products) limits it to {runs * size:,} a day")
    size = max(1, min(size, n))
    daily = runs * size
    cycles = daily / n
    out.update(cycles=round(cycles, 2), daily=daily, runs=runs, interval_h=round(24.0 / runs, 2), slice=size,
               tokens_day=round(daily * tokens_per_product), limited_by=limited_by)
    if cycles < 1.0:
        out["warnings"].append(f"each product is refreshed only every {24.0 / cycles:.0f} hours - the Keepa share is too small for {n:,} products")
    return out


def _ts(text):
    return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def is_busy(runs, now):
    """True when an Amazon sync is already running or waiting (a run older than GHOST_H is a GitHub ghost and does not count)."""
    for r in runs:
        if r.get("status") in WAITING:
            try:
                if (now - _ts(r["created_at"])) / 3600.0 <= GHOST_H:
                    return True
            except (KeyError, ValueError):
                return True
    return False


def last_planned(runs):
    """{'ts', 'conclusion'} of the newest run that counts as 'the last sync' (None when there is none): a scheduled run or one the dispatcher
    launched - a human's targeted run must not move the clock, and a cancelled/skipped run did no work."""
    best = None
    for r in runs:
        if r.get("conclusion") in ("cancelled", "skipped"):
            continue
        planned = r.get("event") == "schedule" or PLANNED_MARK in str(r.get("display_title") or "")
        if planned and r.get("created_at"):
            ts = _ts(r["created_at"])
            if best is None or ts > best["ts"]:
                best = {"ts": ts, "conclusion": r.get("conclusion") or r.get("status") or ""}
    return best


def catch_up_slice(info, elapsed_h, slice_max=SLICE_MAX):
    """Products for THIS run: the plan's slice, grown to cover the real time since the last sync when GitHub dropped ticks (the same idea as the
    eBay sync's adaptive batch) - the day's budget is spent whatever the cadence, never more than one run may carry (slice_max) or the tab holds."""
    size = int(info.get("slice") or 0)
    interval = info.get("interval_h") or 0
    if not size or elapsed_h is None or elapsed_h <= interval:
        return size
    wanted = math.ceil(info.get("daily", 0) * min(elapsed_h, 24.0) / 24.0)
    return max(size, min(wanted, int(slice_max), int(info.get("n") or size)))


def mate_running(mates, fetch, now):
    """The first key-mate (owner/repo) whose Amazon sync is running or waiting - two accounts on one Keepa key must not draw at once (the second
    run finds the bucket empty, waits for refills the first keeps taking, and can run out of retries). An unreadable mate never blocks."""
    for mate in mates:
        try:
            runs = fetch(mate)
        except Exception:                                           # noqa: BLE001 - a mate we cannot see is not a reason to stand still
            continue
        if is_busy(runs, now):
            return mate
    return None


def overdue(now, last, interval_h):
    """True when this account has gone so long without a sync that waiting for a busy key-mate any longer would be the bigger harm."""
    return last is not None and (now - last["ts"]) / 3600.0 > OVERDUE_FACTOR * interval_h + 2.0


def decide(now, last, busy, interval_h, force=False, tolerance_h=TOLERANCE_H):
    """(due?, reason) - reasons are one-line plain text (no '|'). `last` = last_planned(...): a sync that FAILED is retried within FAIL_RETRY_H."""
    if busy:
        return False, "a sync is already running or waiting for the OnBuy slot"
    if force:
        return True, "forced by hand"
    if last is None:
        return True, "no earlier planned sync on record"
    elapsed = (now - last["ts"]) / 3600.0
    interval = min(interval_h, FAIL_RETRY_H) if last.get("conclusion") == "failure" else interval_h
    note = " (the last one failed - retrying sooner)" if interval != interval_h else ""
    if elapsed >= interval - tolerance_h:
        return True, f"{elapsed:.1f} h since the last sync, the plan wants one every {interval:g} h{note}"
    return False, f"{elapsed:.1f} h since the last sync, the next is due after {interval:g} h{note}"


def line(info, due, reason, dispatched, elapsed_h=None, extra="", slice_now=None, waited_min=None, mate=None):
    """The single structured log line the dashboard reads: AMAZON_PLAN|key=value|..."""
    parts = [("n", info.get("n")), ("runs", info.get("runs")), ("slice", info.get("slice")), ("cycles", info.get("cycles")),
             ("daily", info.get("daily")), ("tokens_day", info.get("tokens_day")), ("share", info.get("share")),
             ("key_tokens_day", info.get("key_tokens_day")), ("stores_on_key", info.get("stores_on_key")),
             ("interval_h", info.get("interval_h")), ("limited_by", info.get("limited_by")),
             ("elapsed_h", None if elapsed_h is None else round(elapsed_h, 1)),
             ("slice_now", slice_now if slice_now and slice_now != info.get("slice") else None),
             ("waited_min", waited_min or None), ("mate", mate),
             ("due", "yes" if due else "no"), ("dispatched", "yes" if dispatched else "no"),
             ("reason", str(reason).replace("|", "/")), ("note", extra.replace("|", "/") if extra else None)]
    return "AMAZON_PLAN|" + "|".join(f"{k}={v}" for k, v in parts if v is not None)
