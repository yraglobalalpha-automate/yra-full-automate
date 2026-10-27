"""The Category DROPDOWN of the product tabs and the list behind it (2026-10-27).

The main tab's Category column carries a strict dropdown whose choices are the paths of the "OnBuy Categories" tab; the Amazon tab had none (YRA 333596702103, a table-top dishwasher, sat under
"Furniture > Tables" because the matcher took Keepa's last category name "Table Top" for furniture). This tool

  * sets the same rule - ONE_OF_RANGE over the categories tab, strict, with the dropdown arrow - on the whole Category column of the tabs named in SHEET_TAB (comma-separated; default Amazon),
    from row 2 to the end of the sheet so rows added later get it too;
  * with REFRESH_LIST on, first brings the "OnBuy Categories" tab up to date with onbuy_categories_only.csv - the list the sync itself uses (refresh_categories.py writes only the file, nothing wrote
    the tab, so it drifted: 127 listable paths missing, 113 retired ones still offered) - after copying the old list to a backup tab "OnBuy Categories backup <date>".

A category chosen by hand is a human decision: the sync never replaces it with a guess and resolves its Category ID from the path (generate_xml.resolve_manual_category). Only data validation and the
categories tab (+ its backup) are touched - no product cell, no OnBuy, no Keepa. DRY_RUN (default on) reports without writing.
Env: SHEET_NAME, SHEET_TAB, COLUMN (default Category), REFRESH_LIST, DRY_RUN, SOURCE_LAST_ROW (default 10000: blank cells of the source range are ignored, so the list may grow without the rule being touched).
"""
import csv
import json
import os
from datetime import datetime, timezone

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
SHEET_TABS = [t.strip() for t in (os.getenv("SHEET_TAB") or "Amazon").split(",") if t.strip()]
COLUMN = (os.getenv("COLUMN") or "Category").strip()
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
REFRESH_LIST = (os.getenv("REFRESH_LIST") or "").strip().lower() in ("1", "yes", "true")
CATEGORIES_TAB = "OnBuy Categories"
HERE = os.path.dirname(os.path.abspath(__file__))
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


# ---- the list --------------------------------------------------------------------------------------------------------------------------
def load_denylist(path=None):
    """Category ids OnBuy refuses although its API flags them listable (category_denylist.txt: one id per line, # comments) - as generate_xml.load_category_denylist reads it."""
    ids = set()
    try:
        with open(path or os.path.join(HERE, "category_denylist.txt"), encoding="utf-8") as fh:
            for line in fh:
                token = line.split("#", 1)[0].strip()
                if token:
                    ids.add(token)
    except FileNotFoundError:
        pass
    return ids


