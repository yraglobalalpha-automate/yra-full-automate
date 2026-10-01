"""READ-ONLY: every frozen / flagged row (Sync Status Failed:/BRAND BLOCKED/
Skipped) across both product tabs, whether its OnBuy listing is live, and -
for duplicate-link/ASIN flags - what state the KEEPER row (the earlier row
that owns the supplier product) is in. A frozen row never gets an OnBuy push
(generate_xml gates both the normal push and the OOS zero batch on the flag),
so a live frozen listing cannot be taken offline when its supplier sells out.
Writes only a CSV artifact; nothing in the Sheet, OnBuy or Supabase.
"""
import csv
import json
import os
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import sheet_tabs

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"


def to_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def load(ws):
    values = ws.get_all_values()
    h = [str(x).strip() for x in values[0]]
    ix = {k: i for i, k in enumerate(h) if k}

    def cell(r, k):
        i = ix.get(k)
        return str(r[i]).strip() if i is not None and i < len(r) else ""
    rows = {}
    for n, r in enumerate(values[1:], start=2):
        sku = cell(r, "SKU")
        if not sku and not cell(r, "Supplier URL"):
            continue
        rows[n] = {"row": n, "tab": ws.title, "sku": sku, "stock": to_int(cell(r, "Stock")),
                   "status": cell(r, "Status").upper(), "created": cell(r, "OnBuy Product Created").upper(),
                   "sync": cell(r, "Sync Status"), "price": cell(r, "Selling Price (£)"),
                   "lastchk": cell(r, "Last Checked Time"), "url": cell(r, "Supplier URL")[:70]}
    return rows


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(SHEET_NAME)
    tabs = {"Sheet1": load(sheet_tabs.product_sheet(book))}
    try:
        tabs["Amazon"] = load(book.worksheet("Amazon"))
    except gspread.exceptions.WorksheetNotFound:
        pass

    def live_in_stock(r):
        return r["created"] == "TRUE" and (r["stock"] or 0) > 0 and r["status"] == "ACTIVE"

    out, summary = [], {}
    for tab, rows in tabs.items():
        for n, r in rows.items():
            s = r["sync"]
            if not s.startswith(("Failed", "BRAND BLOCKED", "Skipped")):
                continue
            keeper = None
            m = re.search(r"on row (\d+)(?: \((\w+)\))?", s)
            if m and ("supplier link" in s or "ASIN" in s):
                kt = m.group(2) or tab
                keeper = tabs.get(kt, {}).get(int(m.group(1)))
            kind = ("dup link/ASIN" if (m and ("supplier link" in s or "ASIN" in s))
                    else "dup SKU" if "SKU appears" in s or "registered to" in s or "already used on the eBay tab" in s
                    else "brand blocked" if s.startswith("BRAND") else "other")
            k_state = ("n/a" if keeper is None else "keeper live+in stock" if live_in_stock(keeper)
                       else "keeper live, OOS" if keeper["created"] == "TRUE" else "keeper NOT live")
            out.append([tab, n, r["sku"], kind, r["created"], r["stock"], r["status"], r["price"], r["lastchk"],
                        k_state, (f"{keeper['tab']} row {keeper['row']}" if keeper else ""), s[:150]])
            key = (tab, kind, "live in stock" if live_in_stock(r) else "live OOS/inactive" if r["created"] == "TRUE" else "not live", k_state)
            summary[key] = summary.get(key, 0) + 1
    print(f"frozen/flagged rows: {len(out)}")
    for key in sorted(summary, key=lambda k: -summary[k]):
        print(f"  {summary[key]:5d}  {key[0]:7s} | {key[1]:14s} | own listing: {key[2]:18s} | {key[3]}")
    with open("frozen_rows.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tab", "row", "sku", "kind", "created", "stock", "status", "price", "last_checked",
                    "keeper_state", "keeper", "flag"])
        w.writerows(out)


if __name__ == "__main__":
    main()
