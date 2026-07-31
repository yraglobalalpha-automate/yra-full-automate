"""One-off: unstick the "section B" products - submitted in July but
OnBuy has no record of receiving them (no queue entry ever found, OPC
stuck on PENDING, Sync Status never resolved). Clearing their OnBuy
tracking fields makes the pipeline treat them as never-submitted, so the
next run re-creates them (now with the listing attached) while the
client's full request/response logging captures the payload evidence
OnBuy support asked for.

DRY_RUN=1 (default): list the matching rows, change nothing.
Scope guard: only rows whose OPC is literally PENDING (a create was
accepted into the queue once) with a Pending Approval or Failed status -
never rows with a real OPC.
"""
import json
import logging
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import supabase_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("onbuy_sync")

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")


def main():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]), scope)
    book = gspread.authorize(creds).open("YRA_Full_Feed_Master")
    sheet = book.sheet1
    data = sheet.get_all_records()
    headers = [str(h).strip() for h in sheet.row_values(1)]
    col_map = {col: idx + 1 for idx, col in enumerate(headers) if col}

    def col_letter(n):
        out = ""
        while n:
            n, rem = divmod(n - 1, 26)
            out = chr(65 + rem) + out
        return out

    stuck = []
    for idx, row in enumerate(data, start=2):
        sku = str(row.get("SKU") or "").strip()
        opc = str(row.get("OPC") or "").strip().upper()
        status = str(row.get("Sync Status") or "").strip()
        # "Awaiting OnBuy go-live" included: the restored runs re-tried these
        # rows, got "SKU does not exist", and the anti-duplicate deferral
        # relabeled them - still the same never-received products.
        if sku and opc == "PENDING" and status.startswith(("Pending Approval", "Failed", "Awaiting OnBuy go-live")):
            stuck.append((idx, sku, status[:60]))

    logger.info("Section B candidates (OPC=PENDING, unresolved status): %d", len(stuck))
    for idx, sku, status in stuck:
        logger.info("  row %d: SKU %s (%s)", idx, sku, status)

    if DRY_RUN:
        logger.info("DRY RUN - nothing changed. Re-run with dry_run=no to reset %d row(s).", len(stuck))
        return
    if not stuck:
        logger.info("Nothing to reset")
        return

    updates = []
    for idx, sku, _ in stuck:
        for col in ("Sync Status", "OPC", "Last OnBuy Sync", "OnBuy Product Created", "OnBuy Listing Active"):
            if col in col_map:
                updates.append({"range": f"{col_letter(col_map[col])}{idx}", "values": [[""]]})
    sheet.batch_update(updates)
    logger.info("Sheet: cleared OnBuy tracking on %d row(s)", len(stuck))

    skus = [sku for _, sku, _ in stuck]
    full = supabase_db.fetch_full_rows(skus)
    merged = []
    for sku in skus:
        row = full.get(sku)
        if not row:
            continue
        row = dict(row)
        for col in ("Sync Status", "OPC", "Last OnBuy Sync", "OnBuy Product Created", "OnBuy Listing Active"):
            if col in row:
                row[col] = None
        merged.append(row)
    if merged:
        supabase_db.upsert_products(merged)
        logger.info("Supabase: cleared OnBuy tracking on %d row(s)", len(merged))
    logger.info("DONE - the next run re-submits these products; its log carries the payload evidence")


if __name__ == "__main__":
    main()