def live_paths(csv_path=None, denylist=None):
    """The listable category paths the sync itself uses, in the file's order (sorted by path), without duplicates and without the ids on the denylist."""
    denied = load_denylist() if denylist is None else denylist
    seen, out = set(), []
    with open(csv_path or os.path.join(HERE, "onbuy_categories_only.csv"), newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            path = (row.get("OnBuy Category Path") or "").strip()
            if path and str(row.get("Category ID") or "").strip() not in denied and path not in seen:
                seen.add(path)
                out.append(path)
    return out


def diff_lists(old, new):
    """(paths the new list adds, paths it drops), each sorted."""
    return sorted(set(new) - set(old)), sorted(set(old) - set(new))


def backup_title(existing_titles, today):
    """'OnBuy Categories backup 2026-10-27', or '... (2)' when that name is taken."""
    base = f"{CATEGORIES_TAB} backup {today}"
    if base not in existing_titles:
        return base
    n = 2
    while f"{base} ({n})" in existing_titles:
        n += 1
    return f"{base} ({n})"


def refresh_list(book, cats, today):
    """Bring the categories tab up to date with the file the sync uses; returns the list now on the tab (or the one it would get in a dry run)."""
    old = [v.strip() for v in cats.col_values(1) if v and v.strip()]
    new = live_paths()
    if len(new) < 1000:
        raise SystemExit(f"STOP: onbuy_categories_only.csv holds only {len(new)} paths - not a category file, the tab is left as it is")
    added, dropped = diff_lists(old, new)
    print(f"LIST tab '{CATEGORIES_TAB}': {len(old)} paths now | live list {len(new)} paths | adds {len(added)}, drops {len(dropped)}")
    for p in added[:8]:
        print(f"   + {p}")
    if len(added) > 8:
        print(f"   + ... and {len(added) - 8} more")
    for p in dropped[:8]:
        print(f"   - {p}")
    if len(dropped) > 8:
        print(f"   - ... and {len(dropped) - 8} more")
    if not added and not dropped and old == new:
        print("LIST already equals the live list - nothing to do")
        return old
    if DRY_RUN:
        print(f"DRY RUN - would copy the old list to a new tab '{backup_title({w.title for w in book.worksheets()}, today)}', then write the {len(new)} live paths into {CATEGORIES_TAB}!A1:A{len(new)}")
        return new
    title = backup_title({w.title for w in book.worksheets()}, today)
    backup = book.add_worksheet(title=title, rows=max(len(old) + 10, 100), cols=1)
    with_retry(lambda: backup.update(range_name=f"A1:A{len(old)}", values=[[p] for p in old], value_input_option="RAW"), what="write backup", max_attempts=3)
    saved = [v.strip() for v in backup.col_values(1) if v and v.strip()]
    if saved != old:
        raise SystemExit(f"STOP: the backup tab '{title}' does not read back as the old list ({len(saved)} of {len(old)} paths) - the categories tab was NOT touched")
    print(f"BACKUP '{title}': {len(saved)} paths, read back identical")
    if len(new) > cats.row_count:
        cats.add_rows(len(new) - cats.row_count + 10)
    with_retry(lambda: cats.update(range_name=f"A1:A{len(new)}", values=[[p] for p in new], value_input_option="RAW"), what="write categories", max_attempts=3)
    if cats.row_count > len(new):
        cats.batch_clear([f"A{len(new) + 1}:A{cats.row_count}"])
    now = [v.strip() for v in cats.col_values(1) if v and v.strip()]
    print("LIST REFRESHED AND VERIFIED" if now == new else f"LIST WRITTEN, BUT IT READS BACK AS {len(now)} PATHS, NOT {len(new)} - look at the tab (the old list is in '{title}')")
    return now


# ---- the dropdown ---------------------------------------------------------------------------------------------------------------------
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


def set_dropdown(book, tab, listed):
    """The dropdown on one tab's column; `listed` = the set of paths the list offers (for the report of cells outside it)."""
    ws = book.worksheet(tab)
    headers = [str(h).strip() for h in ws.row_values(1)]
    if COLUMN not in headers:
        print(f"STOP: tab {tab!r} has no {COLUMN!r} column (headers: {[h for h in headers if h]}) - skipped")
        return
    index = headers.index(COLUMN)
    letter = column_letter(index + 1)
    values = with_retry(lambda: ws.col_values(index + 1), what=f"read {tab} {COLUMN}", max_attempts=3)[1:]
    typed = [v.strip() for v in values if v and v.strip()]
    stray = [v for v in typed if v not in listed]
    last = ws.row_count
    print(f"TAB {tab!r} (sheetId {ws.id}, {last} rows) | column {COLUMN!r} = {letter}: {len(typed)} cells filled, {len(stray)} hold text that is not in the list (warning mark, never changed)")
    for sample in stray[:4]:
        print(f"   not in the list: {sample[:120]!r}")
    rows = [2, max(3, last // 2), last]
    before = read_rules(book, tab, letter, rows)
    for n in rows:
        print(f"   now {tab}!{letter}{n}: {describe(before.get(n))}")
    if all(describe(before.get(n)) == describe(dropdown_rule()) for n in rows):
        print(f"   ALREADY SET on {tab} - nothing to do")
        return
    if DRY_RUN:
        print(f"   DRY RUN - would set: {describe(dropdown_rule())} on {tab}!{letter}2 down to the end of the sheet")
        return
    with_retry(lambda: book.batch_update({"requests": [set_request(ws.id, index)]}), what="set dropdown", max_attempts=3)
    after = read_rules(book, tab, letter, rows)
    for n in rows:
        print(f"   after {tab}!{letter}{n}: {describe(after.get(n))}")
    ok = all(describe(after.get(n)) == describe(dropdown_rule()) for n in rows)
    print("   SET AND VERIFIED" if ok else "   SET, BUT THE READ-BACK DOES NOT SHOW IT ON EVERY CHECKED ROW - look at the sheet")


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]), ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    try:
        cats = book.worksheet(CATEGORIES_TAB)
        for tab in SHEET_TABS:
            book.worksheet(tab)
    except gspread.exceptions.WorksheetNotFound as exc:
        raise SystemExit(f"STOP: a worksheet is missing in {SHEET_NAME}: {exc}")
    paths = {p.strip() for p in cats.col_values(1) if p and p.strip()}
    if len(paths) < 100:
        raise SystemExit(f"STOP: the {CATEGORIES_TAB!r} tab holds only {len(paths)} paths - not a category list, nothing changed")
    today = datetime.now(timezone.utc).date().isoformat()
    print(f"sheet {SHEET_NAME} | dry run {'yes' if DRY_RUN else 'no'} | rule: {describe(dropdown_rule())}")
    listed = paths
    if REFRESH_LIST:
        listed = set(refresh_list(book, cats, today))
    else:
        live = set(live_paths())
        print(f"(list not refreshed: the tab holds {len(paths)} paths, the live file {len(live)}; {len(live - paths)} listable paths are missing from the tab, {len(paths - live)} on it are retired)")
    for tab in SHEET_TABS:
        set_dropdown(book, tab, listed)


if __name__ == "__main__":
    main()
