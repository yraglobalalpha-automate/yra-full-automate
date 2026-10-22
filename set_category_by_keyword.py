"""Relabel rows whose Title matches a pattern to the OnBuy category their commission really follows (2026-10-22).

Why: 3D printers were filed under "Electronics & Technology > Computing & Gaming > Printers & Accessories > Printers" (Consumer Electronics,
7% -> Fee % 8.40) by the word scorer, while OnBuy keeps 3D printing under "Arts, Crafts & Sewing > Other Crafts > 3D Printing" (Standard 15%)
and charged 15% on Makstore orders T6QR55D / T6Q6FJQ. The sheet's category decides the fee in the price formula, so the rows must carry the
category OnBuy really uses. RULES: one rule per line, "<title regex> => <OnBuy category path>" (case-insensitive; the path must be listable in
onbuy_categories_only.csv, its ID is looked up there); first matching rule wins. Default rules = the 3D printing family. ONLY_FROM (regex on
the row's current Category; default: any) limits which rows move; a row already on the rule's path is skipped.
APPLY=yes writes Category + Category ID (old values first go to a backup tab "Categories relabelled <date>"); otherwise it prints the plan.
Live listings keep their OnBuy product (updates never send a category); the sync re-prices from the new tier at its next visit.
Prints no cost cell. Env: APPLY, RULES, ONLY_FROM, SHEET_NAME."""
import csv
import json
import os
import re
import time

os.environ.setdefault("FEE_MODE", "category")

import gspread  # noqa: E402
from oauth2client.service_account import ServiceAccountCredentials  # noqa: E402

import fees  # noqa: E402
import sheet_tabs  # noqa: E402
from retry_utils import with_retry  # noqa: E402

APPLY = (os.getenv("APPLY") or "").strip().lower() in ("1", "yes", "true")
SHEET_NAME = os.getenv("SHEET_NAME") or "__SHEET__"
ONLY_FROM = re.compile(os.getenv("ONLY_FROM"), re.I) if (os.getenv("ONLY_FROM") or "").strip() else None
DEFAULT_RULES = """
filament => Arts, Crafts & Sewing > Other Crafts > 3D Printing > 3D Printer Filament
3d[ -]?(printing[ -]?)?pens? => Arts, Crafts & Sewing > Other Crafts > 3D Printing > 3D Printing Pens
3d[ -]?print(er|ing)|resin printer => Arts, Crafts & Sewing > Other Crafts > 3D Printing > 3D Printers
"""
BACKUP_HEADER = ["SKU", "Tab", "Row", "Old Category", "Old Category ID", "New Category", "New Category ID", "Written at (UTC)"]


