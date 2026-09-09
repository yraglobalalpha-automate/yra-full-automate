"""READ-ONLY: what switching to category-exact commission would do to prices.

For every row with a cost and a category (first tab + the Amazon tab) this
prices the row three ways - the flat-20% formula the pipeline used until
now, the category-tier formula, and what is in the Selling Price cell -
and says what the sync would do in FEE_MODE=category: LOWER (the cell
holds a price the automation set itself and the tier formula is lower),
RAISE (the tier formula is higher than the cell), or KEEP. Grouped by
tier, with examples and the rows whose category maps to no tier. Writes
price_impact.csv. Touches nothing.
"""
import csv
import io
import json
import os
import sys
from collections import defaultdict

os.environ["FEE_MODE"] = "category"   # the report is about category mode

import gspread  # noqa: E402
from oauth2client.service_account import ServiceAccountCredentials  # noqa: E402

import fees  # noqa: E402
import pricing  # noqa: E402
from retry_utils import with_retry  # noqa: E402

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
TABS = [t.strip() for t in (os.getenv("SHEET_TABS") or "Amazon").split(",") if t.strip()]
OUT = "price_impact.csv"


def to_f(v):
    try:
        return float(str(v).replace(",", "").replace("£", "").strip())
    except (TypeError, ValueError):
        return 0.0


def formula_priced(price, cost, ship, rule):
    if price <= 0 or cost <= 0:
        return False
    for candidate in (pricing.calculate_selling_price(cost, ship, platform_fee_percent=pricing.PLATFORM_FEE_PERCENT),
                      pricing.calculate_selling_price(cost, ship, fee_rule=rule)):
        if abs(candidate - price) < 0.011:
            return True
    return False


def main():
    table = fees.get()
    if table is None:
        raise SystemExit("commission tables missing - run refresh_fees.py and commit its two CSVs first")
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    titles = [w.title for w in book.worksheets()]
    sheets = [book.sheet1] + [book.worksheet(t) for t in TABS if t in titles and t != book.sheet1.title]

    rows_out, by_tier = [], defaultdict(lambda: {"rows": 0, "lower": 0, "raise": 0, "keep": 0, "old": 0.0, "new": 0.0, "cell": 0.0})
    no_tier, no_category, no_cost = [], 0, 0
    for ws in sheets:
        records = with_retry(lambda ws=ws: ws.get_all_records(), what=f"{ws.title} read", max_attempts=3)
        for idx, r in enumerate(records):
            r = {str(k).strip(): v for k, v in r.items()}
            cost, ship = to_f(r.get("Cost Price (£)")), to_f(r.get("Shipping Cost (£)"))
            cell = to_f(r.get("Selling Price (£)"))
            path = str(r.get("Category") or "").strip()
            if cost <= 0:
                no_cost += 1
                continue
            if not path:
                no_category += 1
                continue
            cid = table.path_to_id.get(path.lower())
            rule = table.rule_for_category_id(cid) if cid else None
            old = pricing.calculate_selling_price(cost, ship, platform_fee_percent=pricing.PLATFORM_FEE_PERCENT)
            if rule is None:
                no_tier.append(f"{ws.title} row {idx + 2} {r.get('SKU')} | {path}")
                continue
            new = pricing.calculate_selling_price(cost, ship, fee_rule=rule)
            if cell > 0 and formula_priced(cell, cost, ship, rule) and new < cell:
                action = "LOWER"
            elif new > cell:
                action = "RAISE"
            else:
                action = "KEEP"
            fee_pct = pricing.effective_fee_percent(new, rule)
            rows_out.append({"tab": ws.title, "row": idx + 2, "sku": r.get("SKU"), "category": path, "tier": rule.name,
                             "fee_pct_at_new_price": f"{fee_pct:.2f}", "cost": f"{cost:.2f}", "shipping": f"{ship:.2f}",
                             "price_flat20": f"{old:.2f}", "price_tier": f"{new:.2f}", "price_in_cell": f"{cell:.2f}",
                             "sync_would": action, "change_vs_cell": f"{(new - cell):+.2f}" if cell else ""})
            agg = by_tier[rule.name]
            agg["rows"] += 1
            agg[action.lower()] += 1
            agg["old"] += old
            agg["new"] += new
            agg["cell"] += cell

    with io.open(OUT, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows_out[0].keys()) if rows_out else ["tab"])
        w.writeheader()
        for row in rows_out:
            w.writerow(row)

    total = len(rows_out)
    lower = sum(1 for r in rows_out if r["sync_would"] == "LOWER")
    raise_ = sum(1 for r in rows_out if r["sync_would"] == "RAISE")
    print(f"rows priced: {total} | would LOWER: {lower} | would RAISE: {raise_} | KEEP: {total - lower - raise_}")
    print(f"skipped: no cost {no_cost} | no category {no_category} | category without a tier {len(no_tier)}")
    print(f"tier mode: {pricing.FEE_TIER_MODE} (upper rate on the part above the threshold)\n")
    print(f"{'tier':<40} {'rows':>5} {'lower':>5} {'raise':>5} {'keep':>5}  {'avg flat20':>10} {'avg tier':>9} {'avg cell':>9}")
    for name, a in sorted(by_tier.items(), key=lambda kv: -kv[1]["rows"]):
        n = a["rows"]
        print(f"{name:<40} {n:>5} {a['lower']:>5} {a['raise']:>5} {a['keep']:>5}  {a['old']/n:>10.2f} {a['new']/n:>9.2f} {a['cell']/n:>9.2f}")
    print("\nexamples (LOWER):")
    for r in [x for x in rows_out if x["sync_would"] == "LOWER"][:12]:
        print(f"   {r['tab']} row {r['row']} {r['sku']} [{r['tier']} {r['fee_pct_at_new_price']}%] cost {r['cost']} -> flat20 {r['price_flat20']}, tier {r['price_tier']}, cell {r['price_in_cell']}")
    print("examples (RAISE):")
    for r in [x for x in rows_out if x["sync_would"] == "RAISE"][:8]:
        print(f"   {r['tab']} row {r['row']} {r['sku']} [{r['tier']} {r['fee_pct_at_new_price']}%] cost {r['cost']} -> tier {r['price_tier']}, cell {r['price_in_cell']}")
    if no_tier:
        print("\ncategories with no tier (first 20):")
        for line in no_tier[:20]:
            print("   ", line)
    print(f"\nwrote {OUT} ({total} rows)")


if __name__ == "__main__":
    sys.exit(main())
