"""READ-ONLY staleness report: how old is each product row's "Last Checked
Time", broken down by tab, sheet stock level, live/not-live and Sync
Status - plus the oldest rows. Writes nothing anywhere (no OnBuy, no
Supabase, no Sheet writes).

"Last Checked Time" is a naive Pakistan wall-clock stamp (generate_xml.py
writes datetime.now(PK_TZ) without tzinfo), so "now" is taken the same way.
"""
import json
import os
import re
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import sheet_tabs

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
PK = ZoneInfo("Asia/Karachi")
BUCKETS = [(6, "<6h"), (12, "6-12h"), (24, "12-24h"), (48, "1-2d"), (96, "2-4d"),
           (168, "4-7d"), (336, "7-14d"), (10 ** 9, ">14d")]


def parse(ts):
    try:
        return datetime.strptime(str(ts).strip(), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def bucket(hours):
    if hours is None:
        return "never"
    for limit, name in BUCKETS:
        if hours < limit:
            return name
    return ">14d"


def to_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def report_tab(ws, now):
    values = ws.get_all_values()
    headers = [str(h).strip() for h in values[0]]
    ix = {h: i for i, h in enumerate(headers) if h}

    def cell(r, h):
        i = ix.get(h)
        return str(r[i]).strip() if i is not None and i < len(r) else ""

    rows = []
    for n, r in enumerate(values[1:], start=2):
        sku = cell(r, "SKU")
        url = cell(r, "Supplier URL")
        if not sku and not url:
            continue
        ts = parse(cell(r, "Last Checked Time"))
        age = (now - ts).total_seconds() / 3600 if ts else None
        rows.append({"row": n, "sku": sku, "url": url, "age": age, "ts": cell(r, "Last Checked Time"),
                     "stock": to_int(cell(r, "Stock")), "status": cell(r, "Status").upper(),
                     "sync": cell(r, "Sync Status"), "created": cell(r, "OnBuy Product Created").upper(),
                     "title": cell(r, "Title")})
    title = ws.title
    with_url = [r for r in rows if r["url"]]
    print(f"\n===== TAB {title}: {len(rows)} rows with a SKU or URL | {len(with_url)} with a Supplier URL =====")
    print("age since last check (rows WITH a Supplier URL):")
    c = Counter(bucket(r["age"]) for r in with_url)
    for name in ["<6h", "6-12h", "12-24h", "1-2d", "2-4d", "4-7d", "7-14d", ">14d", "never"]:
        print(f"   {name:7s} {c.get(name, 0):6d}")

    def grp(r):
        s = r["stock"]
        if s is None:
            return "stock blank"
        if s <= 0:
            return "stock 0 (OOS)"
        return "stock 1-5" if s <= 5 else "stock >5"

    print("median/max age (h) by sheet-stock group, and share older than 24h / 48h:")
    for g in ("stock 1-5", "stock >5", "stock 0 (OOS)", "stock blank"):
        ages = sorted(r["age"] for r in with_url if grp(r) == g and r["age"] is not None)
        never = sum(1 for r in with_url if grp(r) == g and r["age"] is None)
        n = len(ages) + never
        if not n:
            continue
        med = ages[len(ages) // 2] if ages else float("nan")
        print(f"   {g:14s} n={n:5d} median={med:7.1f}h max={(ages[-1] if ages else 0):7.1f}h "
              f">24h={sum(1 for a in ages if a > 24) + never:5d} >48h={sum(1 for a in ages if a > 48) + never:5d} never={never}")

    live_in_stock = [r for r in with_url if r["created"] == "TRUE" and (r["stock"] or 0) > 0]
    print(f"LIVE (created on OnBuy) and in stock on the sheet: {len(live_in_stock)}")
    for lim in (24, 48, 96, 168):
        k = sum(1 for r in live_in_stock if r["age"] is None or r["age"] > lim)
        print(f"   of those not re-checked for >{lim}h: {k}")

    stale = [r for r in with_url if r["age"] is None or r["age"] > 48]
    print(f"stale >48h by Sync Status prefix ({len(stale)} rows):")
    sc = Counter(re.sub(r"[:(\d].*$", "", r["sync"]).strip()[:34] or "(blank)" for r in stale)
    for k, v in sc.most_common(10):
        print(f"   {v:5d}  {k}")

    print("age by EXACT sheet stock (live+in-stock rows only count towards risk):")
    by_stock = {}
    for r in with_url:
        k = r["stock"] if r["stock"] is not None else -1
        k = k if k <= 5 else (6 if k <= 10 else 11)
        by_stock.setdefault(k, []).append(r)
    for k in sorted(by_stock):
        rs = by_stock[k]
        ages = sorted(r["age"] if r["age"] is not None else 9999 for r in rs)
        label = {-1: "blank", 6: "6-10", 11: ">10"}.get(k, str(k))
        pct = lambda q: ages[min(len(ages) - 1, int(q * len(ages)))]
        print(f"   stock {label:>5s}: n={len(rs):5d}  p50={pct(.5):6.1f}h p90={pct(.9):6.1f}h max={ages[-1]:6.1f}h "
              f"live={sum(1 for r in rs if r['created'] == 'TRUE'):5d}")

    # per-row export for offline simulation of rotation strategies
    with open(f"stale_rows_{title}.csv", "w", newline="", encoding="utf-8") as f:
        import csv
        w = csv.writer(f)
        w.writerow(["tab", "row", "sku", "stock", "status", "created", "age_h", "sync"])
        for r in rows:
            w.writerow([title, r["row"], r["sku"], r["stock"] if r["stock"] is not None else "",
                        r["status"], r["created"], "" if r["age"] is None else f"{r['age']:.2f}", r["sync"][:40]])

    print("OLDEST 25 rows:")
    oldest = sorted(with_url, key=lambda r: (-(r["age"] if r["age"] is not None else 10 ** 9)))[:25]
    for r in oldest:
        a = "never" if r["age"] is None else f"{r['age']:.0f}h"
        print(f"   row {r['row']:5d} age {a:>6s} stock {r['stock']} {r['status'][:8]:8s} created={r['created'][:5]:5s} "
              f"sync={r['sync'][:46]!r} sku={r['sku']}")


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(SHEET_NAME)
    now = datetime.now(PK).replace(tzinfo=None)
    print(f"now (PK wall clock): {now:%Y-%m-%d %H:%M} | spreadsheet: {book.title} | tabs: {[t.title for t in book.worksheets()]}")
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        tabs.append(book.worksheet("Amazon"))
    except gspread.exceptions.WorksheetNotFound:
        pass
    for ws in tabs:
        report_tab(ws, now)


if __name__ == "__main__":
    main()
