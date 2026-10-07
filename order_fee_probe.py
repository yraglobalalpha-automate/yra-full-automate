"""READ-ONLY (2026-10-07): what did OnBuy actually CHARGE on recent orders, against what our price formula assumes?

Pages GET /v2/orders (newest first, STATUS default "all") back DAYS days and prints, per order line, ONLY money fields - never a buyer
field: order id, status, date, SKU, quantity, the order's subtotal / delivery / discount / total and OnBuy's own `fee`, plus the
fee as a share of each base. When the sheet is readable it adds the SKU's category tier (the NOMINAL commission the formula starts
from) so the report can say what OnBuy deducts on top of it. SKUS (comma-separated, optional) limits the lines printed; without it
every single-line order is listed and a per-tier summary is printed at the end.

Writes nothing anywhere. Env: DAYS (30), MAX_PAGES (30), STATUS (all), SKUS, SHEET_NAME, FEE_MODE=category (set by the workflow).
"""
import json
import os
import re
import statistics
import time
from datetime import datetime, timedelta, timezone

from onbuy_client import BASE_URL, OnBuyClient

DAYS = int(os.getenv("DAYS") or "30")
MAX_PAGES = int(os.getenv("MAX_PAGES") or "30")
STATUS = (os.getenv("STATUS") or "all").strip()
WANT = {s.strip().lstrip("0") or s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()}
SHEET_NAME = os.getenv("SHEET_NAME") or "OnBuy_Feed_Master"
MONEY_KEY = re.compile(r"(price|fee|commission|vat|tax|total|discount|delivery|quantity|amount|subtotal)", re.I)
NOT_MONEY = re.compile(r"(id\b|_id|reference|capture|transaction|address|name|email|phone|tracking|postcode|country|tag|service)", re.I)


