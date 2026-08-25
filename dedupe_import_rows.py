"""One-off (2026-08-25): the catalog import's sheet-SKU snapshot used
col_values (FORMATTED display strings), so number-formatted SKU cells (e.g.
thousands separators) didn't match their plain live SKUs and those listings
were appended again - import-created same-SKU duplicate rows (YRA 344,
Makstore 1). Delete ONLY the import-created copy: the row must (a) sit in
the appended import block (row >= BLOCK_START), (b) carry the import
fingerprint (no Supplier URL, Sync Status "Synced"), and (c) duplicate the
SKU of a row BELOW BLOCK_START (the team's original - always kept).
Prints formatted-vs-raw evidence for the first pairs so the mechanism is on
record. deleteDimension applies against live state - deletions run in
descending row order. DRY_RUN default on."""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from retry_utils import with_retry

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
BLOCK_START = int(os.environ["BLOCK_START"])  # first sheet row number of the appended import block


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    rows = with_retry(lambda: sheet.get_all_records(), what="sheet read", max_attempts=3)
    headers = with_retry(lambda: sheet.row_values(1), what="headers", max_attempts=3)
    sku_col_idx = {h.strip(): i for i, h in enumerate(headers)}["SKU"] + 1
    formatted = with_retry(lambda: sheet.col_values(sku_col_idx), what="sku col formatted", max_attempts=3)

    pre_skus = {}
    for i, r in enumerate(rows):
        rownum = i + 2
        if rownum >= BLOCK_START:
            break
        sku = str(r.get("SKU") or "").strip()
        if sku and sku not in pre_skus:
            pre_skus[sku] = rownum
    print(f"rows: {len(rows)} | block starts at row {BLOCK_START} | pre-block SKUs: {len(pre_skus)}")

    to_delete, shown = [], 0
    for i, r in enumerate(rows):
        rownum = i + 2
        if rownum < BLOCK_START:
            continue
        sku = str(r.get("SKU") or "").strip()
        if not sku or sku not in pre_skus:
            continue
        if str(r.get("Supplier URL") or "").strip() or str(r.get("Sync Status") or "").strip() != "Synced":
            print(f"  SKIP row {rownum} SKU {sku}: in block but not import-shaped - refusing")
            continue
        prow = pre_skus[sku]
        to_delete.append(rownum)
        if shown < 8:
            pf = formatted[prow - 1] if prow - 1 < len(formatted) else "?"
            bf = formatted[rownum - 1] if rownum - 1 < len(formatted) else "?"
            print(f"  DUP {sku}: keep pre row {prow} (displayed {pf!r}) | delete imported row {rownum} (displayed {bf!r})")
            shown += 1
    print(f"import-created duplicate rows to delete: {len(to_delete)}")
    if DRY_RUN:
        print("DRY RUN - nothing deleted")
        return
    if not to_delete:
        print("nothing to delete")
        return
    requests = [{"deleteDimension": {"range": {
        "sheetId": sheet.id, "dimension": "ROWS",
        "startIndex": rn - 1, "endIndex": rn}}} for rn in sorted(to_delete, reverse=True)]
    for i in range(0, len(requests), 400):
        chunk = requests[i:i + 400]
        with_retry(lambda c=chunk: sheet.spreadsheet.batch_update({"requests": c}), what=f"delete batch {i}", max_attempts=3)
        print(f"deleted {min(i + 400, len(requests))}/{len(requests)}")
    print(f"deleted {len(to_delete)} import-created duplicate row(s); originals untouched")


if __name__ == "__main__":
    main()
