"""One-off, READ-ONLY (2026-08-14): an OnBuy order for SKU 941214867068
showed the content of the neighbouring row's product (939828415904) - a
CSV-era one-row shift inside OnBuy's catalogue, while our sheet is clean
(verified). Sweep every live listing on the account, join to the sheet by
SKU, and compare OnBuy's product name against our Title to count and
locate every shifted listing. Prints the first raw item so we learn the
endpoint's actual fields. Changes nothing."""
import json
import os
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def similar(a, b):
    """Token-overlap similarity - titles may differ in truncation/spacing."""
    ta, tb = set(norm(a).split()), set(norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    listings = {}
    name_key = None
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
        if offset == 0:
            first = dict(items[0] or {})
            print(f"first item keys: {sorted(first.keys())}")
            for k in ("product_name", "name", "title", "product_title"):
                if first.get(k):
                    name_key = k
                    break
            print(f"name field: {name_key}")
            if not name_key:
                print("NO NAME FIELD on listings - scan by name impossible via this endpoint")
                return
        for it in items:
            it = it or {}
            sku = str(it.get("sku") or "").strip()
            if sku:
                listings[sku] = (str(it.get(name_key) or "").strip(),
                                 str(it.get("created_at") or "")[:16])
        if len(items) < limit:
            break
        offset += limit
    print(f"live listings fetched: {len(listings)}")

    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = gspread.authorize(creds).open("YRA_Full_Feed_Master").sheet1
    rows = sheet.get_all_records()

    protected = set()
    _pp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "protected_skus.txt")
    if os.path.exists(_pp):
        with open(_pp, encoding="utf-8") as _fh:
            protected = {ln.split("#", 1)[0].strip() for ln in _fh if ln.split("#", 1)[0].strip()}
    print(f"protected SKUs on file: {len(protected)}")
    matched = mismatched = no_title = not_listed = 0
    mismatches = []  # (rownum, sku, listing_name, sheet_title, shifted_from_above)
    for i, r in enumerate(rows):
        sku = str(r.get("SKU") or "").strip()
        if not sku or sku not in listings:
            not_listed += 1
            continue
        title = str(r.get("Title") or "").strip()
        if not title:
            no_title += 1
            continue
        lname, lcreated = listings[sku]
        if similar(lname, title) >= 0.5:
            matched += 1
            if sku in protected:
                # visible AND the name matches again -> safe to unprotect
                print(f"OK|{i + 2}|{sku}|onbuy={lname[:60]}")
            continue
        mismatched += 1
        # Which neighbouring row does OnBuy's name belong to? +1 = the row
        # BELOW (GTV 2026-08-21 pattern), -1 = the row ABOVE (YRA 2026-08-14
        # pattern); no neighbour within 3 rows = barcode collision on a
        # foreign/shared catalogue product (report-only, delist+relist).
        offset = None
        for k in (1, -1, 2, -2, 3, -3):
            j = i + k
            if 0 <= j < len(rows):
                nt = str(rows[j].get("Title") or "").strip()
                if nt and similar(lname, nt) >= 0.5:
                    offset = k
                    break
        mismatches.append((i + 2, sku, lname[:60], title[:60], offset, lcreated))

    print(f"sheet rows with live listing + title: {matched + mismatched} "
          f"({no_title} rows without title yet, {not_listed} rows with no live listing)")
    kinds = {}
    for m in mismatches:
        k = "collision" if m[4] is None else f"shift{m[4]:+d}"
        kinds[k] = kinds.get(k, 0) + 1
    print(f"MATCHED: {matched} | MISMATCHED: {mismatched} | kinds: {kinds}")
    for m in mismatches:
        k = "collision" if m[4] is None else f"shift{m[4]:+d}"
        print(f"MM|{m[0]}|{m[1]}|{k}|{m[5]}|onbuy={m[2]}|sheet={m[3]}")
    if mismatches:
        rownums = [m[0] for m in mismatches]
        print(f"mismatch row range: {min(rownums)}..{max(rownums)}")
    # Daily guard (2026-08-21): shifts/collisions that are NOT yet protected
    # are new exposure - email the on-call so they get zeroed/protected.
    fresh = [m for m in mismatches if m[1] not in protected]
    if fresh and os.getenv("ALERT_EMAIL_TO"):
        try:
            import notify
            lines = []
            for m in fresh[:60]:
                kind = "collision" if m[4] is None else f"shift{m[4]:+d}"
                lines.append(f"row {m[0]} SKU {m[1]} [{kind}] onbuy={m[2]} | sheet={m[3]}")
            body = (
                "Scan of live listings vs sheet titles found mismatched listings that are "
                "not in protected_skus.txt yet. Action: zero stock (zero_stock_mismatched), "
                "add them to protected_skus.txt, repair shifts / delist collisions."
                + chr(10) + chr(10) + chr(10).join(lines)
            )
            notify.send_alert_email(
                f"OnBuy content mismatch: {len(fresh)} unprotected listing(s) show another product",
                body)
            print(f"alert email sent for {len(fresh)} unprotected mismatch(es)")
        except Exception as exc:
            print(f"alert email failed: {exc}")


if __name__ == "__main__":
    main()
