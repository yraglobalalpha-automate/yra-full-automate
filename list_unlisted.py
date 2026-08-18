"""One-off, READ-ONLY (2026-08-18): OnBuy asked for a CSV of impacted
listings. For this account the enduring impact is sheet products whose
listings remain invisible to the listings API (suspended since the
zero-price incident). Print one parseable line per such row."""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    live = set()
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
            s = str((it or {}).get("sku") or "").strip()
            if s:
                live.add(s)
        if len(items) < limit:
            break
        offset += limit
    print(f"live listings: {len(live)}")

    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = gspread.authorize(creds).open("YRA_Full_Feed_Master").sheet1
    n = 0
    for r in sheet.get_all_records():
        sku = str(r.get("SKU") or "").strip()
        status = str(r.get("Sync Status") or "").strip()
        if not sku or sku in live:
            continue
        if not (status.startswith("Synced") or status.startswith("Pending Approval")):
            continue
        n += 1
        print(f"UNLISTED|{sku}|{str(r.get('OPC') or '').strip()}|{str(r.get('Selling Price (£)') or '')}|{str(r.get('Title') or '')[:80]}")
    print(f"unlisted-but-created rows: {n}")


if __name__ == "__main__":
    main()
