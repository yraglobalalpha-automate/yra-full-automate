"""One-off: repair rows whose stored category disagrees with a curated
TITLE PHRASE (2026-08-10 scan findings - e.g. dozens of Smart TVs filed
under Smart Watch accessories, Network Routers, Freesat Boxes by the
pre-phrase scorer). Sheet-driven for exact row targeting; writes ONLY the
Category cell - Sync Status and OnBuy state are left untouched on purpose,
so already-created rows keep their update path and nothing re-creates.
DRY_RUN default on."""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from generate_xml import title_phrase_category

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
SHEET_NAME = "YRA_Full_Feed_Master"


def col_letter(n):
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def main():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]), scope)
    sheet = gspread.authorize(creds).open(SHEET_NAME).sheet1
    headers = [str(h).strip() for h in sheet.row_values(1)]
    col_map = {col: idx + 1 for idx, col in enumerate(headers) if col}
    data = sheet.get_all_records()
    data = [{str(k).strip(): v for k, v in row.items()} for row in data]

    updates, planned = [], []
    for idx, row in enumerate(data, start=2):
        sku = str(row.get("SKU") or "").strip()
        title = str(row.get("Title") or "").strip()
        cat = str(row.get("Category") or "").strip()
        if not sku or not title or not cat:
            continue
        want = title_phrase_category(title)
        if want and want.strip().lower() != cat.lower():
            planned.append((idx, sku, title[:55], cat, want))
            updates.append({"range": f"{col_letter(col_map['Category'])}{idx}", "values": [[want]]})

    for idx, sku, title, cat, want in planned:
        print(f"row {idx} {sku} | {title}")
        print(f"    {cat}  ->  {want}")
    print(f"\nrepairs planned: {len(planned)}")
    if DRY_RUN:
        print("DRY RUN - nothing written")
        return
    if updates:
        sheet.batch_update(updates)
        print(f"Written {len(updates)} Category cell(s). Supabase mirrors on each row's next sweep.")


if __name__ == "__main__":
    main()
