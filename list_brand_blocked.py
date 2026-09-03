"""READ-ONLY (2026-09-03): list every row currently skipped as brand
blocked, so the stale ones can be cleared.

A brand block used to be permanent: the skip was decided from Sync
Status, which is read from the Supabase mirror first, and a blocked row
returns before the mirror is rewritten - so OnBuy's original refusal
stayed there and the row was never pushed again to test it. Ten Arden
rows spent weeks skipped on brands the platform was accepting on
neighbouring rows the whole time. brand_block_state() now expires a
block after BRAND_BLOCK_RETRY_DAYS, but a flag already on the sheet is
re-dated to today first, so the existing backlog would wait out the full
window. This lists that backlog; feed the SKUs to reset_deleted_rows to
have them re-tested now.

Touches nothing - sheet read only, no OnBuy calls.
"""
import json
import os
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
FLAG_RE = re.compile(r"BRAND BLOCKED(?: \((\d{4}-\d{2}-\d{2})\))? - OnBuy says the brand '([^']*)'", re.I)
PHRASE = "supplied brand is owned by another seller"


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = with_retry(lambda: client.open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    values = with_retry(lambda: sheet.get_all_values(), what="sheet read", max_attempts=3)
    headers = values[0]
    idx = {h.strip().lower(): i for i, h in enumerate(headers)}
    i_sku, i_status = idx.get("sku"), idx.get("sync status")
    i_brand, i_url = idx.get("brand"), idx.get("supplier url")
    i_opc = idx.get("opc")
    if i_sku is None or i_status is None:
        raise SystemExit("sheet has no SKU / Sync Status column")

    def cell(row, i):
        return (row[i] if i is not None and i < len(row) else "").strip()

    hits = []
    for r in range(2, len(values) + 1):
        row = values[r - 1]
        status = cell(row, i_status)
        m = FLAG_RE.search(status)
        if not m and PHRASE not in status:
            continue
        hits.append({
            "row": r,
            "sku": cell(row, i_sku),
            "refused": (m.group(2).strip() if m else ""),
            "stamped": (m.group(1) if m and m.group(1) else ""),
            "brand_cell": cell(row, i_brand),
            "opc": cell(row, i_opc),
            "has_link": bool(cell(row, i_url)),
            "shape": "dated flag" if (m and m.group(1)) else ("flag" if m else "OnBuy wording"),
        })

    print(f"sheet rows: {len(values) - 1}")
    print(f"brand-blocked rows: {len(hits)}")
    print("")
    for h in hits:
        print(f"  row {h['row']:>5} | SKU {h['sku']:<15} | refused {h['refused']!r:<18} "
              f"| Brand cell {h['brand_cell']!r:<18} | OPC {h['opc'] or '-':<8} | {h['shape']}"
              f"{'' if h['has_link'] else ' | NO SUPPLIER LINK'}")
    print("")
    undated = [h for h in hits if not h["stamped"]]
    print(f"of those, {len(undated)} carry no date, so they predate the expiry rule")
    print("")
    # Resetting clears the OPC too, which is only safe on a row that never
    # got a product created - otherwise the row would be re-created rather
    # than updated. Keep those separate for a human to look at.
    never_created = [h for h in hits if h["opc"].upper() in ("", "PENDING") and h["sku"]]
    has_opc = [h for h in hits if h["opc"].upper() not in ("", "PENDING") and h["sku"]]
    print(f"safe to reset (no OPC on record): {len(never_created)}")
    print(",".join(h["sku"] for h in never_created))
    print("")
    print(f"NEEDS REVIEW - already has an OPC, do not blind-reset: {len(has_opc)}")
    for h in has_opc:
        print(f"  row {h['row']} SKU {h['sku']} OPC {h['opc']}")


if __name__ == "__main__":
    main()
