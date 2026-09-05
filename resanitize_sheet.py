"""One-off: run the CURRENT sanitizer over every stored description in the
sheet and write back the ones it changes.

The sanitizer runs when a row is fetched from eBay, so a row filled before
a rule existed keeps the junk it was filled with until its next refetch -
and the sheet is what the team reads. A sweep on 2026-09-05 found 1,772 of
5,465 GTV descriptions still carrying store menus, prices, returns policy
or copyright footers. This brings the stored text up to the current rules
in one pass; repair_descriptions.py then re-pushes the clean text to the
products OnBuy already holds.

Safety: rows are addressed by number, and rows move when the team edits,
so the SKU column is read again immediately before the write and any row
whose SKU is no longer where it was is dropped from the batch (the same
rule the sync uses). A description the sanitizer would empty entirely is
REPORTED, never written - that is a row for a human to look at, not a cell
to blank. DRY_RUN=1 (default) reports only; MAX_ROWS caps a pass.
"""
import json
import os
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from sanitize import sanitize_description

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
MAX_ROWS = int(os.getenv("MAX_ROWS") or "0")          # 0 = no cap
MIN_KEEP = 20                                          # chars; below this we call it "emptied"


def col_letter(n):
    s = ""
    while n >= 0:
        s = chr(n % 26 + 65) + s
        n = n // 26 - 1
    return s


def norm(html):
    return re.sub(r"\s+", " ", str(html or "")).strip()


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    sheet = gspread.authorize(creds).open(SHEET_NAME).sheet1
    values = sheet.get_all_values()
    headers = [h.strip() for h in values[0]]
    col = {h: i for i, h in enumerate(headers)}
    if "Description" not in col or "SKU" not in col:
        raise SystemExit("sheet needs SKU and Description columns")
    i_desc, i_sku = col["Description"], col["SKU"]

    changed, emptied, untouched = [], [], 0
    for r in range(2, len(values) + 1):
        row = values[r - 1]
        desc = row[i_desc] if i_desc < len(row) else ""
        if not desc.strip():
            continue
        clean = sanitize_description(desc)
        if norm(clean) == norm(desc):
            untouched += 1
            continue
        sku = (row[i_sku] if i_sku < len(row) else "").strip()
        if len(re.sub(r"<[^>]+>", "", clean).strip()) < MIN_KEEP:
            emptied.append((r, sku, len(desc)))
            continue
        changed.append((r, sku, desc, clean))

    print(f"descriptions: {untouched + len(changed) + len(emptied)} filled | "
          f"already clean: {untouched} | would change: {len(changed)} | "
          f"would empty (NOT written, review): {len(emptied)}")
    for r, sku, old, new in changed[:12]:
        print(f"   row {r} {sku}: {len(old)} -> {len(new)} chars")
    for r, sku, n in emptied[:20]:
        print(f"   EMPTIED row {r} {sku}: {n} chars of pure seller junk - leave for a human")
    if MAX_ROWS:
        changed = changed[:MAX_ROWS]
        print(f"capped to {len(changed)} row(s) this pass")
    if DRY_RUN or not changed:
        print("DRY RUN - nothing written" if DRY_RUN else "nothing to write")
        return

    # Re-anchor: the sheet may have moved under us while we computed.
    fresh = sheet.col_values(i_sku + 1)
    updates, dropped = [], 0
    for r, sku, _old, new in changed:
        now = (fresh[r - 1] if r - 1 < len(fresh) else "").strip()
        if now != sku:
            dropped += 1
            continue
        updates.append({"range": f"{col_letter(i_desc)}{r}", "values": [[new]]})
    if dropped:
        print(f"rows moved since read - dropped {dropped} write(s) rather than risk the wrong row")
    for i in range(0, len(updates), 200):
        chunk = [dict(u) for u in updates[i:i + 200]]   # batch_update mutates its input
        sheet.batch_update(chunk, value_input_option="RAW")
    print(f"WROTE {len(updates)} cleaned description(s) to the sheet")


if __name__ == "__main__":
    main()
