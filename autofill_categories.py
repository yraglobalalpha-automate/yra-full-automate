"""One-off: apply HAND-CURATED categories for rows the strict matcher
refused. The relaxed auto-scorer tried first and produced DisplayPort-
grade mistakes (Smart TV -> TV Smart Glasses), so each entry below was
chosen by a human eye against the official category file (2026-08-01).
Rows not in the map stay on the employee worklist. DRY_RUN honoured.
"""
import json
import logging
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("onbuy_sync")

SHEET_NAME = "YRA_Full_Feed_Master"
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")

CURATED = {
    "995589894440": "Electronics & Technology > TV & Audio > Speakers & Sound Systems > Radios",
    "996860074254": "Musical Instruments & DJ > String Instruments & Accessories > Guitar Accessories > Guitar Amplifiers",
    "998866897639": "Musical Instruments & DJ > DJ Equipment > DJ Accessories > DJ Lights",
}


def main():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]), scope)
    sheet = gspread.authorize(creds).open(SHEET_NAME).sheet1
    data = sheet.get_all_records()
    headers = [str(h).strip() for h in sheet.row_values(1)]
    col_map = {col: idx + 1 for idx, col in enumerate(headers) if col}

    def col_letter(n):
        out = ""
        while n:
            n, rem = divmod(n - 1, 26)
            out = chr(65 + rem) + out
        return out

    updates, applied = [], []
    for idx, row in enumerate(data, start=2):
        sku = str(row.get("SKU") or "").strip()
        if sku in CURATED and "no matching OnBuy category" in str(row.get("Sync Status") or ""):
            path = CURATED[sku]
            applied.append((idx, sku, path))
            updates.append({"range": f"{col_letter(col_map['Category'])}{idx}", "values": [[path]]})
            updates.append({"range": f"{col_letter(col_map['Sync Status'])}{idx}", "values": [[""]]})
    for idx, sku, path in applied:
        logger.info("row %d %s -> %s", idx, sku, path)
    logger.info("curated categories to apply: %d", len(applied))
    if DRY_RUN:
        logger.info("DRY RUN - nothing written")
        return
    if updates:
        sheet.batch_update(updates)
        logger.info("Written - these rows retry on the next scheduled run")


if __name__ == "__main__":
    main()
