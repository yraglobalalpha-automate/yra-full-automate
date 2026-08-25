"""One-off (2026-08-25): quantify how much of the 08-25 catalog import is
the SAME PRODUCT the sheet already managed, live on OnBuy under a second
SKU. Imported rows are identified by their import fingerprint (no Supplier
URL + the 2026-08-25 sync stamp). Reports: exact-SKU duplicates inside the
sheet (should be none), whitespace/case near-miss SKU pairs, and
title-identical pairs between imported and pre-existing rows. Read-only."""
import json
import os
import re
from collections import defaultdict

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
IMPORT_DAY = os.getenv("IMPORT_DAY") or "2026-08-25"


def norm_sku(s):
    return re.sub(r"\s+", "", str(s or "")).casefold()


def norm_title(t):
    return re.sub(r"\s+", " ", str(t or "").strip()).casefold()


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    rows = with_retry(lambda: sheet.get_all_records(), what="sheet read", max_attempts=3)

    imported, pre = [], []
    for i, r in enumerate(rows):
        sku = str(r.get("SKU") or "").strip()
        if not sku:
            continue
        url = str(r.get("Supplier URL") or "").strip()
        stamp = str(r.get("Last OnBuy Sync") or "").strip()
        rec = (i + 2, sku, str(r.get("Title") or "").strip())
        if not url and stamp.startswith(IMPORT_DAY) and str(r.get("Sync Status") or "").strip() == "Synced":
            imported.append(rec)
        else:
            pre.append(rec)
    print(f"rows: {len(rows)} | pre-existing: {len(pre)} | imported (08-25 fingerprint): {len(imported)}")

    seen = defaultdict(list)
    for rn, sku, _ in pre + imported:
        seen[sku].append(rn)
    exact_dups = {s: rns for s, rns in seen.items() if len(rns) > 1}
    print(f"exact same-SKU duplicate rows in sheet: {len(exact_dups)}")
    for s, rns in list(exact_dups.items())[:10]:
        print(f"  DUP {s}: rows {rns}")

    pre_by_nsku = defaultdict(list)
    for rn, sku, _ in pre:
        pre_by_nsku[norm_sku(sku)].append((rn, sku))
    near = []
    for rn, sku, _ in imported:
        for prn, psku in pre_by_nsku.get(norm_sku(sku), []):
            if psku != sku:
                near.append((prn, psku, rn, sku))
    print(f"whitespace/case near-miss SKU pairs (pre vs imported): {len(near)}")
    for prn, psku, rn, sku in near[:10]:
        print(f"  NEAR pre row {prn} {psku!r} <> imported row {rn} {sku!r}")

    pre_by_title = defaultdict(list)
    for rn, sku, t in pre:
        if t:
            pre_by_title[norm_title(t)].append((rn, sku))
    same_title = []
    for rn, sku, t in imported:
        hits = pre_by_title.get(norm_title(t)) if t else None
        if hits:
            same_title.append((hits[0][0], hits[0][1], rn, sku, t))
    print(f"imported rows whose Title matches a pre-existing row: {len(same_title)} "
          f"({len(imported) - len(same_title)} imported rows are new-to-sheet products)")
    for prn, psku, rn, sku, t in same_title[:12]:
        print(f"  SAME-PRODUCT pre row {prn} SKU {psku} <> imported row {rn} SKU {sku} | {t[:60]}")


if __name__ == "__main__":
    main()
