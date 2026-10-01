"""Worksheet lookup BY NAME, never by position.

Incident 2026-09-30/10-01 (Makstore): the "OnBuy Categories" tab was moved
ahead of "Sheet1". Everything that took `book.sheet1` - the FIRST tab by
POSITION - silently started reading the categories tab: the hourly
deletion reconciler concluded ~5,000 live listings had been deleted from
the sheet, zeroed their stock (inactive on the dashboard) and queued them
for deletion, while the eBay sync and Buy Box Defense crashed on missing
headers. Product tabs are therefore always looked up by NAME; a book whose
main tab was renamed falls back to the first non-Amazon tab whose header
row has both SKU and Supplier URL, and anything else is a loud error -
never a quiet read of whatever tab happens to be first.
"""
import gspread

PRODUCT_TAB = "Sheet1"
AMAZON_TAB = "Amazon"


def product_sheet(book, name=PRODUCT_TAB):
    """The main (eBay) product worksheet of `book`."""
    try:
        return book.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        pass
    for ws in book.worksheets():
        if ws.title == AMAZON_TAB:
            continue
        headers = [str(h).strip() for h in ws.row_values(1)]
        if "SKU" in headers and "Supplier URL" in headers:
            return ws
    raise RuntimeError(
        f"no product tab: the spreadsheet has no worksheet named {name!r} and no other "
        "non-Amazon tab with SKU + Supplier URL headers")
