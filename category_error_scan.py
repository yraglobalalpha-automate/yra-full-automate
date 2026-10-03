"""READ-ONLY (2026-10-03): which rows did OnBuy refuse with "Category 'N' is not a lowest level category"?

OnBuy's category tree grows child categories under ones we list in (first seen 2026-08-21: 3472, 13705, ...); the
create then fails and the row sits at "Failed: An error occurred: Category '38213' is not a lowest level
category". This reads every product tab and reports, per category id named in such a status: how many rows, on
which tab, what our category file says about the id (still listed? under which path), and a sample. The full row
list goes to OUT_DIR/category_error_rows.csv (uploaded as an artifact). Rows that already carry such an id in their
Category ID cell but have not failed yet (never created) are counted too.

Writes nothing to the sheet, Supabase or OnBuy.
"""
import csv
import json
import os
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import sheet_tabs
from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
OUT_DIR = os.getenv("OUT_DIR") or "out"
RX = re.compile(r"Category '?(\d+)'? is not a lowest level category", re.I)


def main():
    listed = {}
    with open("onbuy_categories_only.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            listed[str(r.get("Category ID")).strip()] = r.get("OnBuy Category Path")
    print(f"category file: {len(listed)} listable categories")

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        amz = book.worksheet(sheet_tabs.AMAZON_TAB)
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass

    rows, other_category_failures = [], {}
    for ws in tabs:
        values = with_retry(lambda ws=ws: ws.get_all_values(), what=f"read {ws.title}", max_attempts=3)
        header = [str(h).strip() for h in values[0]]
        ix = {h: i for i, h in enumerate(header) if h}

        def cell(r, col):
            i = ix.get(col)
            return str(r[i]).strip() if i is not None and i < len(r) else ""
        n_rows = 0
        for n, r in enumerate(values[1:], start=2):
            if not cell(r, "SKU"):
                continue
            n_rows += 1
            status = cell(r, "Sync Status")
            m = RX.search(status)
            if m:
                rows.append({"tab": ws.title, "row": n, "sku": cell(r, "SKU"), "bad_id": m.group(1),
                             "category": cell(r, "Category"), "category_id": cell(r, "Category ID"),
                             "created": cell(r, "OnBuy Product Created"), "opc": cell(r, "OPC"),
                             "title": cell(r, "Title")[:100], "status": status[:150]})
            elif "categor" in status.lower() and status.lower().startswith("failed"):
                k = status[:90]
                other_category_failures[k] = other_category_failures.get(k, 0) + 1
        print(f"tab {ws.title!r}: {n_rows} rows with a SKU")

    by_id = {}
    for r in rows:
        by_id.setdefault(r["bad_id"], []).append(r)
    print(f"\nrows refused as 'not a lowest level category': {len(rows)} across {len(by_id)} category id(s)")
    for cid, items in sorted(by_id.items(), key=lambda kv: -len(kv[1])):
        tabs_n = {}
        for r in items:
            tabs_n[r["tab"]] = tabs_n.get(r["tab"], 0) + 1
        cats = {}
        for r in items:
            cats[r["category"][:90]] = cats.get(r["category"][:90], 0) + 1
        in_file = f"STILL LISTED in our file as {listed[cid]!r}" if cid in listed else "no longer in our category file"
        print(f"- id {cid}: {len(items)} rows {tabs_n} | {in_file}")
        print(f"    category cells: {dict(sorted(cats.items(), key=lambda kv: -kv[1])[:4])}")
        print(f"    OnBuy Product Created: {dict((k, sum(1 for r in items if r['created'].upper() == k)) for k in ('TRUE', 'FALSE', ''))}")
        for r in items[:4]:
            print(f"    e.g. {r['tab']} row {r['row']} SKU {r['sku']}: {r['title']!r}")
    if other_category_failures:
        print("\nother category-related Failed statuses (counts):")
        for k, v in sorted(other_category_failures.items(), key=lambda kv: -kv[1])[:8]:
            print(f"   {v:5d}  {k}")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "category_error_rows.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Tab", "Row", "SKU", "Category id named in the error", "Category cell", "Category ID cell",
                    "OnBuy Product Created", "OPC", "Title", "Sync Status"])
        for r in rows:
            w.writerow([r["tab"], r["row"], r["sku"], r["bad_id"], r["category"], r["category_id"], r["created"],
                        r["opc"], r["title"], r["status"]])


if __name__ == "__main__":
    main()
