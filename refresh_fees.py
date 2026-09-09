"""Refresh OnBuy's commission tables from the API (GET calls only).

Writes onbuy_commission_tiers.csv (one row per tier: rate below the
threshold, rate above it, the threshold and the minimum fee, in pounds)
and onbuy_category_tiers.csv (every category id -> its tier), the two
tables fees.py reads. Prints the tier table, how the listable categories
in onbuy_categories_only.csv spread across tiers, and any listable
category the API maps to no tier. Commit both files after a run - like
the category list, the tables live in the repo so a sync never spends
its hourly quota re-fetching ~5,000 categories.
"""
import csv
import io
import json
import os
import time
from collections import Counter

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import raise_for_status, with_retry

_DIR = os.path.dirname(os.path.abspath(__file__))
TIERS_CSV = os.path.join(_DIR, "onbuy_commission_tiers.csv")
CATEGORY_TIERS_CSV = os.path.join(_DIR, "onbuy_category_tiers.csv")
CATEGORIES_CSV = os.path.join(_DIR, "onbuy_categories_only.csv")


def page_all(onbuy, path, limit=100, max_pages=80):
    out, offset = [], 0
    for _ in range(max_pages):
        def _page(off=offset):
            r = onbuy._send("GET", f"{BASE_URL}/{path}", what=f"{path} page",
                            params={"site_id": onbuy.site_id, "limit": limit, "offset": off}, timeout=60)
            raise_for_status(r, what=f"{path} page")
            return r
        body = with_retry(_page, what=f"{path} page {offset}", max_attempts=3).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            break
        out.extend(items)
        if len(items) < limit:
            break
        offset += limit
        time.sleep(0.3)
    return out


def pounds(pence):
    try:
        return f"{float(pence) / 100:.2f}"
    except (TypeError, ValueError):
        return ""


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")

    raw_tiers = page_all(onbuy, "commission-tiers")
    tiers = {}
    for t in raw_tiers:
        a = (t or {}).get("attributes") or {}
        tid = str((t or {}).get("id") or "").strip()
        if not tid:
            continue
        tiers[tid] = {
            "Tier ID": tid,
            "Name": str(a.get("sales_fee_name") or "").strip(),
            "Lower %": str(a.get("sales_fee_lower_threshold_percentage") or "").strip(),
            "Upper %": str(a.get("sales_fee_upper_threshold_percentage") or "").strip(),
            "Threshold (£)": pounds(a.get("sales_fee_threshold_amount")) if a.get("sales_fee_threshold_amount") else "",
            "Min Fee (£)": pounds(a.get("sales_fee_min_amount")) if a.get("sales_fee_min_amount") else "",
        }
    print(f"commission tiers: {len(tiers)}")
    for t in sorted(tiers.values(), key=lambda x: x["Name"]):
        extra = f", {t['Upper %']}% above £{t['Threshold (£)']}" if t["Upper %"] and t["Threshold (£)"] else (
            f", {t['Upper %']}% upper (no threshold given)" if t["Upper %"] else "")
        print(f"   {t['Name']:<42} {t['Lower %']:>6}%{extra}  min £{t['Min Fee (£)']}")

    categories = page_all(onbuy, "categories")
    mapping = {}
    for c in categories:
        cid = str((c or {}).get("category_id") or "").strip()
        tid = str((c or {}).get("commission_tier_id") or "").strip()
        if cid:
            mapping[cid] = tid
    print(f"categories fetched: {len(categories)} | with a tier: {sum(1 for v in mapping.values() if v in tiers)}")

    with io.open(TIERS_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["Tier ID", "Name", "Lower %", "Upper %", "Threshold (£)", "Min Fee (£)"])
        w.writeheader()
        for t in sorted(tiers.values(), key=lambda x: x["Name"]):
            w.writerow(t)
    with io.open(CATEGORY_TIERS_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Category ID", "Commission Tier ID", "Tier Name"])
        for cid in sorted(mapping, key=int):
            tid = mapping[cid]
            w.writerow([cid, tid, tiers.get(tid, {}).get("Name", "")])
    print(f"written: {os.path.basename(TIERS_CSV)} ({len(tiers)} rows), {os.path.basename(CATEGORY_TIERS_CSV)} ({len(mapping)} rows)")

    if os.path.exists(CATEGORIES_CSV):
        spread, missing = Counter(), []
        with io.open(CATEGORIES_CSV, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                cid = str(r.get("Category ID") or "").strip()
                tid = mapping.get(cid, "")
                if tid in tiers:
                    spread[tiers[tid]["Name"]] += 1
                else:
                    missing.append(f"{cid} {r.get('OnBuy Category Path')}")
        print("\nlistable categories by tier:")
        for name, n in spread.most_common():
            print(f"   {n:>5}  {name}")
        print(f"listable categories with NO tier: {len(missing)}")
        for m in missing[:20]:
            print("   ", m)
    print("\nsample category record:", json.dumps(categories[0])[:400] if categories else "-")


if __name__ == "__main__":
    main()