def col_letter(n):
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def num(v):
    try:
        return float(str(v).replace("£", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def load_rules():
    paths = {}
    with open("onbuy_categories_only.csv", encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            p = str(r.get("OnBuy Category Path") or "").strip()
            if p:
                paths[p.lower()] = (p, str(r.get("Category ID") or "").strip())
    rules = []
    for ln in (os.getenv("RULES") or DEFAULT_RULES).splitlines():
        if "=>" not in ln.strip():
            continue
        pat, path = [x.strip() for x in ln.split("=>", 1)]
        if path.lower() not in paths:
            raise SystemExit(f"rule {pat!r}: category {path!r} is not a listable OnBuy category in onbuy_categories_only.csv")
        rules.append((re.compile(pat, re.I), paths[path.lower()][0], paths[path.lower()][1]))
    return rules


def tier_text(cid, path):
    rule = fees.rule_for_category_id(cid) or fees.rule_for_category_path(path)
    return f"{rule.name} {rule.lower_pct:g}%" if rule else "no tier"


def main():
    rules = load_rules()
    print(f"{'APPLY' if APPLY else 'DRY RUN'} | {len(rules)} rule(s): " + "; ".join(f"/{r[0].pattern}/ -> {r[1].split(' > ')[-1]} [{r[2]}]" for r in rules)
          + (f" | only rows whose Category matches /{ONLY_FROM.pattern}/" if ONLY_FROM else ""))
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        tabs.append(book.worksheet(sheet_tabs.AMAZON_TAB))
    except Exception as exc:  # noqa: BLE001
        print(f"(no Amazon tab: {str(exc)[:80]})")
    plan, same = [], 0
    for ws in tabs:
        values = with_retry(lambda ws=ws: ws.get_all_values(), what=f"read {ws.title}", max_attempts=3)
        if not values:
            continue
        header = [str(h).strip() for h in values[0]]
        ix = {h: i for i, h in enumerate(header) if h}
        if not {"SKU", "Title", "Category"} <= set(ix):
            print(f"tab {ws.title!r} lacks SKU / Title / Category - skipped")
            continue

        def cell(r, k):
            return str(r[ix[k]]).strip() if k in ix and ix[k] < len(r) else ""
        for n, r in enumerate(values[1:], start=2):
            sku, title = cell(r, "SKU"), cell(r, "Title")
            if not sku or not title:
                continue
            for pat, path, cid in rules:
                if not pat.search(title):
                    continue
                old_cat, old_cid = cell(r, "Category"), cell(r, "Category ID")
                if old_cat.lower() == path.lower():
                    same += 1
                elif ONLY_FROM and not ONLY_FROM.search(old_cat):
                    pass
                else:
                    plan.append({"ws": ws, "ix": ix, "n": n, "sku": sku, "old_cat": old_cat, "old_cid": old_cid, "new_cat": path, "new_cid": cid,
                                 "fee": cell(r, "Fee %"), "price": num(cell(r, "Selling Price (£)")), "stock": num(cell(r, "Stock")), "title": title})
                break
    print(f"\nROWS TO RELABEL: {len(plan)} (already on the target path: {same})")
    for p in plan[:300]:
        print(f"   {p['sku']} | {p['ws'].title} row {p['n']} | {p['old_cat'][:60] or '-'} [{p['old_cid'] or '-'}] ({tier_text(p['old_cid'], p['old_cat'])}) "
              f"-> {p['new_cat'].split(' > ')[-1]} [{p['new_cid']}] ({tier_text(p['new_cid'], p['new_cat'])}) | Fee % {p['fee'] or '-'} | price {p['price']} | "
              f"stock {p['stock']} | {p['title'][:60]}")
    if not plan:
        print("nothing to write")
        return
    if not APPLY:
        print("DRY RUN - nothing written. Re-run with apply=yes to relabel.")
        return

    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    title, names, k = f"Categories relabelled {stamp}", {w.title for w in book.worksheets()}, 1
    while title in names:
        k += 1
        title = f"Categories relabelled {stamp} ({k})"
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    body = [[p["sku"], p["ws"].title, p["n"], p["old_cat"], p["old_cid"], p["new_cat"], p["new_cid"], now] for p in plan]
    bk = book.add_worksheet(title=title, rows=len(body) + 20, cols=len(BACKUP_HEADER))
    with_retry(lambda: bk.update("A1", [BACKUP_HEADER], value_input_option="RAW"), what="backup header", max_attempts=3)
    for i in range(0, len(body), 500):
        with_retry(lambda ch=body[i:i + 500]: bk.append_rows(ch, value_input_option="RAW"), what="backup rows", max_attempts=3)
    print(f"backup tab {title!r}: {len(body)} row(s)")

    written = skipped = 0
    for ws in {p["ws"] for p in plan}:
        items = [p for p in plan if p["ws"] is ws]
        ix = items[0]["ix"]
        cat_col = ix["Category"] + 1
        fresh = with_retry(lambda ws=ws, c=cat_col: ws.col_values(c), what=f"re-read Category {ws.title}", max_attempts=3)
        updates = []
        for p in items:
            current = fresh[p["n"] - 1] if p["n"] - 1 < len(fresh) else ""
            if str(current).strip() != p["old_cat"]:
                skipped += 1
                print(f"   {p['sku']}: Category changed while this ran ({p['old_cat']!r} -> {current!r}) - left alone")
                continue
            updates.append({"range": f"{col_letter(cat_col)}{p['n']}", "values": [[p["new_cat"]]]})
            if "Category ID" in ix:
                updates.append({"range": f"{col_letter(ix['Category ID'] + 1)}{p['n']}", "values": [[p["new_cid"]]]})
            written += 1
        for i in range(0, len(updates), 1500):
            part = updates[i:i + 1500]
            with_retry(lambda part=part: ws.batch_update(part), what=f"write categories {ws.title}", max_attempts=3)
    print(f"WRITTEN: {written} row(s) relabelled, {skipped} skipped. The sync re-prices them from the new tier at its next visit.")


if __name__ == "__main__":
    main()
