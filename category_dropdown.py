"""Put the Category DROPDOWN on a product tab (default: the Amazon tab) - the same list the main tab has, so a wrong category can be changed by hand from the cell (2026-10-27).

The main tab's Category column carries a strict dropdown whose choices are the paths of the "OnBuy Categories" tab; the Amazon tab had none (YRA 333596702103, a table-top dishwasher, sat under
"Furniture > Tables" because the matcher took Keepa's last category name "Table Top" for furniture). This sets the same rule - ONE_OF_RANGE over the categories tab, strict, with the dropdown arrow -
on the whole Category column of the tab (from row 2 to the end of the sheet, so rows added later get it too). A category chosen by hand is a human decision: the sync never replaces it with a guess
and resolves its Category ID from the path (generate_xml.resolve_manual_category).

Only data validation is touched - no cell value, no other tab, no OnBuy, no Keepa. DRY_RUN (default on) reports what the column has and what it would get. Env: SHEET_NAME, SHEET_TAB
(default Amazon), COLUMN (default Category), DRY_RUN, SOURCE_LAST_ROW (default 10000: blank cells of the source range are ignored, so the list may grow without the rule being touched).
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
SHEET_TAB = (os.getenv("SHEET_TAB") or "Amazon").strip()
COLUMN = (os.getenv("COLUMN") or "Category").strip()
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
CATEGORIES_TAB = "OnBuy Categories"
try:
    SOURCE_LAST_ROW = max(int(os.getenv("SOURCE_LAST_ROW") or "10000"), 100)
except ValueError:
    SOURCE_LAST_ROW = 10000


def column_letter(number):
    """1 -> A, 27 -> AA."""
    letters = ""
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def source_formula(last_row=SOURCE_LAST_ROW):
    return f"='{CATEGORIES_TAB}'!$A$1:$A${last_row}"


def dropdown_rule(last_row=SOURCE_LAST_ROW):
    """The rule the main tab has: a strict dropdown from a range."""
    return {"condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": source_formula(last_row)}]}, "strict": True, "showCustomUi": True}


def set_request(sheet_id, column_index, last_row=SOURCE_LAST_ROW):
    """The Sheets API request: the rule on the column from row 2 down, with no end row (so rows added later have it)."""
    return {"setDataValidation": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": column_index, "endColumnIndex": column_index + 1},
                                  "rule": dropdown_rule(last_row)}}


def describe(rule):
    if not rule:
        return "no dropdown"
    cond = rule.get("condition") or {}
    values = ", ".join(str(v.get("userEnteredValue")) for v in cond.get("values") or [])
    return f"{cond.get('type')} {values} (strict {rule.get('strict')}, dropdown arrow {rule.get('showCustomUi')})"


def read_rules(book, title, letter, rows):
    """{row number: the data validation rule of that cell of the column or None} for the given rows."""
    ranges = [f"'{title}'!{letter}{n}:{letter}{n}" for n in rows]
    meta = book.fetch_sheet_metadata(params={"includeGridData": "true", "ranges": ranges, "fields": "sheets(properties(title),data(startRow,rowData(values(dataValidation))))"})
    out = {}
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["title"] != title:
            continue
        for data in sheet.get("data", []):
            for i, row in enumerate(data.get("rowData", [])):
                cells = row.get("values", [])
                out[data.get("startRow", 0) + 1 + i] = (cells[0].get("dataValidation") if cells else None)
    return out


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]), ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    try:
        ws = book.worksheet(SHEET_TAB)
        cats = book.worksheet(CATEGORIES_TAB)
    except gspread.exceptions.WorksheetNotFound as exc:
        raise SystemExit(f"STOP: a worksheet is missing in {SHEET_NAME}: {exc}")
    headers = [str(h).strip() for h in ws.row_values(1)]
    if COLUMN not in headers:
        raise SystemExit(f"STOP: tab {SHEET_TAB!r} has no {COLUMN!r} column (headers: {[h for h in headers if h]})")
    index = headers.index(COLUMN)
    letter = column_letter(index + 1)
    paths = {p.strip() for p in cats.col_values(1) if p and p.strip()}
    if len(paths) < 100:
        raise SystemExit(f"STOP: the {CATEGORIES_TAB!r} tab holds only {len(paths)} paths - not a category list, nothing changed")
    values = with_retry(lambda: ws.col_values(index + 1), what=f"read {SHEET_TAB} {COLUMN}", max_attempts=3)[1:]
    typed = [v.strip() for v in values if v and v.strip()]
    stray = [v for v in typed if v not in paths]
    last = ws.row_count
    print(f"sheet {SHEET_NAME} | tab {SHEET_TAB!r} (sheetId {ws.id}, {last} rows) | column {COLUMN!r} = {letter}")
    print(f"choices: '{CATEGORIES_TAB}'!A1:A{cats.row_count} ({len(paths)} paths); the rule would read {source_formula()}")
    print(f"{COLUMN} cells filled: {len(typed)}; {len(stray)} of them hold text that is not in the list (they get a warning mark, they are not changed)")
    for sample in stray[:5]:
        print(f"   not in the list: {sample[:120]!r}")
    rows = [2, max(3, last // 2), last]
    before = read_rules(book, SHEET_TAB, letter, rows)
    for n in rows:
        print(f"now {SHEET_TAB}!{letter}{n}: {describe(before.get(n))}")
    if all(describe(before.get(n)) == describe(dropdown_rule()) for n in rows):
        print("ALREADY SET - the column has this dropdown; nothing to do")
        return
    if DRY_RUN:
        print(f"DRY RUN - would set: {describe(dropdown_rule())} on {SHEET_TAB}!{letter}2 down to the end of the sheet")
        return
    with_retry(lambda: book.batch_update({"requests": [set_request(ws.id, index)]}), what="set dropdown", max_attempts=3)
    after = read_rules(book, SHEET_TAB, letter, rows)
    for n in rows:
        print(f"after {SHEET_TAB}!{letter}{n}: {describe(after.get(n))}")
    ok = all(describe(after.get(n)) == describe(dropdown_rule()) for n in rows)
    print("SET AND VERIFIED" if ok else "SET, BUT THE READ-BACK DOES NOT SHOW IT ON EVERY CHECKED ROW - look at the sheet")


if __name__ == "__main__":
    main()
