"""READ-ONLY sheet inspector: prints what is ACTUALLY in a tab's cells.

Built 2026-09-11 while diagnosing rows whose four Amazon columns were
filled although their Supplier URL cell was empty. WebFetch cannot see
this Sheet (it fabricates - see CLAUDE.md), so any question about real
cell contents gets answered by running this and reading the output.
Writes nothing to the Sheet, Supabase or OnBuy; costs no Keepa tokens.

SHEET_TAB picks the worksheet ("" = first tab). The whole-tab summary
always prints; ROWS ("195-320" or "2-10,50") adds per-row detail lines.
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import keepa_client
from retry_utils import with_retry

SHEET_NAME = "YRA_Full_Feed_Master"
TAB = (os.getenv("SHEET_TAB") or "").strip()
ROWS = (os.getenv("ROWS") or "").strip()
# Shown per row, when the tab has the column. Supplier URL is also parsed
# for its ASIN and cross-checked against the ASIN column - a mismatch means
# the row's Keepa data was written from some OTHER row's product.
SHOW = ["SKU", "Supplier URL", "ASIN", "Amazon Seller", "Amazon Availability",
        "Keepa Updated", "Sync Status", "OnBuy Product Created", "Title"]


def ranges(nums):
    """[2,3,4,9] -> '2-4, 9'"""
    nums = sorted(nums)
    out = []
    while nums:
        a = b = nums.pop(0)
        while nums and nums[0] == b + 1:
            b = nums.pop(0)
        out.append(f"{a}-{b}" if b > a else f"{a}")
    return ", ".join(out) or "(none)"


def row_selector(spec):
    parts = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        parts.append((min(lo, hi), max(lo, hi)))
    return lambda n: any(lo <= n <= hi for lo, hi in parts)


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    tab = book.worksheet(TAB) if TAB else book.sheet1
    values = tab.get_all_values()
    headers = [str(h).strip() for h in values[0]] if values else []
    idx = {h: i for i, h in enumerate(headers) if h}

    def cell(r, col):
        i = idx.get(col)
        return str(r[i]).strip() if i is not None and i < len(r) else ""

    data = values[1:]
    print(f"Tab '{tab.title}': {len(data)} data rows, {len(headers)} header cells")

    with_url, without_url = [], []
    for k, r in enumerate(data):
        if not any(str(c).strip() for c in r):
            continue  # fully blank trailing rows aren't rows
        (with_url if cell(r, "Supplier URL") else without_url).append(k + 2)
    print(f"Rows WITH a Supplier URL     : {len(with_url):4d}  ->  {ranges(with_url)}")
    print(f"Rows WITHOUT (but not blank) : {len(without_url):4d}  ->  {ranges(without_url)}")

    # Formula view of the URL column: an =HYPERLINK(...) or a reference to
    # another sheet shows up here even when the displayed value is empty.
    if "Supplier URL" in idx and data:
        letter = gspread.utils.rowcol_to_a1(1, idx["Supplier URL"] + 1).rstrip("1")
        formulas = tab.get(f"{letter}2:{letter}{len(data) + 1}", value_render_option="FORMULA")
        odd = [k + 2 for k, v in enumerate(formulas) if str(v[0] if v else "").startswith("=")]
        print(f"URL cells holding a FORMULA  : {ranges(odd) if odd else '(none - all plain values)'}")

    if not ROWS:
        print("\n(no ROWS given - summary only)")
        return
    wanted = row_selector(ROWS)
    shown = [h for h in SHOW if h in idx]
    print(f"\nPer-row detail for {ROWS}  (columns: {', '.join(shown)})")
    for k, r in enumerate(data):
        n = k + 2
        if not wanted(n) or not any(str(c).strip() for c in r):
            continue
        parts = []
        for h in shown:
            v = cell(r, h)
            if h == "Supplier URL":
                link_asin = keepa_client.parse_asin(v)
                stored = cell(r, "ASIN")
                v = (v[:47] + "...") if len(v) > 50 else (v or "EMPTY")
                if link_asin and stored and link_asin != stored:
                    v += f" [link ASIN {link_asin} != ASIN column {stored} - MISALIGNED WRITE]"
            elif h == "Title":
                v = v[:30]
            parts.append(f"{h}={v or '-'}")
        print(f"row {n}: " + " | ".join(parts))
    print("\nDone - nothing was written.")


if __name__ == "__main__":
    main()
