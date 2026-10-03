"""2026-10-03: give the rows that sit in a DENIED category (category_denylist.txt - OnBuy refuses them as
"Category 'N' is not a lowest level category") the right leaf, so the create can go through.

A row qualifies when its Category cell holds a denied category's path, or its Sync Status names a denied id. Its
Category cell is rewritten to the replacement leaf chosen by the title (RULES below); the "Failed: ..." status it
carries stays on purpose - a Failed row keeps the create fallback, so the next visit re-submits it with the new
category. Rows no rule matches are listed and left alone. Only the Category cell is written.

DRY_RUN (default on) prints the plan and writes nothing. Reads the sheet and onbuy_categories_only.csv; never
touches OnBuy or Supabase. Env: SHEET_NAME, DRY_RUN.
"""
import csv
import json
import os
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import sheet_tabs
from retry_utils import with_retry

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"

KICK = ("2338", "Toys & Games > Toys > Children's Scooters & Ride-On Toys > Children's Scooters")
STREAMERS = ("3271", "Electronics & Technology > TV & Audio > Streaming & Catchup > Media Streaming Devices")

# denied id -> (its path, [(title regex, (replacement id, replacement path))]). First rule that matches wins.
RULES = {
    "38213": ("Mobile Phones > Toys > Children's Scooters & Ride-On Toys",
              [(r"scooter", KICK)]),
    "38188": ("Home, Office & Outdoor > Cars & Automotive > In-Car Entertainment & Electronics",
              [(r"stream|chromecast|fire ?tv|roku|apple tv|tv box", STREAMERS)]),
}
ERR = re.compile(r"Category '?(\d+)'? is not a lowest level category", re.I)


def denied_id_of(category_cell, sync_status):
    """The denied category id this row is stuck on, or None."""
    text = str(category_cell or "").strip().lower()
    for cid, (path, _) in RULES.items():
        if text == path.lower():
            return cid
    m = ERR.search(str(sync_status or ""))
    if m and m.group(1) in RULES:
        return m.group(1)
    return None


def replacement_for(cid, title):
    """(id, path) for a row of denied category `cid` with this title, or None."""
    for pattern, target in RULES[cid][1]:
        if re.search(pattern, str(title or ""), re.I):
            return target
    return None


def col_letter(n):
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def main():
    listable = {}
    with open("onbuy_categories_only.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            listable[str(r.get("OnBuy Category Path") or "").strip().lower()] = str(r.get("Category ID") or "").strip()
    for cid, (path, rules) in RULES.items():
        for _, (tid, tpath) in rules:
            assert listable.get(tpath.lower()) == tid, f"replacement {tid} {tpath!r} is not in the category file"
        assert path.lower() not in listable, f"denied path {path!r} is still in the category file"

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

    total, written, unmatched = 0, 0, []
    per_rule, states = {}, {}
    for ws in tabs:
        values = with_retry(lambda ws=ws: ws.get_all_values(), what=f"read {ws.title}", max_attempts=3)
        header = [str(h).strip() for h in values[0]]
        ix = {h: i for i, h in enumerate(header) if h}
        if "Category" not in ix or "SKU" not in ix:
            print(f"tab {ws.title!r}: no Category/SKU column - skipped")
            continue

        def cell(r, col):
            i = ix.get(col)
            return str(r[i]).strip() if i is not None and i < len(r) else ""
        plan = []
        for n, r in enumerate(values[1:], start=2):
            if not cell(r, "SKU"):
                continue
            cid = denied_id_of(cell(r, "Category"), cell(r, "Sync Status"))
            if cid is None:
                continue
            target = replacement_for(cid, cell(r, "Title"))
            if target is None:
                unmatched.append((ws.title, n, cell(r, "SKU"), cid, cell(r, "Title")[:80]))
                continue
            plan.append((n, cell(r, "SKU"), cell(r, "Category"), target))
            per_rule[(cid, target[0])] = per_rule.get((cid, target[0]), 0) + 1
            state = (ws.title, cell(r, "Sync Status")[:42] or "(blank)",
                     "has OPC" if cell(r, "OPC").upper() not in ("", "PENDING") else "no OPC")
            states[state] = states.get(state, 0) + 1
        total += len(plan)
        print(f"tab {ws.title!r}: {len(plan)} row(s) to remap")
        if not plan or DRY_RUN:
            continue
        # Re-read the SKU + Category columns right before writing: a row that moved or changed since the read above
        # is skipped (redone by the next run), never overwritten blind.
        sku_col = with_retry(lambda: ws.col_values(ix["SKU"] + 1), what="sku column", max_attempts=3)
        cat_col = with_retry(lambda: ws.col_values(ix["Category"] + 1), what="category column", max_attempts=3)
        updates = []
        for n, sku, old_cat, (tid, tpath) in plan:
            same_sku = n - 1 < len(sku_col) and str(sku_col[n - 1]).strip() == sku
            same_cat = n - 1 < len(cat_col) and str(cat_col[n - 1]).strip() == old_cat
            if same_sku and same_cat:
                updates.append({"range": f"{col_letter(ix['Category'] + 1)}{n}", "values": [[tpath]]})
            else:
                print(f"  row {n} (SKU {sku}) changed since the read - skipped")
        if updates:
            with_retry(lambda: ws.batch_update([dict(u) for u in updates]), what="category write", max_attempts=3)
            written += len(updates)
    print(f"\nrows to remap: {total} | by (denied id, replacement id): {per_rule}")
    print("what state those rows are in (tab, Sync Status, OPC): " + str(dict(sorted(states.items(), key=lambda kv: -kv[1]))))
    print(f"written: {written}" if not DRY_RUN else "DRY RUN - nothing written")
    if unmatched:
        print(f"{len(unmatched)} row(s) no rule matches (left alone):")
        for u in unmatched[:30]:
            print("   ", u)


if __name__ == "__main__":
    main()
