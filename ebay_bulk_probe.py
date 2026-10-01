"""READ-ONLY probe (2026-10-01): can this app call the Browse API's bulk
getItems (up to 20 items per call)? eBay's rate-limit listing shows a
separate "buy.browse.item.bulk" resource with its own 5,000/day pool; if
the endpoint answers, one call replaces up to 20 single-item fetches and
the per-row call budget stops being the bottleneck.

Takes a sample of real eBay item ids from the sheet, calls getItems once
(20 ids) and prints the HTTP status, per-item fields (availability, price,
sold quantity) and the rate-limit counters before/after so we can see which
pool the call consumed. Writes nothing anywhere.
"""
import json
import os
import re

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

import sheet_tabs
from generate_xml import get_ebay_token

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
HEAD = {"X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"}


def limits(token):
    r = requests.get("https://api.ebay.com/developer/analytics/v1_beta/rate_limit/",
                     headers={"Authorization": f"Bearer {token}"},
                     params={"api_context": "buy", "api_name": "browse"}, timeout=30)
    out = {}
    for rl in r.json().get("rateLimits", []):
        for res in rl.get("resources", []):
            for rate in res.get("rates", []):
                out[res.get("name")] = (rate.get("limit"), rate.get("remaining"))
    return out


def main():
    token = get_ebay_token()
    if not token:
        raise SystemExit("no eBay token")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(SHEET_NAME)
    ws = sheet_tabs.product_sheet(book)
    values = ws.get_all_values()
    h = [str(x).strip() for x in values[0]]
    iu, istock = h.index("Supplier URL"), h.index("Stock")
    ids = []
    for r in values[1:]:
        url = r[iu] if iu < len(r) else ""
        m = re.search(r"/itm/(\d+)", url)
        if m and "var=" not in url and len(ids) < 20 and (len(ids) < 10 or str(r[istock]).strip() == "5"):
            ids.append(m.group(1))
        if len(ids) >= 20:
            break
    print(f"sample: {len(ids)} item ids")

    before = limits(token)
    print("limits BEFORE:", before)
    rest_ids = ",".join(f"v1|{i}|0" for i in ids)
    resp = requests.get("https://api.ebay.com/buy/browse/v1/item", params={"item_ids": rest_ids},
                        headers={"Authorization": f"Bearer {token}", **HEAD}, timeout=60)
    print("getItems HTTP", resp.status_code)
    try:
        body = resp.json()
    except ValueError:
        print("non-JSON body:", resp.text[:500])
        return
    print("top-level keys:", list(body.keys()))
    if resp.status_code != 200 and "items" not in body:
        print("ERROR BODY:", json.dumps(body)[:1200])
    for it in body.get("items", [])[:20]:
        est = (it.get("estimatedAvailabilities") or [{}])[0]
        print("  ", it.get("legacyItemId"), "|", str(it.get("title"))[:34], "| price", (it.get("price") or {}).get("value"),
              "| status", est.get("estimatedAvailabilityStatus"), "| qty", est.get("estimatedAvailableQuantity"),
              "| thr", est.get("availabilityThresholdType"), est.get("availabilityThreshold"),
              "| sold", est.get("estimatedSoldQuantity"))
    for w in (body.get("warnings") or [])[:6]:
        print("  WARNING:", json.dumps(w)[:200])
    for e in (body.get("errors") or [])[:6]:
        print("  ERROR:", json.dumps(e)[:200])
    after = limits(token)
    print("limits AFTER :", after)
    for k in after:
        if before.get(k) and after[k][1] is not None and before[k][1] is not None:
            print(f"  consumed on {k}: {int(before[k][1]) - int(after[k][1])}")


if __name__ == "__main__":
    main()
