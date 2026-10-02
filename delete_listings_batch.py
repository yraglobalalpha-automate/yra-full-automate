"""Batched by-SKU listing deletion (2026-10-02).

delete_listings.py sends ONE SKU per request, and OnBuy allows 240 DELETE
requests an hour - a 1,700-listing clean-up held the API for ~7 hours (and a
SKU that is not on OnBuy still costs a request). DELETE /v2/listings/by-sku
takes an array, so this sends many SKUs per request and reads each SKU's own
answer from the results node.

SAFETY: runs only on an explicit file of SKUs (DELETE_SKUS_FILE, one per line);
DRY_RUN is on unless set to 0. The first request is small (DELETE_FIRST_BATCH)
and is checked - every SKU in it must be answered - before the batch size
grows; if the endpoint answers a batch badly the tool shrinks, and at size 1
it is the old single-SKU behaviour. SKUs are sent in the spelling the platform
holds (sku_aliases: 21 YRA listings carry a prepended zero).
"""
import csv
import logging
import os
import time

import requests

import sku_aliases
from onbuy_client import BASE_URL, OnBuyClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def classify(node):
    """One SKU's answer -> 'ok', 'gone', 'suspended' or the error text."""
    if isinstance(node, dict):
        err = str(node.get("error") or "").strip()
        if not err and str(node.get("status") or "").lower() == "ok":
            return "ok"
    else:
        err = str(node or "").strip()
    low = err.lower()
    if "not found" in low or "does not exist" in low:
        return "gone"
    if "suspended" in low:
        return "suspended"
    return err or "no answer"


def parse_results(body):
    """{true SKU: outcome} for every SKU the response answered."""
    results = body.get("results") if isinstance(body, dict) else None
    out = {}
    if isinstance(results, dict):
        for k, node in results.items():
            out[sku_aliases.to_true(k)] = classify(node)
    elif isinstance(results, list):
        for node in results:
            if isinstance(node, dict) and node.get("sku") is not None:
                out[sku_aliases.to_true(node["sku"])] = classify(node)
    return out


def send(onbuy, wire_skus):
    """One DELETE request; waits out the hourly cap and retries server errors.
    Returns (status code, parsed body or None, text)."""
    server_errors = 0
    for _ in range(80):
        try:
            resp = onbuy._send("DELETE", f"{BASE_URL}/listings/by-sku", what=f"onbuy delete batch({len(wire_skus)})",
                               json={"site_id": onbuy.site_id, "skus": wire_skus}, timeout=180)
        except requests.RequestException as exc:
            server_errors += 1
            if server_errors >= 4:
                return 599, None, f"network error: {str(exc)[:200]}"
            log.warning("network error (%s) - retrying in 20s", str(exc)[:120])
            time.sleep(20)
            continue
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After") or 0) if str(resp.headers.get("Retry-After") or "").isdigit() else 0
            log.warning("rate limited (%s) - waiting %ds", resp.text[:120], max(wait, 90))
            time.sleep(max(wait, 90))
            continue
        if resp.status_code >= 500:
            server_errors += 1
            if server_errors >= 4:
                return resp.status_code, None, resp.text[:300]
            log.warning("server error %s - retrying in 20s", resp.status_code)
            time.sleep(20)
            continue
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, body, resp.text[:300]
    return 429, None, "rate limit never cleared"


def main():
    path = os.environ["DELETE_SKUS_FILE"]
    dry = (os.getenv("DRY_RUN") or "1").strip().lower() in ("1", "yes", "true")
    batch = int(os.getenv("DELETE_BATCH") or "100")
    first = int(os.getenv("DELETE_FIRST_BATCH") or "10")
    with open(path, encoding="utf-8") as fh:
        skus = list(dict.fromkeys(l.strip() for l in fh if l.strip() and not l.startswith("#")))
    if not skus:
        raise SystemExit("no SKUs - this tool never runs without an explicit list")
    aliased = sum(1 for s in skus if sku_aliases.to_onbuy(s) != s)
    log.info("SKUs to delete: %d (%d sent under their platform spelling) %s", len(skus), aliased,
             "- DRY RUN, nothing deleted" if dry else "")
    if dry:
        return
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")

    outcomes, size, idx, requests_sent = {}, min(first, len(skus)), 0, 0
    validated = shrunk = False
    while idx < len(skus):
        chunk = skus[idx:idx + size]
        wire = [sku_aliases.to_onbuy(s) for s in chunk]
        status, body, text = send(onbuy, wire)
        requests_sent += 1
        parsed = parse_results(body)
        if requests_sent <= 3:
            log.info("batch %d (%d SKUs) -> HTTP %s: %s", requests_sent, len(chunk), status, text)
        answered = [s for s in chunk if s in parsed]
        if not answered:
            if size > 1:
                size, shrunk = max(1, size // 4), True
                log.warning("batch not answered per SKU (HTTP %s: %s) - shrinking to %d", status, text[:160], size)
                continue
            outcomes[chunk[0]] = f"error: {text[:100]}"
            idx += 1
            continue
        for s in chunk:
            outcomes[s] = parsed.get(s, "no answer")
        idx += len(chunk)
        if len(answered) < len(chunk):
            log.warning("%d of %d SKUs got no answer in this batch - they are retried singly at the end",
                        len(chunk) - len(answered), len(chunk))
        elif not validated:
            validated = True
            if not shrunk and size < batch:
                size = batch
                log.info("first batch answered every SKU - batch size now %d", size)
        time.sleep(1.0)

    # second chance, one at a time, for anything that was not a clean answer
    again = [s for s, o in outcomes.items() if o not in ("ok", "gone", "suspended")]
    for s in again:
        status, body, text = send(onbuy, [sku_aliases.to_onbuy(s)])
        requests_sent += 1
        outcomes[s] = parse_results(body).get(s) or f"error: {text[:100]}"
        time.sleep(1.0)

    tally = {}
    for o in outcomes.values():
        k = o if o in ("ok", "gone", "suspended") else "other error"
        tally[k] = tally.get(k, 0) + 1
    log.info("DONE in %d request(s): %s", requests_sent, ", ".join(f"{v} {k}" for k, v in sorted(tally.items())))
    with open("delete_results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sku", "outcome"])
        w.writerows(sorted(outcomes.items()))
    for s, o in outcomes.items():
        if o == "suspended":
            log.info("SUSPENDED (needs OnBuy): %s", s)
    errs = [(s, o) for s, o in outcomes.items() if o not in ("ok", "gone", "suspended")]
    for s, o in errs[:30]:
        log.warning("ERROR %s: %s", s, o)
    if len(errs) > max(5, len(skus) // 20):
        raise SystemExit(f"{len(errs)} SKUs ended in an error - see delete_results.csv")


if __name__ == "__main__":
    main()
