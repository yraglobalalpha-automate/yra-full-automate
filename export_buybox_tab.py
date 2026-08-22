"""Export the latest Buy Box decisions (the "BuyBox" tab written by
buybox_defense.py) as a CSV artifact, joined with the product titles from
the main sheet. ACTIONS env filters which actions to include (default
REPRICE = prices actually pushed); "ALL" exports every contested row."""
import csv
import io
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
ACTIONS = [a.strip().upper() for a in (os.getenv("ACTIONS") or "REPRICE").split(",") if a.strip()]
OUT = os.getenv("OUT") or "buybox_export.csv"


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    ss = gspread.authorize(creds).open(SHEET_NAME)
    titles = {}
    for r in with_retry(lambda: ss.sheet1.get_all_records(), what="main sheet", max_attempts=3):
        sku = str(r.get("SKU") or "").strip()
        if sku:
            titles[sku] = (str(r.get("Title") or "").strip(), str(r.get("Cost Price (£)") or "").strip())
    try:
        tab = ss.worksheet("BuyBox")
    except gspread.WorksheetNotFound:
        raise SystemExit("no BuyBox tab yet - run buybox_defense first")
    values = with_retry(lambda: tab.get_all_values(), what="buybox tab", max_attempts=3)
    if not values:
        raise SystemExit("BuyBox tab is empty")
    header = [h.strip() for h in values[0]]
    summary = values[1] if len(values) > 1 and str(values[1][0]).startswith("run ") else None
    print("summary:", " | ".join(summary) if summary else "(none)")
    idx = {h: i for i, h in enumerate(header)}
    rows_out = []
    for row in values[2 if summary else 1:]:
        if not row or not str(row[0]).strip():
            continue
        get = lambda k: row[idx[k]].strip() if k in idx and idx[k] < len(row) else ""
        action = get("Action").upper()
        if "ALL" not in ACTIONS and action not in ACTIONS:
            continue
        sku = get("SKU")
        title, cost = titles.get(sku, ("", ""))
        rows_out.append([sku, title, cost, get("Our Price"), get("Buy Box Price"), get("New Price"), get("Floor"), action, get("Decided At")])
    with io.open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["SKU", "Title", "Cost Price (£)", "Price Before", "Buy Box Price", "New Price (pushed)", "Floor", "Action", "Decided At (UTC)"])
        w.writerows(rows_out)
    print(f"exported {len(rows_out)} row(s) with action in {ACTIONS} -> {OUT}")


if __name__ == "__main__":
    main()
