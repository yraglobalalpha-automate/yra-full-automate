"""One-off, READ-ONLY snapshot of OnBuy's full /v2/queues submission history.

OnBuy support (2026-07-08) confirmed platform-wide processing delays caused
our duplicate product submissions, and asked us to "retain the details of
any affected SKUs and OPCs" until they can assess remediation. Those details
live in OnBuy's own queue history - which we shouldn't assume stays
available indefinitely - so this captures all of it on our side:

- queue_history_snapshot.csv : every queue entry (SKU, status, OPC,
  queue_id, product_url, error message), newest first as OnBuy returns them.
- duplicates_report.csv      : one row per SKU with MORE than one successful
  submission - i.e. exactly the duplicate-OPC records OnBuy asked us to
  retain, with every OPC/queue_id listed.

Makes no changes anywhere. Run from the Actions tab and download the
artifact; keep the files somewhere safe (artifacts expire after ~90 days).
"""
import csv
import os
from collections import defaultdict

from onbuy_client import OnBuyClient

MAX_PAGES = int(os.getenv("QUEUE_SNAPSHOT_MAX_PAGES") or "200")
PAGE_SIZE = 50
SNAPSHOT_FILE = "queue_history_snapshot.csv"
DUPLICATES_FILE = "duplicates_report.csv"

use_sandbox = os.getenv("ONBUY_USE_SANDBOX", "false").strip().lower() == "true"
onbuy = OnBuyClient(use_sandbox=use_sandbox)
if not onbuy.authenticate():
    print("FAILED to authenticate with OnBuy")
    raise SystemExit(1)

print(f"Paging through up to {MAX_PAGES} page(s) of {PAGE_SIZE} queue entries...")

entries = []
offset = 0
for _ in range(MAX_PAGES):
    try:
        result = onbuy.list_queue(limit=PAGE_SIZE, offset=offset)
    except Exception as exc:
        print(f"Queue lookup failed at offset {offset} - snapshot is partial from here: {exc}")
        break
    page = result.get("results", []) if isinstance(result, dict) else []
    if not page:
        break
    entries.extend(page)
    offset += PAGE_SIZE

print(f"Captured {len(entries)} queue entrie(s).")

with open(SNAPSHOT_FILE, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f)
    writer.writerow(["SKU", "status", "OPC", "queue_id", "product_url", "error_message"])
    for e in entries:
        writer.writerow([
            str(e.get("uid", "")).strip(),
            e.get("status", ""),
            e.get("opc", ""),
            e.get("queue_id", ""),
            e.get("product_url", ""),
            e.get("error_message", ""),
        ])
print(f"Wrote {SNAPSHOT_FILE}")

by_sku = defaultdict(list)
for e in entries:
    sku = str(e.get("uid", "")).strip()
    if sku and e.get("status") == "success":
        by_sku[sku].append(e)

duplicates = {sku: subs for sku, subs in by_sku.items() if len(subs) > 1}
with open(DUPLICATES_FILE, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f)
    writer.writerow(["SKU", "successful_submissions", "OPCs (newest first)", "queue_ids (newest first)"])
    for sku, subs in sorted(duplicates.items()):
        writer.writerow([
            sku,
            len(subs),
            "; ".join(str(s.get("opc", "")) for s in subs),
            "; ".join(str(s.get("queue_id", "")) for s in subs),
        ])
print(f"Wrote {DUPLICATES_FILE}: {len(duplicates)} SKU(s) with duplicate successful submissions")
print("No changes were made anywhere - snapshot only.")
