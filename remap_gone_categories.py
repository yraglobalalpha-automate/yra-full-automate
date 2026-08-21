"""One-off (2026-08-21): OnBuy's category tree changed - six category IDs our
rows used are no longer listable ("Category 'X' is not a lowest level
category" on create). Remap the affected rows to the new leaf under the same
parent, chosen from the title; write Category (path) + Category ID back to
the sheet; optionally flip Sync Status to a Failed text for SKUs we KNOW
failed creation (MARK_FAILED_SKUS), so the create fallback resubmits them
with the corrected category. DRY_RUN default on.

Rows already live on OnBuy keep working regardless (updates never send a
category) - this only matters for rows still to be created."""
import json
import os
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from retry_utils import with_retry

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
MARK_FAILED = {s.strip() for s in (os.getenv("MARK_FAILED_SKUS") or "").split(",") if s.strip()}
FAILED_TEXT = "Failed: Category is not a lowest level category (OnBuy tree change) - category remapped, resubmitting"

L = "Home & Garden > Furniture, Furnishings & Decor > Lighting"
O = "Home & Garden > Garden & Outdoor Living > Outdoor Lighting"
# Title rules checked FIRST for every retired id (order matters): the retired
# parents were catch-alls, so many rows are not really "lamps"/"ceiling lights".
GLOBAL_RULES = [
    (r"car (dash|bulb|instrument)|capless|w5w|\bt5\b|\bt10\b|h4 |h7 ", "10725", "Cars & Automotive > Car Parts > Car Lights, Bulbs & Indicators > Car Bulbs & LEDs"),
    (r"grow light|hydroponic|plant light|full spectrum", "14108", "Home & Garden > Garden & Outdoor Living > Hydroponics & Seed Starting > Grow Lights, Bulbs & Fixtures"),
    (r"\bbulbs?\b|gu10|\be27\b|\be14\b|\bb22\b|\bes\b|\bses\b|\bbc\b|filament", "38250", "Appliances > Furniture, Furnishings & Decor > Lighting > Light Bulbs"),
    (r"street light|flood ?light|security light|solar (power(ed)? )?(pir|motion|led|wall|garden|outdoor|street)|outdoor.*(solar|security|flood)|solar.*(outdoor|garden|security|flood)", "9658", f"{O} > Outdoor Security Lights, Floodlights & Spotlights"),
    (r"outdoor wall light|garden wall light|fence light|door light|step light|porch light", "9656", f"{O} > Outdoor Wall & Ceiling Lights"),
    (r"led strip|light strip|light bar|backlight|neon|rope light", "9670", f"{L} > LED Strips"),
    (r"night light|motion sensor (light|lamp)|pir (light|lamp|sensor)|cabinet light|closet|stair light|wardrobe light|plug-?in", "8036", f"{L} > Night Lights"),
    (r"nail (lamp|dryer)|uv led nail|gel polish", "10288", "Health & Beauty > Nail Care > Manicure Nail Tools > Manicure & Pedicure Sets"),
    (r"chandelier", "3485", f"{L} > Ceiling Lights > Chandeliers"),
    (r"pendant|hanging light|drop light", "3487", f"{L} > Ceiling Lights > Pendant Lights"),
    (r"spot ?light|downlight|track light", "3484", f"{L} > Ceiling Lights > Ceiling Spotlights"),
    (r"batten|tube light", "17970", f"{L} > Ceiling Lights > LED Batten Lights"),
    (r"ceiling fan", "17925", f"{L} > Ceiling Lights > Ceiling Fans"),
    (r"floor lamp|standing lamp|tripod lamp|arc lamp|corner (floor )?lamp|standing light", "9620", f"{L} > Lamps > Floor Lamps"),
    (r"wall lamp|wall light|sconce|wall mounted", "17974", f"{L} > Lamps > Wall Lamps"),
    (r"desk lamp|table lamp|bedside|reading lamp|reading light|magnif", "3475", f"{L} > Lamps > Desk & Table Lamps"),
    (r"ceiling (light|lamp)|flush mount|garage light|panel light|led panel|hexagon|hex led|work light|shop light", "38245", "Appliances > Furniture, Furnishings & Decor > Lighting > Ceiling Lights"),
    (r"parasol cover", "14000", "Home & Garden > Garden & Outdoor Living > Parasols, Gazebos & Garden Shade > Parasol Covers"),
]
# retired id -> (retired path, fallback (id, path)) - used when no title rule fires
RULES = {
    "3472": (f"{L} > Ceiling Lights", ("38245", "Appliances > Furniture, Furnishings & Decor > Lighting > Ceiling Lights")),
    "13705": (f"{L} > Lamps", ("3475", f"{L} > Lamps > Desk & Table Lamps")),
    "3463": (f"{L} > Light Bulbs", ("38250", "Appliances > Furniture, Furnishings & Decor > Lighting > Light Bulbs")),
    "25994": ("Electronics & Technology > TV & Audio > Speakers & Sound Systems > Soundbases",
              ("3238", "Electronics & Technology > TV & Audio > Speakers & Sound Systems > Soundbars")),
    "31571": ("Home & Garden > Garden & Outdoor Living > Parasols, Gazebos & Garden Shade > Parasol Parts & Accessories",
              ("18085", "Home & Garden > Garden & Outdoor Living > Parasols, Gazebos & Garden Shade > Parasol Parts")),
    "11345": ("Toys & Games > Hobby Toys & Games > Wargaming & Role-Playing Games > Historical Wargaming",
              ("11344", "Toys & Games > Hobby Toys & Games > Wargaming & Role-Playing Games > Wargaming Role Playing Games & Figures")),
}
GONE_PATHS = {v[0].lower(): k for k, v in RULES.items()}


