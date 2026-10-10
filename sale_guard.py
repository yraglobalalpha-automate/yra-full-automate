"""Sale-price guard (2026-10-10): no OnBuy listing of ours may carry a SALE price.

Why: the user saw "On Sale: GBP 65.11" next to the price 67.99 on listing 0000089208441-zzz-40 and does not want any sale shown - the selling price is
the direct price. A census the same day found sale prices on most of Makstore's and Arden's listings (window 2026-10-05 .. 2026-10-10, mostly 4-5%
under the price, some 20%) that OUR code never sent: the sync only sends price / stock / boost_marketing_commission. They come from somewhere on
OnBuy's side (a campaign, a bulk edit); whatever puts them there can put them back, so this is a guard, not a one-off.

How OnBuy lets a sale go (found by experiment 2026-10-10, Makstore sale_clear_experiment runs): there is NO "empty" spelling - sale_price null / dates
null are accepted and ignored, 0 and "" are rejected ("Sale price must be numeric and greater than zero"). What works is to END the sale: send the
listing's own sale price with a window that lies in the past. OnBuy applies it within ~30 s and drops the expired sale (sale_price, start and end all
null) about a minute later. Read-backs seconds after the update still show the old values - the listing store is eventually consistent - so judge
a clear by a read a few minutes later. The update carries NO price and NO stock, so it cannot race with the sync.

What it does: sweep the account's listings (or reuse the nightly's shared sweep, listings_cache), pick every record with a sale price, send the
past-window update in chunks of 500 (PUT /v2/listings/by-sku). API budget: GET = one sweep (64-150 of the 240/hour quota, none when the nightly's cache
is reused), PUT = ceil(listings with a sale / 500) (the sync itself uses ~195 of 240/hour). Dry run by default.

Env: DRY_RUN (default 1), MAX_FIX (listings to end in this run, 0 = all), CHUNK (default 500), VERIFY_SAMPLE (default 5: after a live run, read that many
ended listings back), VERIFY_WAIT (seconds before the read-back, default 150), LISTINGS_CACHE (shared sweep file), SALE_GUARD_ALERT (default 1: one
alert mail when sales were found).
"""
import json
import os
import time
from collections import Counter

import listing_sweep
import listings_cache
from onbuy_client import BASE_URL, OnBuyClient

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
MAX_FIX = int(os.getenv("MAX_FIX") or "0")
CHUNK = int(os.getenv("CHUNK") or "500")
VERIFY_SAMPLE = int(os.getenv("VERIFY_SAMPLE") or "5")
VERIFY_WAIT = int(os.getenv("VERIFY_WAIT") or "150")
ALERT = (os.getenv("SALE_GUARD_ALERT") or "1").strip().lower() in ("1", "yes", "true")
PAUSE = 2.0

# The window that ends a sale: a fixed past one (this exact spelling was accepted and cleared by OnBuy on 2026-10-10).
PAST_START = "2026-09-01 00:00:00"
PAST_END = "2026-09-02 00:00:00"


def to_float(v):
    try:
        return float(str(v).replace(",", "").replace("£", "").strip())
    except (TypeError, ValueError):
        return 0.0


def clearing_item(rec):
    """The by-sku update item that ends this listing's sale (None when it has no sale price). Price and stock are NOT part of it."""
    sku = str((rec or {}).get("sku") or "").strip()
    sale = to_float((rec or {}).get("sale_price"))
    if not sku or sale <= 0:
        return None
    price = to_float(rec.get("price"))
    if price > 0 and sale > price:
        sale = price                       # a sale above the price (the price was lowered later) would not validate
    return {"sku": sku, "boost_marketing_commission": 0, "sale_price": f"{sale:.2f}",
            "sale_start_date": PAST_START, "sale_end_date": PAST_END}


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def put_chunk(onbuy, items, sleep=time.sleep):
    """(results, error): the per-item result list of one PUT /v2/listings/by-sku, or (None, text) when the request itself failed."""
    payload = {"site_id": onbuy.site_id, "seller_id": onbuy.seller_id, "listings": items}
    resp = None
    for attempt in range(6):
        resp = onbuy._send("PUT", f"{BASE_URL}/listings/by-sku", what=f"sale guard update ({len(items)} SKUs)", json=payload, timeout=120)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 5:
            print(f"sale guard: HTTP {resp.status_code} on a chunk of {len(items)} - waiting 120s before retrying", flush=True)
            sleep(120)
            continue
        break
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code} {resp.text[:300]}"
    try:
        body = resp.json()
    except ValueError:
        return None, f"HTTP 200 but no JSON: {resp.text[:300]}"
    results = body.get("results") if isinstance(body, dict) else body
    return (results if isinstance(results, list) else []), None


