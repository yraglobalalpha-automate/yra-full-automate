"""READ-ONLY (2026-09-03): the complete list OnBuy support asked for -
every SKU whose submission COMPLETED SUCCESSFULLY in the processing queue
(so it has an OPC and a product URL) yet is still not addressable through
the seller API: check-winning answers "SKU does not exist" and by-SKU
updates are refused, so price corrections cannot be submitted.

Support has confirmed this state is unexpected on their side and asked
for the full set of affected SKUs and queue IDs to hand their Product
and Development teams. Sources used, all OnBuy's own records:
  1. /v2/queues history - newest success per SKU (queue_id, OPC).
  2. /v2/listings/check-winning in batches of CHECK_BATCH - the SKUs the
     API actually serves (max 1,000 per request per support, 2026-08-24).

Writes audit_unaddressable.csv. Makes no changes anywhere.
"""
import csv
import json
import os
import time

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import OnBuyClient
from retry_utils import PermanentError, RateLimitError, with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
MAX_PAGES = int(os.getenv("MAX_PAGES") or "150")
CHECK_BATCH = int(os.getenv("CHECK_BATCH") or "500")
OUT = "audit_unaddressable.csv"


def fetch_queue_successes(onbuy):
    """uid -> (queue_id, opc) for the NEWEST successful submission per uid
    (the queue pages newest-first, so first-seen wins)."""
    successes = {}
    offset, pages = 0, 0
    while pages < MAX_PAGES:
        try:
            result = onbuy.list_queue(limit=50, offset=offset)
        except RateLimitError:
            print(f"quota at offset {offset} - waiting 90s")
            time.sleep(90)
            continue
        except Exception as exc:
            print(f"queue page at offset {offset} failed ({exc}) - continuing with what we have")
            break
        page = result.get("results", []) if isinstance(result, dict) else []
        if not page:
            break
        for e in page:
            uid = str(e.get("uid") or "").strip()
            if uid and e.get("status") == "success" and uid not in successes:
                successes[uid] = (str(e.get("queue_id") or ""), str(e.get("opc") or ""))
        offset += 50
        pages += 1
        time.sleep(0.3)
    print(f"queue history: {pages} page(s), {len(successes)} SKU(s) with a successful submission")
    return successes


def fetch_addressable(onbuy, skus):
    """SKUs the API serves vs SKUs it refuses, via check-winning batches.
    An entry carrying an error string counts as refused; a SKU missing
    from the answer entirely is counted refused as well."""
    served, refused = set(), set()
    queue = [skus[i:i + CHECK_BATCH] for i in range(0, len(skus), CHECK_BATCH)]
    while queue:
        chunk = queue.pop(0)
        try:
            res = onbuy.check_winning(chunk) or []
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
                print(f"check-winning rejected a batch of {len(chunk)} - splitting")
                continue
            print(f"check-winning failed for {len(chunk)} SKU(s): {str(exc)[:120]}")
            refused.update(chunk)
            continue
        answered = set()
        for r in res:
            r = r or {}
            sku = str(r.get("sku") or "").strip()
            if not sku:
                continue
            answered.add(sku)
            if str(r.get("error") or "").strip():
                refused.add(sku)
            else:
                served.add(sku)
        refused.update(s for s in chunk if s not in answered)
        time.sleep(1.0)
    return served, refused


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = with_retry(lambda: client.open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    values = with_retry(lambda: sheet.get_all_values(), what="sheet read", max_attempts=3)
    idx = {h.strip().lower(): i for i, h in enumerate(values[0])}
    i_sku, i_status = idx["sku"], idx.get("sync status")

    def cell(row, i):
        return (row[i] if i is not None and i < len(row) else "").strip()

    sheet_rows = {}
    for r in range(2, len(values) + 1):
        s = cell(values[r - 1], i_sku)
        if s:
            sheet_rows.setdefault(s, r)
    print(f"sheet: {len(sheet_rows)} SKU(s)")

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")

    successes = fetch_queue_successes(onbuy)
    ours = sorted(set(sheet_rows) & set(successes))
    print(f"of our sheet SKUs, {len(ours)} have a successful queue submission")

    served, refused = fetch_addressable(onbuy, ours)
    print(f"served by the API: {len(served)} | refused (SKU does not exist): {len(refused)}")

    affected = sorted(refused, key=lambda s: sheet_rows[s])
    with open(OUT, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["sku", "queue_id", "opc", "sheet_row", "sheet_status"])
        for s in affected:
            qid, opc = successes[s]
            st = cell(values[sheet_rows[s] - 1], i_status)[:80]
            w.writerow([s, qid, opc, sheet_rows[s], st])
    print(f"wrote {OUT}: {len(affected)} SKU(s) successfully processed yet unaddressable")
    for s in affected[:12]:
        qid, opc = successes[s]
        print(f"   {s}  queue {qid}  opc {opc}")


if __name__ == "__main__":
    main()
