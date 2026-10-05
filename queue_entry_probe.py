"""READ-ONLY (2026-10-05): what does OnBuy's queue history say about these SKUs?

A product submission (create / repair re-submit) only returns "accepted into the async queue"; its real outcome - success, or the
error that made OnBuy refuse it - lives in GET /v2/queues, newest first, matched by uid (= our SKU). This pages that history and
prints, per requested SKU, every entry it can still see (status, error message, OPC, dates) plus how far back the history reaches.

Env: SKUS (comma-separated, leading zeros ignored), MAX_PAGES (default 120 pages of 50 = 6,000 entries = 120 GETs of the
240-an-hour quota). Writes nothing anywhere.
"""
import json
import os

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
            uid = str(e.get("uid") or "").strip()
            if (uid.lstrip("0") or uid) in KEY:
                hits.append(e)
        if len(entries) < PAGE:
            break
    print(f"queue entries seen: {seen} | the history reaches back to {oldest}")
    print(f"entries for the requested SKUs: {len(hits)}")
    for e in sorted(hits, key=when):
        keep = {k: e.get(k) for k in ("uid", "status", "opc", "error_message", "queue_id", "product_url") if k in e}
        print(f"ENTRY {when(e)} | " + json.dumps(keep, ensure_ascii=False, default=str)[:600])
    for s in WANT:
        if not any((str(h.get("uid") or "").lstrip("0") or "") == (s.lstrip("0") or s) for h in hits):
            print(f"NO ENTRY IN THE VISIBLE HISTORY: {s}")


if __name__ == "__main__":
    main()
