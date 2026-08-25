"""Import every live OnBuy listing that has no sheet row (2026-08-25, user
request: the whole catalogue under the Buy Box system, not just sheet rows).

Pages GET /listings and appends a row for each SKU missing from the sheet:
SKU, Title (the listing's name), Selling Price (current listing price),
Stock (current listing stock), Sync Status "Synced", Last OnBuy Sync +
Last Checked Time stamped now. No Supplier URL is set, so the main loop
skips these rows (they cost no sync capacity) and the OOS/activation passes
leave them alone (their push state is stamped current). The Buy Box engine
already checks these listings live; once the team fills Cost Price (and
Shipping) on a row, the engine can compute its floor and defend it.

Appends use table_range="A1" in chunks (the bare-append column-drift gotcha,
2026-08-12). Protected SKUs are skipped. DRY_RUN default on."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
CHUNK = int(os.getenv("CHUNK") or "500")
PK_TZ = timezone(timedelta(hours=5))


def _load_protected():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "protected_skus.txt")
    out = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    out.add(line)
    return out


def page_listings(onbuy):
    out = {}
    offset, limit = 0, 100
    while True:
        def _page(off=offset):
            r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                            params={"site_id": onbuy.site_id, "limit": limit, "offset": off}, timeout=60)
            r.raise_for_status()
            return r
        body = with_retry(_page, what=f"listings page {offset}", max_attempts=4).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            break
        for it in items:
            it = it or {}
            sku = str(it.get("sku") or "").strip()
            if sku and sku not in out:
                out[sku] = (str(it.get("name") or "").strip(),
                            str(it.get("price") or "").strip(),
                            str(it.get("stock") or "").strip())
        if len(items) < limit:
            break
        offset += limit
        time.sleep(0.3)
    return out


def main():
    protected = _load_protected()
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    headers = with_retry(lambda: sheet.row_values(1), what="headers", max_attempts=3)
    col = {h.strip(): i for i, h in enumerate(headers)}
    for need in ("SKU", "Title", "Selling Price (£)", "Stock", "Sync Status"):
        if need not in col:
            raise SystemExit(f"missing column {need!r} - headers: {headers}")
    sheet_skus = {str(v).strip() for v in with_retry(
        lambda: sheet.col_values(col["SKU"] + 1), what="sku column", max_attempts=3)[1:] if str(v).strip()}
    print(f"sheet SKUs: {len(sheet_skus)}")

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    listings = page_listings(onbuy)
    missing = [s for s in listings if s not in sheet_skus and s not in protected]
    print(f"live listings: {len(listings)} | already in sheet: {len(listings) - len(missing) - len([s for s in listings if s in protected and s not in sheet_skus])} | to import: {len(missing)}")
    for s in missing[:8]:
        n, p, st = listings[s]
        print(f"  sample: {s} | {p} | stock {st} | {n[:70]}")
    if DRY_RUN:
        print("DRY RUN - nothing appended")
        return

    now = datetime.now(PK_TZ).strftime("%Y-%m-%d %H:%M:%S")
    width = len(headers)
    rows_out = []
    for s in sorted(missing):
        name, price, stock = listings[s]
        row = [""] * width
        row[col["SKU"]] = s
        row[col["Title"]] = name
        row[col["Selling Price (£)"]] = price
        row[col["Stock"]] = stock
        row[col["Sync Status"]] = "Synced"
        if "Last OnBuy Sync" in col:
            row[col["Last OnBuy Sync"]] = now
        if "Last Checked Time" in col:
            row[col["Last Checked Time"]] = now
        rows_out.append(row)
    appended = 0
    for i in range(0, len(rows_out), CHUNK):
        chunk = rows_out[i:i + CHUNK]
        with_retry(lambda c=chunk: sheet.append_rows(c, value_input_option="RAW", table_range="A1"),
                   what=f"append {i}", max_attempts=3)
        appended += len(chunk)
        print(f"appended {appended}/{len(rows_out)}")
        time.sleep(1.0)
    print(f"DONE: {appended} listing(s) imported as sheet rows")


if __name__ == "__main__":
    main()
