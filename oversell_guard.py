"""Oversell guard (2026-10-02): OnBuy stock must be 0 wherever the sheet says 0.

Why: a wrong order landed on a product the sheet had marked out of stock for a
day (SKU 175117835194, order T6PYXRQ). The sync zeroes a row once, stamps it
"pushed", and never looks again until the row is re-checked; the only second
look was the nightly audit (186 drifted listings on 2026-10-01). Whatever put
the stock back - a bounced push that was stamped as done, a dashboard edit,
OnBuy itself - the listing stayed sellable for up to a day.

What: every run of the sync and of the hourly backfill ends/starts with this
pass - read the sheet's Stock (+ Status, Sync Status, created flag, price)
for both product tabs, and re-send "stock 0" for every row that says 0. The
push is idempotent: a listing already at 0 stays at 0. No sweep is needed to
find the offenders, so the pass costs NO GET calls.

API budget (the point of this design, see tests):
  OnBuy   GET  0                      (a listings sweep is 64-117 of the 240/hour GET quota)
          PUT  ceil(rows / 500)       (1-3 of the 240/hour PUT quota; the sync itself uses ~195)
          auth 0                      (reuses the host run's client and token)
  Sheets  2 header reads + 2 column reads (only six columns, a few KB)
  Queue   0 new workflow runs         (runs inside the onbuy-api runs that already exist)

Safety: dry-run mode; only rows that say Stock 0 AND Status INACTIVE (when the
Status column is filled) with a usable price; a SKU on more than one row is
left alone; a mass breaker refuses when "out of stock" suddenly looks like a
third of the catalogue (a mis-read sheet, not a market); a rate-limited or
failed push is dropped quietly - the next run asks again; never raises.
"""
import os
import time

import gspread

import sheet_tabs

NEEDED = ("SKU", "Stock", "Status", "Sync Status", "OnBuy Product Created", "Selling Price (£)")
ELIGIBLE_PREFIXES = ("Synced", "Pending Approval", "Awaiting OnBuy go-live")
FROZEN_PREFIXES = ("Failed", "BRAND BLOCKED")
CHUNK = 500                      # SKUs per PUT request (OnBuy accepts up to 1,000)
MIN_PRICE = float(os.getenv("GUARD_MIN_PRICE") or "1.00")   # below OnBuy's minimum a push can suspend the listing
# A broken sheet read looks like "nearly everything is out of stock" - and fails the Stock/Status
# agreement check first. OpenMaal legitimately has 54% of its catalogue at stock 0 (2026-10-02), so
# the breaker only trips for a huge count that is also nearly the whole catalogue.
BREAKER_MIN = int(os.getenv("GUARD_BREAKER_MIN") or "1000")
BREAKER_FRACTION = float(os.getenv("GUARD_BREAKER_FRACTION") or "0.80")
MAX_SECONDS = float(os.getenv("GUARD_MAX_SECONDS") or "90")


def _say(msg):
    print(f"oversell guard: {msg}", flush=True)


def to_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def to_float(v):
    try:
        return float(str(v).replace(",", "").replace("£", "").strip())
    except (TypeError, ValueError):
        return 0.0


def col_letter(n):
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def candidates(rows, min_price=MIN_PRICE):
    """rows: dicts with tab, row, sku, stock (int|None), status, sync, created (bool), price (float).
    -> ([(sku, price)], stats). A row qualifies when the sheet says Stock 0 for a listing that exists."""
    seen = {}
    for r in rows:
        if r["sku"]:
            seen[r["sku"]] = seen.get(r["sku"], 0) + 1
    stats = {"rows": len(rows), "zero": 0, "eligible": 0, "other_status": 0, "status_disagrees": 0,
             "no_price": 0, "below_min_price": 0, "repeated_sku": 0}
    out = []
    for r in rows:
        if not r["sku"] or r["stock"] != 0:
            continue
        stats["zero"] += 1
        sync = str(r.get("sync") or "").strip()
        frozen_live = sync.startswith(FROZEN_PREFIXES) and r["created"]
        if not (sync.startswith(ELIGIBLE_PREFIXES) or frozen_live):
            stats["other_status"] += 1
            continue
        status = str(r.get("status") or "").strip().upper()
        if status and status != "INACTIVE":
            stats["status_disagrees"] += 1          # Stock says 0 but Status says active: do not trust either
            continue
        if seen[r["sku"]] > 1:
            stats["repeated_sku"] += 1
            continue
        price = r.get("price") or 0.0
        if price <= 0:
            stats["no_price"] += 1                  # the sync's OOS pass (listing price) handles these
            continue
        if price < min_price:
            stats["below_min_price"] += 1
            continue
        stats["eligible"] += 1
        out.append((r["sku"], round(price, 2)))
    return out, stats


def tripped(n_candidates, n_created, min_count=None, fraction=None):
    """True when 'out of stock' looks like a broken read, not a market."""
    min_count = BREAKER_MIN if min_count is None else min_count
    fraction = BREAKER_FRACTION if fraction is None else fraction
    return n_candidates >= min_count and n_created > 0 and n_candidates >= fraction * n_created


