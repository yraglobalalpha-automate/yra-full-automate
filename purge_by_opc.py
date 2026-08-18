"""One-off (2026-08-18): completely remove the under-£5 listings the user
exported - from OnBuy, from Supabase, and from the sheet. The export's SKU
column is Excel-corrupted (trailing zeros), so the intact OPC column is the
authoritative key: resolve each OPC to its real SKU via the live listings
map, falling back to the sheet's own OPC column where listings are hidden
(suspended accounts). Order per product: OnBuy listing delete -> Supabase
row delete -> sheet row delete (descending row numbers - deleteDimension
applies sequentially against live state). DRY_RUN default on."""
import json
import os
import time

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import supabase_db
from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import RateLimitError, raise_for_status, with_retry

SHEET_NAME = "YRA_Full_Feed_Master"
OPCS = [s.strip().upper() for s in (os.getenv("PURGE_OPCS") or "").split(",") if s.strip()]
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
SKIP_ONBUY = (os.getenv("SKIP_ONBUY") or "").strip().lower() in ("1", "yes", "true")


def main():
    if not OPCS:
        raise SystemExit("PURGE_OPCS is empty - this tool never runs without an explicit list")
    print(f"OPCs to purge: {len(OPCS)}")

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    live = {}  # opc -> sku
    offset, limit = 0, 100
    while True:
        def _page(off=offset):
            r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                            params={"site_id": onbuy.site_id, "limit": limit, "offset": off},
                            timeout=60)
            r.raise_for_status()
            return r
        body = with_retry(_page, what=f"listings page {offset}", max_attempts=3).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            break
        for it in items:
            it = it or {}
            opc = str(it.get("opc") or "").strip().upper()
            sku = str(it.get("sku") or "").strip()
            if opc and sku:
                live[opc] = sku
        if len(items) < limit:
            break
        offset += limit
    print(f"live listings mapped: {len(live)}")

    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = gspread.authorize(creds).open(SHEET_NAME).sheet1
    rows = sheet.get_all_records()
    sheet_by_opc = {}
    for i, r in enumerate(rows):
        o = str(r.get("OPC") or "").strip().upper()
        if o:
            sheet_by_opc.setdefault(o, []).append((i + 2, str(r.get("SKU") or "").strip()))

    resolved, unresolved = {}, []
    for opc in OPCS:
        sku = live.get(opc) or (sheet_by_opc.get(opc, [(None, None)])[0][1])
        if sku:
            resolved[opc] = sku
        else:
            unresolved.append(opc)
    print(f"resolved {len(resolved)} OPC(s) to SKUs "
          f"({sum(1 for o in resolved if o in live)} via live listings, rest via sheet) "
          f"| unresolved: {len(unresolved)}")
    for o in unresolved[:10]:
        print(f"  UNRESOLVED {o}")

    skus = sorted(set(resolved.values()))
    doomed_rows = sorted({rn for i, r in enumerate(rows)
                          for rn in [i + 2]
                          if str(r.get("SKU") or "").strip() in set(skus)
                          or str(r.get("OPC") or "").strip().upper() in resolved},
                         reverse=True)
    print(f"plan: {len(skus)} OnBuy/Supabase SKU(s), {len(doomed_rows)} sheet row(s)")
    if DRY_RUN:
        print("DRY RUN - nothing deleted")
        return

    ob_deleted = ob_failed = 0
    if not SKIP_ONBUY:
        idx = 0
        while idx < len(skus):
            sku = skus[idx]
            try:
                def _do(sku=sku):
                    resp = onbuy._send("DELETE", f"{BASE_URL}/listings/by-sku",
                                       what=f"onbuy delete_listing({sku})",
                                       json={"site_id": onbuy.site_id, "skus": [sku]})
                    raise_for_status(resp, what=f"onbuy delete_listing({sku})")
                    return resp
                with_retry(_do, what=f"onbuy delete_listing({sku})", max_attempts=3)
                ob_deleted += 1
                idx += 1
            except RateLimitError:
                print(f"burst limit at {idx}/{len(skus)} - waiting 90s")
                time.sleep(90)
                continue
            except Exception as exc:
                ob_failed += 1
                print(f"ONBUY DELETE {sku} failed - {str(exc)[:120]}")
                idx += 1
            time.sleep(1.0)
        print(f"OnBuy: {ob_deleted} deleted, {ob_failed} failed")
    else:
        print("OnBuy deletion SKIPPED (SKIP_ONBUY set)")

    supabase_db.delete_products(skus)
    print(f"Supabase: delete issued for {len(skus)} SKU(s)")

    sheet_id = sheet._properties["sheetId"]
    for i in range(0, len(doomed_rows), 100):
        chunk = doomed_rows[i:i + 100]
        reqs = [{"deleteDimension": {"range": {
            "sheetId": sheet_id, "dimension": "ROWS",
            "startIndex": rn - 1, "endIndex": rn}}} for rn in chunk]
        with_retry(lambda rq=reqs: sheet.spreadsheet.batch_update({"requests": rq}),
                   what="sheet row deletion", max_attempts=3)
    print(f"Sheet: {len(doomed_rows)} row(s) deleted")


if __name__ == "__main__":
    main()