def num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def money_fields(node):
    """{key: number} for the money-like numeric keys of one dict (never ids, names, addresses)."""
    out = {}
    for k, v in (node or {}).items():
        if isinstance(v, (dict, list)) or not MONEY_KEY.search(str(k)) or NOT_MONEY.search(str(k)):
            continue
        n = num(v)
        if n is not None:
            out[k] = n
    return out


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def order_time(o):
    for k in ("date", "created", "created_at", "order_date"):
        v = o.get(k)
        if isinstance(v, str) and v[:4].isdigit():
            try:
                return datetime.strptime(v[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
    return None


def sheet_rules():
    """SKU -> (tab, category id, tier name, lower %, upper %, threshold) from the sheet, or {} when it cannot be read."""
    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
        import fees
        import sheet_tabs
        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            json.loads(os.environ["GOOGLE_CREDENTIALS"]),
            ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
        book = gspread.authorize(creds).open(SHEET_NAME)
        out = {}
        for ws in (sheet_tabs.product_sheet(book), book.worksheet(sheet_tabs.AMAZON_TAB)):
            values = ws.get_all_values()
            header = [str(h).strip() for h in values[0]]
            ix = {h: i for i, h in enumerate(header) if h}

            def cell(r, k):
                return str(r[ix[k]]).strip() if k in ix and ix[k] < len(r) else ""
            for r in values[1:]:
                sku = cell(r, "SKU")
                if not sku:
                    continue
                rule = fees.rule_for_category_id(cell(r, "Category ID")) or fees.rule_for_category_path(cell(r, "Category"))
                out.setdefault(sku.lstrip("0") or sku, (ws.title, cell(r, "Category ID"),
                                                         rule.name if rule else "", rule.lower_pct if rule else None,
                                                         rule.upper_pct if rule else None, rule.threshold if rule else None))
        return out
    except Exception as exc:  # noqa: BLE001 - the money lines are still useful without the tiers
        print(f"(sheet/tier lookup unavailable: {str(exc)[:120]})")
        return {}


def main():
    os.environ.setdefault("FEE_MODE", "category")
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    rules = sheet_rules()
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=DAYS)
    seen = 0
    oldest = None
    lines = []
    offset = 0
    for page in range(MAX_PAGES):
        params = {"site_id": onbuy.site_id, "limit": 100, "offset": offset, "sort[created]": "desc"}
        if STATUS:
            params["filter[status]"] = STATUS
        resp = onbuy._send("GET", f"{BASE_URL}/orders", what="orders page", params=params, timeout=90)
        if resp.status_code == 429:
            print("rate limited - waiting 90s")
            time.sleep(90)
            continue
        if resp.status_code != 200:
            print(f"HTTP {resp.status_code}: {resp.text[:300]}")
            break
        body = resp.json()
        results = body.get("results") if isinstance(body, dict) else body
        if not isinstance(results, list) or not results:
            break
        if page == 0 and isinstance(results[0], dict):
            print("order money keys:", sorted(money_fields(results[0])))
            prods = [d for d in walk(results[0]) if d.get("sku") is not None]
            if prods:
                print("product-line money keys:", sorted(money_fields(prods[0])))
        for o in results:
            seen += 1
            t = order_time(o)
            if t and (oldest is None or t < oldest):
                oldest = t
            prods = [d for d in walk(o) if d.get("sku") is not None and ("name" in d or "quantity" in d or "unit_price" in d)]
            skus = [str(d.get("sku")).strip() for d in prods]
            if WANT and not any((s.lstrip("0") or s) in WANT for s in skus):
                continue
            if not WANT and len(prods) != 1:
                continue
            top = money_fields(o)
            for d in prods:
                sku = str(d.get("sku")).strip()
                if WANT and (sku.lstrip("0") or sku) not in WANT:
                    continue
                lines.append({"order": o.get("order_id"), "status": o.get("status"), "date": str(o.get("date"))[:10], "sku": sku,
                              "line": money_fields(d), "order_money": top, "n_lines": len(prods)})
        print(f"page {page + 1}: scanned {seen} order(s), oldest {oldest}")
        if oldest and oldest < cutoff:
            break
        if len(results) < 100:
            break
        offset += 100
        time.sleep(1.0)

    ratios = {}
    for ln in lines:
        top, row = ln["order_money"], ln["line"]
        sub = top.get("price_subtotal")
        dlv = top.get("price_delivery") or 0.0
        disc = top.get("price_discount") or 0.0
        tot = top.get("price_total")
        fee_ex = top.get("sales_fee_ex_VAT", top.get("fee"))
        fee_inc = top.get("sales_fee_inc_VAT")
        rule = rules.get(ln["sku"].lstrip("0") or ln["sku"])
        base = (sub + dlv) if sub else None

        def share(x, b=base):
            return None if x is None or not b else round(x / b * 100, 2)
        print(f"ORDER {ln['order']} | {ln['status']} | {ln['date']} | sku {ln['sku']} | lines {ln['n_lines']} | "
              f"subtotal {sub} delivery {top.get('price_delivery')} discount {top.get('price_discount')} total {tot} | "
              f"sales_fee_ex_VAT {fee_ex} ({share(fee_ex)}% of subtotal+delivery) sales_fee_inc_VAT {fee_inc} ({share(fee_inc)}%) | "
              f"line {json.dumps(row, sort_keys=True)} | "
              f"tier {rule[2] if rule else '?'} {rule[3] if rule else ''}{'/' + str(rule[4]) + ' above ' + str(rule[5]) if rule and rule[4] is not None else ''} [{rule[0] if rule else '?'}]")
        if rule and rule[3] is not None and rule[4] is None and ln["n_lines"] == 1 and fee_ex and base and sub:
            ratios.setdefault(rule[3], []).append((fee_ex / base * 100, (fee_inc / base * 100) if fee_inc else None, ln["status"]))
    if ratios:
        print("SUMMARY - OnBuy's own sales fee as a share of subtotal+delivery, per FLAT tier (nominal -> observed):")
        for nominal, vals in sorted(ratios.items()):
            ex = [v[0] for v in vals]
            inc = [v[1] for v in vals if v[1]]
            print(f"  nominal {nominal:g}%: {len(vals)} order(s) | ex-VAT median {statistics.median(ex):.2f}% (min {min(ex):.2f}, max {max(ex):.2f}) "
                  f"ratio to nominal {statistics.median(ex) / nominal:.3f}"
                  + (f" | inc-VAT median {statistics.median(inc):.2f}% ratio {statistics.median(inc) / nominal:.3f}" if inc else ""))
    print(f"DONE: scanned {seen} order(s), {len(lines)} line(s) listed, oldest {oldest}")


if __name__ == "__main__":
    main()
