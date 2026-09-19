"""Plan job of the server-side Bulk Delete Chain (2026-09-15).

deletable = LIST_FILE minus every SKU currently on the sheet (any tab -
a seeded/synced SKU is pardoned, the newest-instruction rule) minus
EXCLUDE_FILE (SKUs already attempted by earlier chunks). Splits the
result into 4 chunk files under chunks/ for the delete jobs.
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from retry_utils import with_retry

SHEET_NAME = "YRA_Full_Feed_Master"


def load(path):
    out = []
    if path and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def main():
    target = list(dict.fromkeys(load(os.environ["LIST_FILE"])))
    done = set(load(os.getenv("EXCLUDE_FILE") or ""))
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    sheet_skus = set()
    # PRODUCT tabs only (first/eBay + Amazon). System tabs carry a SKU
    # column too - BuyBox lists every CONTESTED live listing (its
    # candidates come from the API, not the sheet), so counting all
    # worksheets pardoned 543 contested ORPHANS on 2026-09-19 - the
    # exact false-positive class the uniqueness guard hit on 09-17.
    _tabs = [book.sheet1]
    try:
        _amz = book.worksheet("Amazon")
        if _amz.title != _tabs[0].title:
            _tabs.append(_amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    for tab in _tabs:
        headers = [str(x).strip() for x in tab.row_values(1)]
        if "SKU" in headers:
            for v in tab.col_values(headers.index("SKU") + 1)[1:]:
                v = str(v).replace(",", "").strip()
                if v:
                    sheet_skus.add(v)
    excluded = [s for s in target if s in sheet_skus]
    already = [s for s in target if s not in sheet_skus and s in done]
    deletable = [s for s in target if s not in sheet_skus and s not in done]
    print(f"list {len(target)} | EXCLUDED as sheet-synced {len(excluded)} | "
          f"already attempted {len(already)} | deletable now {len(deletable)}")
    os.makedirs("chunks", exist_ok=True)
    per = (len(deletable) + 3) // 4 if deletable else 1
    for i in range(4):
        with open(f"chunks/chunk_{i + 1:02d}.txt", "w", encoding="utf-8") as fh:
            fh.write("\n".join(deletable[i * per:(i + 1) * per]))
    with open("chunks/deletable.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(deletable))
    with open("chunks/excluded.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(excluded))
    print("chunks written")


if __name__ == "__main__":
    main()
