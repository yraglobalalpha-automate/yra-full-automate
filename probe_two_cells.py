"""One-off diagnostic (2026-08-25): print exactly what the SKU column holds
at the given row numbers, in every read mode, plus current row totals -
resolves why displayed-text pairing found 0 leading-zero pairs right after
the dedupe dry-run printed them. Read-only."""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
ROWS = [int(x) for x in (os.getenv("ROWS") or "2829,7926").split(",") if x.strip()]


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    headers = with_retry(lambda: sheet.row_values(1), what="headers", max_attempts=3)
    idx = {h.strip(): i for i, h in enumerate(headers)}
    print(f"headers ({len(headers)}): SKU at col {idx.get('SKU')} | Supplier URL at col {idx.get('Supplier URL')}")
    dup_headers = len(headers) != len({h.strip() for h in headers if h.strip()})
    print(f"duplicate header names present: {dup_headers}")
    sku_col = idx["SKU"] + 1
    disp = with_retry(lambda: sheet.col_values(sku_col), what="col formatted", max_attempts=3)
    records = with_retry(lambda: sheet.get_all_records(), what="records", max_attempts=3)
    print(f"col_values length: {len(disp)} | get_all_records rows: {len(records)}")
    col_letter = chr(65 + (sku_col - 1) % 26) if sku_col <= 26 else None
    for rn in ROWS:
        d = disp[rn - 1] if rn - 1 < len(disp) else "<beyond column>"
        rec = records[rn - 2] if 0 <= rn - 2 < len(records) else {}
        r = rec.get("SKU", "<beyond records>")
        u = str(rec.get("Supplier URL") or "").strip()
        s = str(rec.get("Sync Status") or "").strip()
        raw = "?"
        if col_letter:
            try:
                got = with_retry(lambda a=f"{col_letter}{rn}": sheet.get(a, value_render_option="UNFORMATTED_VALUE"),
                                 what="raw cell", max_attempts=3)
                raw = got[0][0] if got and got[0] else "<empty>"
            except Exception as exc:
                raw = f"<err {exc}>"
        print(f"row {rn}: display={d!r} | records={r!r} ({type(r).__name__}) | raw={raw!r} ({type(raw).__name__}) | url={'yes' if u else 'no'} | status={s[:40]!r}")


if __name__ == "__main__":
    main()
