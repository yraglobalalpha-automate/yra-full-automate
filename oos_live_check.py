"""READ-ONLY: which listings can still SELL although the sheet says out of stock?

Sweeps every live OnBuy listing (GET /v2/listings) and compares its stock with
the sheet's Stock: rows where the sheet says 0 but OnBuy still shows stock are
the oversell class (a customer can buy something the supplier no longer has).
For each one it prints when the sheet last checked the row, whether the sync
believes it already pushed that state (Last OnBuy Sync >= Last Checked Time)
and when OnBuy last touched the listing - so a push that was skipped can be
told apart from one that was sent and did not hold. Writes only a CSV artifact.
"""
import csv
import json
import os
import time
from datetime import datetime, timedelta

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import sheet_tabs
import sku_aliases
from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
FMT = "%Y-%m-%d %H:%M:%S"


def ptime(s):
    try:
        return datetime.strptime(str(s).strip()[:19], FMT)
    except (TypeError, ValueError):
        return None


def to_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def sheet_rows():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        amz = book.worksheet("Amazon")
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    out = {}
    for tab in tabs:
        for idx, row in enumerate(tab.get_all_records()):
            sku = str(row.get("SKU") or "").replace(",", "").strip()
            if sku:
                out.setdefault(sku, {"tab": tab.title, "row": idx + 2, "stock": to_int(row.get("Stock")),
                                     "checked": ptime(row.get("Last Checked Time")),
                                     "pushed": ptime(row.get("Last OnBuy Sync")),
                                     "sync": str(row.get("Sync Status") or "")[:40],
                                     "created": str(row.get("OnBuy Product Created") or "").strip().upper() == "TRUE"})
    return out


def live_listings(onbuy):
    out, offset = {}, 0
    while True:
        def _page(off=offset):
            for _try in range(6):
                r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                                params={"site_id": onbuy.site_id, "limit": 100, "offset": off}, timeout=60)
                if r.status_code in (429, 500, 502, 503) and _try < 5:
                    time.sleep(60)
                    continue
                r.raise_for_status()
                return r
        body = with_retry(_page, what=f"listings page {offset}", max_attempts=3).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            break
        for it in items:
            it = it or {}
            sku = sku_aliases.to_true(it.get("sku"))
            if sku:
                out[sku] = {"stock": to_int(it.get("stock")) or 0, "price": it.get("price"),
                            "updated": ptime(it.get("updated_at")), "opc": it.get("opc")}
        if len(items) < 100:
            break
        offset += 100
    return out


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    sheet = sheet_rows()
    live = live_listings(onbuy)
    now = datetime.utcnow()
    print(f"sheet SKUs: {len(sheet)} | live OnBuy listings swept: {len(live)}")

    risky, lost = [], []
    for sku, l in live.items():
        s = sheet.get(sku)
        if not s or s["stock"] is None:
            continue
        if s["stock"] == 0 and l["stock"] > 0:
            risky.append((sku, s, l))
        elif s["stock"] > 0 and l["stock"] == 0:
            lost.append((sku, s, l))
    not_on_sheet = [(k, l) for k, l in live.items() if k not in sheet and l["stock"] > 0]
    print(f"\nSELLABLE ON ONBUY BUT SHEET SAYS OUT OF STOCK: {len(risky)}")
    print(f"in stock on the sheet but 0 on OnBuy (lost sales): {len(lost)}")
    print(f"live with stock but NOT on the sheet (unmanaged): {len(not_on_sheet)}")

    def pushed_after_check(s):
        return bool(s["pushed"] and s["checked"] and s["pushed"] >= s["checked"])

    cls = {}
    for sku, s, l in risky:
        k = ("sync believes the zero was pushed" if pushed_after_check(s)
             else "zero push still pending (sheet checked after last push)")
        cls[k] = cls.get(k, 0) + 1
    for k, v in cls.items():
        print(f"  {v:5d}  {k}")
    for sku, s, l in sorted(risky, key=lambda t: (t[1]["checked"] or datetime(2000, 1, 1)))[:40]:
        # sheet times are Pakistan wall clock (UTC+5), OnBuy times are UTC
        age = f"{(datetime.utcnow() - (s['checked'] - timedelta(hours=5))).total_seconds() / 3600:.0f}h" if s["checked"] else "?"
        print(f"  RISK {sku} {s['tab']} row {s['row']} | live stock {l['stock']} price {l['price']} | "
              f"sheet checked {s['checked']} (~{age} ago) pushed {s['pushed']} | OnBuy updated {l['updated']} | {s['sync']}")

    # Who has been WRITING to the listings? updated_at by hour (UTC) and the busiest minutes - a burst that
    # matches no workflow run is an outside writer (a dashboard/CSV upload).
    hours, minutes = {}, {}
    for l in live.values():
        u = l["updated"]
        if u and (now - u).total_seconds() < 72 * 3600:
            hours[u.strftime("%m-%d %H")] = hours.get(u.strftime("%m-%d %H"), 0) + 1
            minutes[u.strftime("%m-%d %H:%M")] = minutes.get(u.strftime("%m-%d %H:%M"), 0) + 1
    print("\nlisting updated_at by hour (UTC), last 72h:")
    print("  " + "  ".join(f"{h}:{n}" for h, n in sorted(hours.items())))
    print("busiest minutes (>=40 listings):")
    for m, n in sorted(minutes.items()):
        if n >= 40:
            print(f"  {m} UTC: {n}")

    with open("oos_live_check.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["class", "sku", "tab", "row", "sheet_stock", "live_stock", "sheet_checked_pk", "last_onbuy_sync_pk",
                    "onbuy_updated_at", "sync_status"])
        for sku, s, l in risky:
            w.writerow(["sellable-but-sheet-oos", sku, s["tab"], s["row"], s["stock"], l["stock"], s["checked"], s["pushed"],
                        l["updated"], s["sync"]])
        for sku, s, l in lost:
            w.writerow(["in-stock-but-zero-on-onbuy", sku, s["tab"], s["row"], s["stock"], l["stock"], s["checked"],
                        s["pushed"], l["updated"], s["sync"]])
        for k, l in not_on_sheet:
            w.writerow(["live-not-on-sheet", k, "", "", "", l["stock"], "", "", l["updated"], ""])


if __name__ == "__main__":
    main()
