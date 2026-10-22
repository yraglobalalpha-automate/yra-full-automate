"""READ-ONLY (2026-10-22): which sheet rows sit on a commission tier that OnBuy may not honour, and which rows match a title keyword?
Why: OnBuy charged 15% on two 3D printers (orders T6QR55D / T6Q6FJQ) that the sheet files under "Printers" (Consumer Electronics, 7%):
the commission follows the category of OnBuy's catalogue product, so rows whose sheet category is on a cheap tier are exposed.
Prints (both product tabs; never a cost cell):
  1. every row whose Title matches KEYWORDS (regex, default: 3D printing products): SKU | tab/row | Category | Fee % | price | stock,
     with counts per Category,
  2. the exposure census: every priced row grouped by tier name / nominal % / Fee % cell, with row counts and the sum of selling prices.
Env: SHEET_NAME, KEYWORDS (regex, case-insensitive), SHOW (max keyword rows to print, default 400)."""
import json
import os
import re
from collections import Counter, defaultdict

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import fees
import sheet_tabs
from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "__SHEET__"
KEYWORDS = re.compile(os.getenv("KEYWORDS") or r"3d[ -]?print|filament|3d pen|resin printer", re.I)
SHOW = int(os.getenv("SHOW") or "400")


def num(v):
    try:
        return float(str(v).replace("£", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def main():
    os.environ.setdefault("FEE_MODE", "category")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        tabs.append(book.worksheet(sheet_tabs.AMAZON_TAB))
    except Exception as exc:  # noqa: BLE001 - a store without an Amazon tab
        print(f"(no Amazon tab: {str(exc)[:80]})")
    hits = []
    census = defaultdict(lambda: [0, 0.0, 0])        # (tab, tier, nominal, fee cell) -> rows, price sum, in-stock rows
    no_rule = Counter()
    for ws in tabs:
        values = with_retry(lambda ws=ws: ws.get_all_values(), what=f"read {ws.title}", max_attempts=3)
        if not values:
            continue
        header = [str(h).strip() for h in values[0]]
        ix = {h: i for i, h in enumerate(header) if h}

        def cell(r, k):
            return str(r[ix[k]]).strip() if k in ix and ix[k] < len(r) else ""
        for n, r in enumerate(values[1:], start=2):
            sku = cell(r, "SKU")
            if not sku:
                continue
            title = cell(r, "Title") or cell(r, "Product Name") or cell(r, "Name")
            cat, cid, fee_cell = cell(r, "Category"), cell(r, "Category ID"), cell(r, "Fee %")
            price, stock = num(cell(r, "Selling Price (£)")), num(cell(r, "Stock"))
            rule = fees.rule_for_category_id(cid) or fees.rule_for_category_path(cat)
            tier = rule.name if rule else "(no tier)"
            nominal = f"{rule.lower_pct:g}" + (f"/{rule.upper_pct:g} above {rule.threshold:g}" if rule and rule.upper_pct is not None else "") if rule else "-"
            if price:
                c = census[(ws.title, tier, nominal, fee_cell)]
                c[0] += 1
                c[1] += price
                c[2] += 1 if (stock or 0) > 0 else 0
            if not rule and cat:
                no_rule[cat[:70]] += 1
            if title and KEYWORDS.search(title):
                hits.append((sku, ws.title, n, cat, cid, fee_cell, price, stock, cell(r, "Sync Status"), title[:70]))
    print(f"KEYWORD ROWS ({KEYWORDS.pattern!r}): {len(hits)}")
    for h in hits[:SHOW]:
        sku, tab, n, cat, cid, fee_cell, price, stock, sync, title = h
        print(f"   {sku} | {tab} row {n} | cat {cat[:70] or '-'} [{cid or '-'}] | Fee % {fee_cell or '-'} | price {price} | stock {stock} | {sync} | {title}")
    print("   per category: " + "; ".join(f"{c or '-'}: {k}" for c, k in Counter(h[3][:70] for h in hits).most_common(12)))
    print("\nEXPOSURE CENSUS (priced rows by tab / tier / nominal % / Fee % cell): rows | in stock | sum of selling prices")
    for (tab, tier, nominal, fee_cell), (rows, total, instock) in sorted(census.items(), key=lambda kv: (kv[0][0], -kv[1][0])):
        print(f"   {tab:10s} | {tier:40s} | nominal {nominal:16s} | Fee % cell {fee_cell or '-':6s} | {rows:6d} rows | {instock:6d} in stock | GBP {total:,.0f}")
    if no_rule:
        print("\nCATEGORIES WITH NO TIER (rows): " + "; ".join(f"{c}: {k}" for c, k in no_rule.most_common(15)))


if __name__ == "__main__":
    main()
