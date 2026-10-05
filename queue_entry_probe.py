"""READ-ONLY (2026-10-05): what does OnBuy's queue history say about these SKUs?

A product submission (create / repair re-submit) only returns "accepted into the async queue"; its real outcome - success, or the
error that made OnBuy refuse it - lives in GET /v2/queues, newest first, matched by uid (= our SKU). This pages that history and
prints, per requested SKU, every entry it can still see (status, error message, OPC, dates) plus how far back the history reaches.

Env: SKUS (comma-separated, leading zeros ignored), MAX_PAGES (default 120 pages of 50 = 6,000 entries = 120 GETs of the
240-an-hour quota). Writes nothing anywhere.
"""
import json
import os
from collections import Counter

from onbuy_client import OnBuyClient

WANT = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
KEY = {s.lstrip("0") or s for s in WANT}
MAX_PAGES = int(os.getenv("MAX_PAGES") or "120")
PAGE = 50
DATE_KEYS = ("created_at", "date_created", "created", "date", "updated_at", "processed_at")


def when(entry):
    for k in DATE_KEYS:
        if entry.get(k):
            return str(entry[k])
    return "?"


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    seen, oldest, hits, shown_keys = 0, None, [], False
    statuses, levels, failed_samples, key_sets = Counter(), Counter(), [], Counter()
    for page in range(MAX_PAGES):
        result = onbuy.list_queue(limit=PAGE, offset=page * PAGE)
        entries = result.get("results", []) if isinstance(result, dict) else []
        if not entries:
            break
        if not shown_keys:
            print("entry keys:", sorted(entries[0].keys()))
            shown_keys = True
        for e in entries:
            seen += 1
            oldest = when(e)
            statuses[str(e.get("status"))] += 1
            levels[json.dumps(e.get("permitted_write_levels"), sort_keys=True, default=str)] += 1
            key_sets[tuple(sorted(e.keys()))] += 1
            if str(e.get("status")).lower() not in ("success", "pending") and len(failed_samples) < 3:
                failed_samples.append({k: (str(v)[:200]) for k, v in e.items()})
            uid = str(e.get("uid") or "").strip()
            if (uid.lstrip("0") or uid) in KEY:
                hits.append(e)
        if len(entries) < PAGE:
            break
    print(f"queue entries seen: {seen} | the history reaches back to {oldest}")
    print("statuses:", dict(statuses.most_common(8)))
    print("permitted_write_levels:", dict(levels.most_common(8)))
    print("entry key sets:", [(list(k), n) for k, n in key_sets.most_common(4)])
    for sample in failed_samples:
        print("NOT-SUCCESS SAMPLE:", json.dumps(sample, ensure_ascii=False)[:700])
    print(f"entries for the requested SKUs: {len(hits)}")
    for e in sorted(hits, key=when):
        keep = {k: e.get(k) for k in ("uid", "status", "opc", "error_message", "queue_id", "product_url") if k in e}
        print(f"ENTRY {when(e)} | " + json.dumps(keep, ensure_ascii=False, default=str)[:600])
    for s in WANT:
        if not any((str(h.get("uid") or "").lstrip("0") or "") == (s.lstrip("0") or s) for h in hits):
            print(f"NO ENTRY IN THE VISIBLE HISTORY: {s}")


if __name__ == "__main__":
    main()
