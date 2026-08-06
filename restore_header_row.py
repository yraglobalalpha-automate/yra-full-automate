"""One-off: repair the Sheet's header row against sheet_headers.csv.

2026-08-06: cell A1 (the SKU header) turned up blank mid-morning - with no
"SKU" key, row.get("SKU") returned None for every row, so autofill matched
nothing and the next sync would have skipped every row as "no SKU
provided". Adapted from YRA-semi's restore_header_row.py (2026-07-29
incident, whole row deleted) with a second mode for this case:

- row 1 largely matches the canonical headers -> repair ONLY the cells
  that differ, in place (inserting a fresh row here would shove the
  near-intact header row down as a junk data row);
- row 1 is not a header row at all -> insert the canonical row above it.

DRY_RUN honoured (default on).
"""
import csv
import json
import logging
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("onbuy_sync")

SHEET_NAME = "YRA_Full_Feed_Master"
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")


def col_letter(n):
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def main():
    with open("sheet_headers.csv", newline="", encoding="utf-8-sig") as f:
        canonical = [h.strip() for h in next(csv.reader(f)) if h.strip()]
    logger.info("Canonical headers (%d): %s ...", len(canonical), ", ".join(canonical[:5]))

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]), scope)
    sheet = gspread.authorize(creds).open(SHEET_NAME).sheet1

    row1 = [str(c).strip() for c in sheet.row_values(1)]
    row1 += [""] * (len(canonical) - len(row1))
    overlap = sum(1 for a, b in zip(canonical, row1) if a == b)

    if overlap == len(canonical):
        logger.info("Row 1 already matches the canonical headers - nothing to do")
        return

    if overlap >= len(canonical) // 2:
        wrong = [(i + 1, canonical[i], row1[i])
                 for i in range(len(canonical)) if row1[i] != canonical[i]]
        for col, want, got in wrong:
            logger.info("cell %s1: %r -> %r", col_letter(col), got, want)
        if DRY_RUN:
            logger.info("DRY RUN - %d header cell(s) would be repaired, nothing written", len(wrong))
            return
        sheet.batch_update(
            [{"range": f"{col_letter(col)}1", "values": [[want]]} for col, want, _ in wrong])
        logger.info("Repaired %d header cell(s) in place", len(wrong))
    else:
        logger.info("Row 1 is NOT a header row (starts: %s...) - a fresh header row is needed", row1[:3])
        if DRY_RUN:
            logger.info("DRY RUN - would insert the canonical header row above row 1")
            return
        sheet.insert_row(canonical, 1)
        logger.info("Header row inserted above the old row 1")

    check = [str(c).strip() for c in sheet.row_values(1)]
    assert check[:len(canonical)] == canonical, "repair failed: " + str(check[:6])
    logger.info("Header row verified - syncs and autofill read normally again")


if __name__ == "__main__":
    main()
