"""READ-ONLY (2026-09-03): which of the rows we believe EXIST on the
platform does the seller API refuse to serve?

Complements audit_unaddressable.py: that one starts from the queue
history, which only reaches ~7,500 entries back, so anything created in
the July bulk-upload era falls outside it. This starts from the sheet
instead - every SKU whose row carries an OPC, or a status that says the
product was created (Synced / Pending Approval / Awaiting OnBuy go-live /
suspended) - and asks check-winning about all of them in batches. The
refused ones are the complete "exists but unaddressable" set, whatever
their age. A handful of check-winning calls, no queue walk, no changes.
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
CHECK_BATCH = int(os.getenv("CHECK_BATCH") or "500")
OUT = "check_refused.csv"

CREATED_PREFIXES = ("Synced", "Pending Approval", "Awaiting OnBuy go-live")


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = with_retry(lambda: client.open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    values = with_retry(lambda: sheet.get_all_values(), what="sheet read", max_attempts=3)
    idx = {h.strip().lower(): i for i, h in enumerate(values[0])}
    i_sku, i_status, i_opc = idx["sku"], idx.get("sync status"), idx.get("opc")

    def cell(row, i):
        return (row[i] if i is not None and i < len(row) else "").strip()

    candidates = {}
    for r in range(2, len(values) + 1):
        row = values[r - 1]
        sku = cell(row, i_sku)
        if not sku:
            continue
        status = cell(row, i_status)
        opc = cell(row, i_opc)
        believed_created = (
            opc.upper() not in ("", "PENDING")
            or status.startswith(CREATED_PREFIXES)
            or "suspended" in status.lower()
        )
        if believed_created and sku not in candidates:
            candidates[sku] = (r, opc, status[:80])
    print(f"sheet rows: {len(values) - 1} | believed created on the platform: {len(candidates)}")

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")

    skus = sorted(candidates, key=lambda s: candidates[s][0])
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
        for item in res:
            item = item or {}
            sku = str(item.get("sku") or "").strip()
            if not sku:
                continue
            answered.add(sku)
            if str(item.get("error") or "").strip():
                refused.add(sku)
            else:
                served.add(sku)
        refused.update(s for s in chunk if s not in answered)
        time.sleep(1.0)

    print(f"served by the API: {len(served)} | refused (SKU does not exist): {len(refused)}")
    with open(OUT, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["sku", "sheet_opc", "sheet_row", "sheet_status"])
        for s in sorted(refused, key=lambda x: candidates[x][0]):
            r, opc, status = candidates[s]
            w.writerow([s, opc, r, status])
    print(f"wrote {OUT}: {len(refused)} row(s)")
    for s in sorted(refused, key=lambda x: candidates[x][0])[:15]:
        r, opc, status = candidates[s]
        print(f"   row {r} | {s} | opc {opc or '-'} | {status[:60]}")


if __name__ == "__main__":
    main()
