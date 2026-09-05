"""READ-ONLY: the sheet's current verdict on a list of SKUs, as a worklist.

Built for the never-live set (1,125 SKUs, 2026-09-05): after their stale
OnBuy state was cleared, the sync re-created the few it could and stamped
the rest with WHY it could not - a dead eBay link, no usable price, out
of stock at source. This reads those stamps back by SKU (never by row
number - rows move) and splits the list into buckets a person can act on.

Writes status_by_sku.csv: sku, row, bucket, status, cost, sell, link.
Touches nothing.
"""
import csv
import json
import os
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
SKUS = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
OUT = "status_by_sku.csv"


def bucket(status, cost, sell, link, opc, checked):
    s = status.lower()
    if not link:
        return "no supplier link"
    if s.startswith("synced") or s.startswith("pending approval"):
        return "live / re-created"
    if s.startswith("awaiting onbuy go-live"):
        return "created, awaiting go-live"
    if "dead ebay link" in s or "unavailable" in s or "no longer available" in s:
        return "dead eBay link"
    if "no usable selling price" in s or "no-price" in s or (not cost and not sell):
        return "no price (source OOS or unpriced)"
    if s.startswith("failed"):
        return "failed: " + re.sub(r"\s+", " ", status)[8:70]
    if "brand" in s:
        return "brand blocked"
    if not status and not checked:
        return "not yet processed"
    return "other: " + (status[:50] or "blank")


def main():
    if not SKUS:
        raise SystemExit("SKUS required")
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    sheet = gspread.authorize(creds).open(SHEET_NAME).sheet1
    values = sheet.get_all_values()
    idx = {h.strip().lower(): i for i, h in enumerate(values[0])}

    def cell(row, name):
        i = idx.get(name.lower())
        return (row[i] if i is not None and i < len(row) else "").strip()

    by_sku = {}
    for r in range(2, len(values) + 1):
        s = cell(values[r - 1], "SKU")
        if s and s not in by_sku:
            by_sku[s] = r

    rows, missing = [], []
    for s in SKUS:
        r = by_sku.get(s)
        if not r:
            missing.append(s)
            continue
        row = values[r - 1]
        status, cost, sell = cell(row, "Sync Status"), cell(row, "Cost Price (£)"), cell(row, "Selling Price (£)")
        link, opc, checked = cell(row, "Supplier URL"), cell(row, "OPC"), cell(row, "Last Checked Time")
        try:
            c, p = float(cost or 0), float(sell or 0)
        except ValueError:
            c, p = 0.0, 0.0
        rows.append({"sku": s, "row": r, "bucket": bucket(status, c, p, link, opc, checked),
                     "status": status[:90], "cost": cost, "sell": sell, "opc": opc,
                     "link": link[:80]})

    with open(OUT, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["sku", "row", "bucket", "status", "cost", "sell", "opc", "link"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda x: (x["bucket"], x["row"])))

    from collections import Counter
    print(f"SKUs asked: {len(SKUS)} | found: {len(rows)} | not in sheet: {len(missing)}")
    for b, n in Counter(r["bucket"] for r in rows).most_common():
        print(f"   {n:5d}  {b}")
    if missing:
        print("not in sheet:", ", ".join(missing[:20]))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
