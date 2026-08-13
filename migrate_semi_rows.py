"""One-off: migrate the YRA semi sheet's products into the Full-auto
sheet (2026-08-13, same proven pattern as Arden and GTV).
For every semi row whose SKU has a LIVE listing on the YRA OnBuy account, append a Full-sheet row carrying SKU + Supplier URL +
Category (kept when valid) + the OPC harvested straight from the account's
own listings - the OPC is what locks the pipeline onto the UPDATE path so
nothing is ever re-created. Rows without a live listing are deferred to
phase 2 (post-incident); rows whose SKU already exists in the Full sheet
are skipped. The semi sheet is NOT modified - its cleanup is a separate,
later step after verification. DRY_RUN default on."""
import csv
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry

FULL_SHEET = "YRA_Full_Feed_Master"
SEMI_SHEET = "YRA_Feed_Master"
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
LIMIT = int(os.getenv("MIGRATE_LIMIT") or "0")  # 0 = no cap
INCLUDE_SUSPENDED = (os.getenv("INCLUDE_SUSPENDED") or "").strip().lower() in ("1", "yes", "true")


def live_opcs():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    out = {}
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
        if not isinstance(items, list):
            break
        for it in items:
            it = it or {}
            sku = str(it.get("sku") or "").strip()
            opc = str(it.get("opc") or "").strip()
            if sku and opc:
                out[sku] = opc
        if len(items) < limit:
            break
        offset += limit
    return out


def main():
    with open("onbuy_categories_only.csv", newline="", encoding="utf-8") as f:
        valid_cats = {r["OnBuy Category Path"].strip().lower()
                      for r in csv.DictReader(f) if r.get("OnBuy Category Path")}

    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    client = gspread.authorize(creds)

    full = client.open(FULL_SHEET).sheet1
    try:
        semi = client.open(SEMI_SHEET).sheet1
    except gspread.SpreadsheetNotFound:
        raise SystemExit(
            f"Cannot open '{SEMI_SHEET}' - share that sheet (Editor) with "
            f"{creds_dict.get('client_email')} and re-run.")

    full_headers = [str(h).strip() for h in full.row_values(1)]
    full_skus = {str(r.get("SKU") or "").strip() for r in full.get_all_records()}
    semi_rows = semi.get_all_records()
    # Same key hygiene the pipelines use: a stray space in a semi header
    # ("SKU ") makes row.get("SKU") return None on every row otherwise.
    semi_rows = [{str(k).strip(): v for k, v in row.items()} for row in semi_rows]
    print(f"semi rows: {len(semi_rows)} | full sheet already has {len(full_skus)} SKUs")

    opcs = live_opcs()
    print(f"live listings with OPC on the account: {len(opcs)}")

    migrate, deferred, skipped_dupe, skipped_nourl = [], 0, 0, 0
    for row in semi_rows:
        sku = str(row.get("SKU") or "").strip()
        url = str(row.get("Supplier URL") or "").strip()
        if not sku or "ebay." not in url.lower():
            skipped_nourl += 1
            if skipped_nourl <= 5:
                print(f"  sample non-eBay row: {sku} url={url[:70]!r}")
            continue
        if sku in full_skus:
            skipped_dupe += 1
            continue
        opc = opcs.get(sku)
        if not opc:
            # Suspended listings are invisible to GET /v2/listings, so no OPC
            # is harvestable for them. With INCLUDE_SUSPENDED they migrate
            # with a blank OPC anyway: Sync Status "Synced" alone keeps the
            # pipeline on the by-SKU UPDATE path (never the create fallback),
            # and the suspended-locked guard in generate_xml.py bounces a
            # still-suspended listing back to rotation instead of failing it.
            if not INCLUDE_SUSPENDED:
                deferred += 1
                continue
            opc = ""
        cat = str(row.get("Category") or "").strip()
        if cat.lower() not in valid_cats:
            cat = ""
        migrate.append((sku, url, cat, opc))
        if LIMIT and len(migrate) >= LIMIT:
            break

    no_opc = sum(1 for m in migrate if not m[3])
    print(f"\nPLAN: migrate {len(migrate)} ({no_opc} without OPC - suspended/not yet visible) | "
          f"deferred (no live listing - phase 2): {deferred} | "
          f"already in full sheet: {skipped_dupe} | no eBay URL: {skipped_nourl}")
    for sku, url, cat, opc in migrate[:8]:
        print(f"  {sku}  opc={opc}  cat={'kept' if cat else 'blank->worklist'}")
    if DRY_RUN:
        print("DRY RUN - nothing written")
        return

    def cell(header, sku, url, cat, opc):
        return {"SKU": sku, "Supplier URL": url, "Category": cat,
                "Sync Status": "Synced", "OPC": opc}.get(header, "")

    new_rows = [[cell(h, *m) for h in full_headers] for m in migrate]
    CHUNK = 500
    for i in range(0, len(new_rows), CHUNK):
        full.append_rows(new_rows[i:i + CHUNK], value_input_option="RAW",
                         table_range="A1")
    print(f"Appended {len(new_rows)} row(s) to {FULL_SHEET}. The pipeline fills "
          "everything else on each row's first sync; semi sheet untouched.")


if __name__ == "__main__":
    main()