def describe(on_sale):
    """A few facts about the sales found (selling prices and dates only)."""
    lines = [f"start dates: {dict(Counter(str(r.get('sale_start_date'))[:10] for r in on_sale).most_common(6))}",
             f"end dates:   {dict(Counter(str(r.get('sale_end_date'))[:10] for r in on_sale).most_common(6))}"]
    ratios = []
    for r in on_sale:
        p, s = to_float(r.get("price")), to_float(r.get("sale_price"))
        if p > 0 and s > 0:
            ratios.append(s / p)
    if ratios:
        bands = Counter("<0.7" if x < 0.7 else "0.7-0.9" if x < 0.9 else "0.9-0.99" if x < 0.99 else "0.99-1.0" if x <= 1.0 else ">1.0" for x in ratios)
        lines.append(f"sale price / price bands: {dict(bands)}")
    lines.append("examples: " + "; ".join(f"{r.get('sku')} {r.get('price')} -> {r.get('sale_price')}" for r in on_sale[:5]))
    return lines


def end_sales(onbuy, on_sale, dry_run=True, max_fix=0, chunk=CHUNK, sleep=time.sleep):
    """Send the past-window update for the listings in `on_sale`; returns {"sent", "ok", "errors", "error_text", "ended_skus"}."""
    todo = [it for it in (clearing_item(r) for r in on_sale) if it]
    if max_fix > 0:
        todo = todo[:max_fix]
    stats = {"planned": len(todo), "sent": 0, "ok": 0, "errors": 0, "failed_requests": 0, "error_text": Counter(), "ended_skus": []}
    if dry_run:
        return stats
    for part in chunks(todo, max(1, chunk)):
        results, err = put_chunk(onbuy, part, sleep)
        stats["sent"] += len(part)
        if results is None:
            stats["failed_requests"] += 1
            stats["errors"] += len(part)
            stats["error_text"][err or "request failed"] += len(part)
            print(f"sale guard: a chunk of {len(part)} failed: {err}", flush=True)
        else:
            answered = {str(r.get("sku") or "").strip(): r for r in results if isinstance(r, dict)}
            for it in part:
                r = answered.get(it["sku"])
                if r is not None and r.get("error"):
                    stats["errors"] += 1
                    stats["error_text"][str(r["error"])[:160]] += 1
                else:
                    stats["ok"] += 1
                    stats["ended_skus"].append(it["sku"])
        sleep(PAUSE)
    return stats


def verify(onbuy, skus, wait, sleep=time.sleep):
    """Read a few ended listings back after `wait` seconds (filtered GETs) and print what OnBuy says now."""
    if not skus:
        return
    print(f"sale guard: reading {len(skus)} ended listing(s) back in {wait}s ...", flush=True)
    sleep(wait)
    for sku in skus:
        try:
            rec = onbuy.get_listing(sku)
        except Exception as exc:  # noqa: BLE001 - a read-back failure is information, not an error of the guard
            print(f"sale guard: read-back of {sku} failed: {exc}", flush=True)
            continue
        if rec is None:
            print(f"sale guard: {sku}: no listing answered", flush=True)
            continue
        state = {k: rec.get(k) for k in ("price", "stock", "sale_price", "sale_start_date", "sale_end_date", "updated_at")}
        print(f"sale guard: {sku}: {json.dumps(state, default=str)}", flush=True)


def spread(seq, n):
    if n <= 0 or not seq:
        return []
    if len(seq) <= n:
        return list(seq)
    step = len(seq) / n
    return [seq[int(i * step)] for i in range(n)]


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    listings = listings_cache.load()
    if listings is None:
        listings, short, meta, pages = listing_sweep.sweep(onbuy)
        listings_cache.save(listings)
        print(f"sale guard: swept {len(listings)} listings in {pages} request(s); short pages: {short[:10]}{' ...' if len(short) > 10 else ''}", flush=True)
    else:
        print(f"sale guard: using the shared sweep of {len(listings)} listings", flush=True)
    on_sale = [r for r in listings if listing_sweep.has_sale(r)]
    print(f"sale guard: {len(on_sale)} of {len(listings)} listings carry a sale price", flush=True)
    for line in describe(on_sale) if on_sale else []:
        print("sale guard: " + line, flush=True)
    if not on_sale:
        return
    stats = end_sales(onbuy, on_sale, dry_run=DRY_RUN, max_fix=MAX_FIX)
    if DRY_RUN:
        print(f"sale guard: DRY RUN - would end the sale on {stats['planned']} listing(s) in {-(-stats['planned'] // max(1, CHUNK))} request(s)", flush=True)
        return
    print(f"sale guard: sent {stats['sent']} | accepted {stats['ok']} | errors {stats['errors']} ({stats['failed_requests']} failed request(s))", flush=True)
    for text, n in stats["error_text"].most_common(8):
        print(f"sale guard: error x{n}: {text}", flush=True)
    if ALERT:
        try:
            import notify
            notify.send_alert_email(
                f"Sale prices found on {len(on_sale)} listings - ended",
                f"{len(on_sale)} of {len(listings)} OnBuy listings carried a SALE price (the dashboard shows 'On Sale').\n"
                f"Ended: {stats['ok']} accepted, {stats['errors']} refused.\n" + "\n".join(describe(on_sale)) + "\n"
                "OnBuy drops an ended sale within a few minutes; the next guard run (or sale_price_probe) confirms it.")
        except Exception as exc:  # noqa: BLE001 - an alert failure must never fail the guard
            print(f"sale guard: alert mail not sent: {exc}", flush=True)
    verify(onbuy, spread(stats["ended_skus"], VERIFY_SAMPLE), VERIFY_WAIT)
    if stats["ok"] == 0 and stats["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
