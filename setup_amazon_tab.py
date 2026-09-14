"""One-off: create (or complete) the "Amazon" worksheet so the Keepa sync
can run - row 1 becomes the first tab's header plus the four Amazon
columns. Idempotent: re-running only fills what is missing, touches row 1
of the Amazon tab only, and never writes data rows or other tabs.
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from retry_utils import with_retry

SHEET_NAME = "YRA_Full_Feed_Master"
TAB = (os.getenv("SHEET_TAB") or "Amazon").strip()

AMAZON_EXTRA = ["ASIN", "Amazon Seller", "Amazon Availability", "Keepa Updated"]
# What generate_xml.py writes unconditionally (KeyError without them).
REQUIRED = ["SKU", "Supplier URL", "Title", "Status", "Last Checked Time", "Cost Price (£)",
            "Stock", "Selling Price (£)", "Description", "Image URL", "Additional Images",
            "Brand", "Last Updated", "Category"]


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    first = book.sheet1
    base = [str(h).strip() for h in first.row_values(1)]
    print(f"First tab '{first.title}': {len(base)} header cells (the template)")

    try:
        tab = book.worksheet(TAB)
        existing = [str(h).strip() for h in tab.row_values(1)]
        created = False
    except gspread.WorksheetNotFound:
        tab = book.add_worksheet(title=TAB, rows=2000,
                                 cols=max(len(base) + len(AMAZON_EXTRA) + 2, 40))
        existing = []
        created = True

    header = [h for h in existing if h] if any(existing) else list(base)
    for col in list(base) + AMAZON_EXTRA:
        if col and col not in header:
            header.append(col)
    if header != existing:
        tab.update(range_name="A1", values=[header])
        print(f"Tab '{TAB}': {'created' if created else 'existed'} - header written ({len(header)} cells)")
    else:
        print(f"Tab '{TAB}': already complete ({len(header)} cells) - nothing written")

    missing = [h for h in REQUIRED if h not in header]
    print("required, missing :", missing or "none - OK")
    print("Amazon extras     :", {h: (h in header) for h in AMAZON_EXTRA})
    dup = sorted({h for h in header if h and header.count(h) > 1})
    if dup:
        print("DUPLICATED headers:", dup)
    print("Done.")


if __name__ == "__main__":
    main()
