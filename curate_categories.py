"""One-off: hand-curated category corrections from the 2026-08-10
correctness scan (tier-2 review). Every SKU below was checked by eye
against its title; writes ONLY the Category cell - Sync Status and
OnBuy state stay untouched. DRY_RUN default on."""

SHEET_NAME = 'YRA_Full_Feed_Master'

CORRECTIONS = {
    '186667190954': 'Jewellery & Watches > Watches > Watch Accessories > Watch Boxes',
    '786394023374': 'Home & Garden > Cooking, Dining & Barware > Food Storage > Food Storage Jars & Bottles',
    '766754231571': 'Electronics & Technology > TV & Audio > Speakers & Sound Systems > Speakers',
    '605048780190': 'Electronics & Technology > TV & Audio > Speakers & Sound Systems > Hi-Fi Systems',
    '776833826871': 'Electronics & Technology > TV & Audio > Speakers & Sound Systems > Hi-Fi Systems',
    '393021638131': 'Electronics & Technology > TV & Audio > Speakers & Sound Systems > Hi-Fi Systems',
    '465960679411': 'Electronics & Technology > TV & Audio > HiFi Seperates > Audio Amplifiers & Preamps',
    '976745098759': 'Electronics & Technology > TV & Audio > Speakers & Sound Systems > Radios',
    '971959390903': 'Electronics & Technology > TV & Audio > Speakers & Sound Systems > Radios',
    '992792799227': 'Musical Instruments & DJ > DJ Equipment > DJ Decks > DJ Mixers',
    '729429032392': 'Home & Garden > Kitchen & Home Appliances > Small Kitchen Appliances > Soup Makers',
    '443723050882': 'Electronics & Technology > Computing & Gaming > Computer Monitors & Monitor Accessories > Computer Monitors',
    '977326533805': 'Toys & Games > Toys > Educational Toys & Games > Sensory Toys',
    '253196494034': 'Sports & Outdoors > Cycling > Bike Lights & Reflectors > Bike Taillights',
}

import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")


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

    updates, planned = [], []
    for idx, row in enumerate(data, start=2):
        sku = str(row.get("SKU") or "").strip()
        want = CORRECTIONS.get(sku)
        if not want:
            continue
        cat = str(row.get("Category") or "").strip()
        if cat.lower() == want.lower():
            continue
        planned.append((idx, sku, cat, want))
        updates.append({"range": f"{col_letter(col_map['Category'])}{idx}", "values": [[want]]})

    for idx, sku, cat, want in planned:
        print(f"row {idx} {sku}")
        print(f"    {cat or '(blank)'}  ->  {want}")
    print(f"\ncorrections planned: {len(planned)}")
    if DRY_RUN:
        print("DRY RUN - nothing written")
        return
    if updates:
        sheet.batch_update(updates)
        print(f"Written {len(updates)} Category cell(s).")


if __name__ == "__main__":
    main()