def choose(gone_id, title):
    t = (title or "").lower()
    if gone_id in ("3472", "13705", "3463"):
        for rx, nid, npath in GLOBAL_RULES:
            if re.search(rx, t):
                return nid, npath
    elif gone_id == "31571" and "cover" in t:
        return "14000", "Home & Garden > Garden & Outdoor Living > Parasols, Gazebos & Garden Shade > Parasol Covers"
    return RULES[gone_id][1]


def col_letter(idx):
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    headers = with_retry(lambda: sheet.row_values(1), what="headers", max_attempts=3)
    col = {h.strip(): i for i, h in enumerate(headers)}
    for need in ("SKU", "Title", "Category", "Category ID", "Sync Status"):
        if need not in col:
            raise SystemExit(f"missing column {need!r} - headers: {headers}")
    rows = with_retry(lambda: sheet.get_all_records(), what="sheet read", max_attempts=3)
    updates, hits, marked = [], 0, 0
    for i, r in enumerate(rows):
        rownum = i + 2
        cid = str(r.get("Category ID") or "").strip()
        cpath = str(r.get("Category") or "").strip()
        gone = cid if cid in RULES else GONE_PATHS.get(cpath.lower())
        if not gone:
            continue
        sku = str(r.get("SKU") or "").strip()
        title = str(r.get("Title") or "")
        nid, npath = choose(gone, title)
        hits += 1
        status = str(r.get("Sync Status") or "").strip()
        mark = sku in MARK_FAILED
        print(f"row {rownum} SKU {sku} | {gone} -> {nid} ({npath.rsplit(' > ', 1)[-1]}) | {status[:28]!r}{' -> FAILED/resubmit' if mark else ''} | {title[:70]}")
        updates.append({"range": f"{col_letter(col['Category'])}{rownum}", "values": [[npath]]})
        updates.append({"range": f"{col_letter(col['Category ID'])}{rownum}", "values": [[nid]]})
        if mark:
            marked += 1
            updates.append({"range": f"{col_letter(col['Sync Status'])}{rownum}", "values": [[FAILED_TEXT]]})
    print(f"rows to remap: {hits} | of which flipped to Failed for resubmission: {marked}")
    if DRY_RUN:
        print("DRY RUN - nothing written")
        return
    for c0 in range(0, len(updates), 200):
        chunk = updates[c0:c0 + 200]
        with_retry(lambda b=chunk: sheet.batch_update([dict(u) for u in b]), what="remap write", max_attempts=3)
    print(f"written: {len(updates)} cell range(s)")


if __name__ == "__main__":
    main()