def read_rows(book):
    """The six columns the guard needs from both product tabs - two small
    batch reads per tab, never the descriptions/images."""
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        amz = book.worksheet(sheet_tabs.AMAZON_TAB)
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    rows = []
    for ws in tabs:
        header = [str(h).strip() for h in ws.row_values(1)]
        idx = {h: i for i, h in enumerate(header) if h}
        if "SKU" not in idx or "Stock" not in idx:
            raise RuntimeError(f"tab {ws.title!r} has no SKU/Stock column - guard skipped")
        present = [c for c in NEEDED if c in idx]
        ranges = [f"{col_letter(idx[c] + 1)}2:{col_letter(idx[c] + 1)}" for c in present]
        cols = dict(zip(present, ws.batch_get(ranges)))
        n = max((len(v) for v in cols.values()), default=0)

        def cell(c, i):
            v = cols.get(c)
            if v is None or i >= len(v) or not v[i]:
                return ""
            return str(v[i][0]).strip()
        for i in range(n):
            sku = cell("SKU", i).replace(",", "").strip()
            if not sku:
                continue
            rows.append({"tab": ws.title, "row": i + 2, "sku": sku, "stock": to_int(cell("Stock", i)),
                         "status": cell("Status", i), "sync": cell("Sync Status", i),
                         "created": cell("OnBuy Product Created", i).upper() == "TRUE",
                         "price": to_float(cell("Selling Price (£)", i))})
    return rows


def run(book, onbuy, dry_run=None):
    """One pass. Returns {"requests", "pushed", "bounced", ...}; never raises."""
    stats = {"requests": 0, "pushed": 0, "bounced": 0, "candidates": 0, "skipped": ""}
    try:
        if (os.getenv("OVERSELL_GUARD") or "1").strip().lower() in ("0", "no", "false", "off"):
            stats["skipped"] = "disabled (OVERSELL_GUARD)"
            return stats
        if (os.getenv("ONBUY_API_PUSH_ENABLED") or "").strip().lower() != "true":
            stats["skipped"] = "OnBuy pushes are not enabled for this store"
            return stats
        dry = (os.getenv("GUARD_DRY_RUN") or "0").strip().lower() in ("1", "yes", "true") if dry_run is None else dry_run
        t0 = time.time()
        rows = read_rows(book)
        pushes, cs = candidates(rows)
        created = sum(1 for r in rows if r["created"])
        stats["candidates"] = len(pushes)
        _say(f"{cs['rows']} sheet rows | {cs['zero']} say stock 0 | {len(pushes)} live and priced "
             f"(skipped: {cs['other_status']} no live status, {cs['no_price']} no price, {cs['below_min_price']} "
             f"under the minimum price, {cs['status_disagrees']} Stock/Status disagree, {cs['repeated_sku']} repeated SKU)"
             f"{' - DRY RUN' if dry else ''}")
        if tripped(len(pushes), created):
            msg = (f"{len(pushes)} of {created} created listings look out of stock on the sheet - far more than a "
                   "real stock-out; looks like a mis-read sheet. Nothing was pushed.")
            _say("REFUSING - " + msg)
            try:
                import notify
                notify.send_alert_email("Oversell guard refused to act", msg, routine=False)
            except Exception as exc:  # noqa: BLE001
                _say(f"alert email failed ({exc})")
            stats["skipped"] = "breaker"
            return stats
        if dry or not pushes:
            return stats
        errors = {}
        for c in range(0, len(pushes), CHUNK):
            if time.time() - t0 > MAX_SECONDS:
                _say("time budget used - the rest waits for the next pass")
                break
            chunk = pushes[c:c + CHUNK]
            try:
                results = onbuy.update_listings_by_sku_batch([(s, p, 0) for s, p in chunk])
            except Exception as exc:  # noqa: BLE001 - rate limit, 5xx, network: drop quietly, next pass asks again
                _say(f"push postponed ({type(exc).__name__}: {str(exc)[:120]})")
                stats["requests"] += 1
                break
            stats["requests"] += 1
            answered = {}
            for it in results or []:
                it = it or {}
                s = str(it.get("sku") or "").strip()
                if s:
                    answered[s] = str(it.get("error") or "").strip()
            for s, _p in chunk:
                err = answered.get(s, "no answer")
                if err:
                    stats["bounced"] += 1
                    errors.setdefault(err[:60], []).append(s)
                else:
                    stats["pushed"] += 1
        _say(f"re-zeroed {stats['pushed']} listing(s) in {stats['requests']} PUT request(s) (0 GET, 0 auth); "
             f"{stats['bounced']} bounced")
        for err, skus in errors.items():
            # "SKU does not exist" = the sheet says live but OnBuy has no such listing: nothing to oversell.
            _say(f"  bounced '{err}': {len(skus)} e.g. {', '.join(skus[:6])}")
    except Exception as exc:  # noqa: BLE001 - the guard must never cost its host run anything
        _say(f"skipped this pass ({type(exc).__name__}: {str(exc)[:160]})")
        stats["skipped"] = "error"
    return stats


# ----------------------------------------------------------------- manual run
SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"


def main():
    """Manual entry (oversell_guard.yml): one pass on its own client. GUARD_DRY_RUN=1 only reports.
    GUARD_PROBE=1 also makes ONE read-only GET and prints any rate-limit headers OnBuy sends."""
    import json
    import re

    from oauth2client.service_account import ServiceAccountCredentials

    from onbuy_client import BASE_URL, OnBuyClient

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(SHEET_NAME)
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    stats = run(book, onbuy)
    print("result:", stats)
    if (os.getenv("GUARD_PROBE") or "").strip().lower() in ("1", "yes", "true"):
        resp = onbuy._send("GET", f"{BASE_URL}/listings", what="header probe",
                           params={"site_id": onbuy.site_id, "limit": 1}, timeout=60)
        hdrs = {k: v for k, v in resp.headers.items() if re.search(r"rate|limit|remaining|retry|quota", k, re.I)}
        print(f"header probe: HTTP {resp.status_code}; rate-limit headers: {hdrs or 'none sent'}")


if __name__ == "__main__":
    main()
