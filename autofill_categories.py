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
    # 2026-08-06 batch - the monitor-heavy backlog refusing since 2026-07-31,
    # diagnosed with diagnose_categories.py: no eBay Type set, no leaf named
    # in any title. LG / iiyama / electriQ / Dell / AOC monitors:
    "176431198422": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "177769370627": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "195060051922": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "251795961483": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "289259524585": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "334736263331": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "389238151556": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "410024645895": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "603463946627": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "603642177606": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "621064148424": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "655784881958": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "662599982725": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "682289971402": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "730849956106": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "745817010032": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "764628858909": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "778154519739": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "783014990405": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "843534195824": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    "900074836188": "Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors",
    # Lexar NM100 internal SATA SSD:
    "145817446914": "Electronics & Technology > Computing & Gaming > Computer Components > Internal Solid State Drives",
    # Lenovo FHD webcam:
    "327293750246": "Electronics & Technology > Computing & Gaming > Computing Peripherals > Computer Webcams",
    # BNC male coupler (coax connector):
    "445736877470": "Electronics & Technology > Cables & Adapters > Adapters > Coaxial Cable Connectors",
    # Lenovo ThinkServer 3.5" SATA HDD:
    "914337862685": "Electronics & Technology > Computing & Gaming > Computer Components > Internal Hard Drives",
    # Dell WM126 wireless mouse - the scorer would have WRONGLY matched
    # this to Office Wireless Presentations Supplies from description
    # noise; a curated valid category preempts the matcher entirely:
    "760815897973": "Electronics & Technology > Computing & Gaming > Keyboards, Mice & Input Devices > Computer Mice",
    "995589894440": "Electronics & Technology > TV & Audio > Speakers & Sound Systems > Radios",
    "996860074254": "Musical Instruments & DJ > String Instruments & Accessories > Guitar Accessories > Guitar Amplifiers",
    "998866897639": "Musical Instruments & DJ > DJ Equipment > DJ Accessories > DJ Lights",
}

# SKUs whose curated value must overwrite whatever Category is currently in
# the row, regardless of Sync Status. ONLY for repairing a wrong category
# that AUTOMATION itself wrote - never list a SKU here to override a human.
# 760815897973: a run's title scorer filed the Dell WM126 wireless mouse
# under Office Wireless Presentations Supplies (description noise) and that
# valid-but-wrong path stuck; this forces it back to Computer Mice.
FORCE_RECATEGORIZE = {"760815897973"}


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
        if sku not in CURATED:
            continue
        refused = "no matching OnBuy category" in str(row.get("Sync Status") or "")
        # A mapped SKU whose Category cell is empty is fillable regardless
        # of what Sync Status says - a later run may have overwritten the
        # refusal text, and writing into a blank cell can't clobber a
        # human's choice (2026-08-06: all 26 blank-category rows here
        # matched the map but not the status gate, so nothing applied).
        blank = not str(row.get("Category") or "").strip()
        force = sku in FORCE_RECATEGORIZE and str(row.get("Category") or "").strip() != CURATED[sku]
        if refused or blank or force:
            path = CURATED[sku]
            applied.append((idx, sku, path + (" [FORCED]" if force and not (refused or blank) else "")))
            updates.append({"range": f"{col_letter(col_map['Category'])}{idx}", "values": [[path]]})
            updates.append({"range": f"{col_letter(col_map['Sync Status'])}{idx}", "values": [[""]]})
        elif DRY_RUN:
            # Explain no-ops so a zero-applied dry run is diagnosable
            # instead of a mystery (2026-08-06).
            logger.info("skip row %d %s: Sync Status=%r Category=%r",
                        idx, sku,
                        str(row.get("Sync Status") or "")[:70],
                        str(row.get("Category") or "")[:70])
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
