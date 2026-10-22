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
KEYS = (os.getenv("KEYS") or "").strip().lower() in ("1", "true", "yes")
SHOW_VALUE = re.compile(r"categor|commission|tier|fee|rate|sku|opc|quantity|price", re.I)
HIDE = re.compile(r"name|email|address|phone|postcode|tracking|note|message", re.I)


def key_paths(node, prefix=""):
    """Every key path of an order (values shown only for money / category / sku keys, never a buyer field)."""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                out += key_paths(v, path)
            else:
                out.append(f"{path}={v!r}" if SHOW_VALUE.search(str(k)) and not HIDE.search(str(k)) else path)
    elif isinstance(node, list):
        for i, v in enumerate(node[:2]):
            out += key_paths(v, f"{prefix}[{i}]")
    return out
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
    """SKU -> (tab, category id, tier name, lower %, upper %, threshold, category path, Fee % cell) from the sheet, or {} when it cannot be read."""
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
                                                         rule.upper_pct if rule else None, rule.threshold if rule else None,
                                                         cell(r, "OnBuy Category Path") or cell(r, "Category"), cell(r, "Fee %")))
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
            by_sku = {}
            for d in prods:        # OnBuy repeats the product dict inside the order payload: one line per SKU, the fullest dict wins
                key = str(d.get("sku")).strip()
                if key not in by_sku or len(d) > len(by_sku[key]):
                    by_sku[key] = d
            prods = list(by_sku.values())
            skus = [str(d.get("sku")).strip() for d in prods]
            if WANT and not any((s.lstrip("0") or s) in WANT for s in skus):
                continue
            if not WANT and len(prods) != 1:
                continue
            if KEYS and not lines:
                print(f"KEY PATHS of order {o.get('order_id')} ({len(prods)} line(s)):")
                for kp in key_paths(o):
                    print("   " + kp[:160])
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
    charged = {}
    mismatches = []
    extra_fee = 0.0
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
              f"tier {rule[2] if rule else '?'} {rule[3] if rule else ''}{'/' + str(rule[4]) + ' above ' + str(rule[5]) if rule and rule[4] is not None else ''} [{rule[0] if rule else '?'}]"
              + (f" | cat {rule[6][:90]} | sheet Fee % {rule[7]}" if rule else "")
              + (f"  <-- CHARGED {fee_ex / base * 100:.1f}% ex VAT, sheet tier {rule[3]:g}%"
                 if rule and rule[3] is not None and rule[4] is None and fee_ex and base and sub and abs(fee_ex / base * 100 - rule[3]) > 0.6 else ""))
        if rule and rule[3] is not None and rule[4] is None and ln["n_lines"] == 1 and fee_ex and base and sub:
            observed = fee_ex / base * 100
            ratios.setdefault(rule[3], []).append((observed, (fee_inc / base * 100) if fee_inc else None, ln["status"]))
            charged.setdefault(rule[3], {}).setdefault(round(observed), []).append(ln["sku"])
            if abs(observed - rule[3]) > 0.6:
                mismatches.append((rule[6], rule[3], observed, ln["sku"], ln["order"], base))
                if ln["status"] not in ("Cancelled by Seller", "Cancelled by Customer", "Refunded") and observed > rule[3]:
                    extra_fee += (observed - rule[3]) / 100 * base
    if ratios:
        print("SUMMARY - OnBuy's own sales fee as a share of subtotal+delivery, per FLAT tier (nominal -> observed):")
        for nominal, vals in sorted(ratios.items()):
            ex = [v[0] for v in vals]
            inc = [v[1] for v in vals if v[1]]
            print(f"  nominal {nominal:g}%: {len(vals)} order(s) | ex-VAT median {statistics.median(ex):.2f}% (min {min(ex):.2f}, max {max(ex):.2f}) "
                  f"ratio to nominal {statistics.median(ex) / nominal:.3f}"
                  + (f" | inc-VAT median {statistics.median(inc):.2f}% ratio {statistics.median(inc) / nominal:.3f}" if inc else ""))
            print("      charged: " + ", ".join(f"{pct}% x{len(skus)}" for pct, skus in sorted(charged.get(nominal, {}).items())))
    if mismatches:
        print(f"MISMATCHES: {len(mismatches)} single-line order(s) where OnBuy charged a different flat rate than the sheet's category tier | "
              f"extra commission paid on the kept orders: GBP {extra_fee:.2f} ex VAT (x1.2 inc VAT = GBP {extra_fee * 1.2:.2f})")
        by_path = {}
        for path, nominal, observed, sku, order, base in mismatches:
            by_path.setdefault((path, nominal), []).append((round(observed), sku))
        for (path, nominal), items in sorted(by_path.items(), key=lambda kv: -len(kv[1])):
            rates = sorted({r for r, _ in items})
            print(f"   {len(items):3d} order(s) | sheet tier {nominal:g}% | charged {rates} | {path[:100]} | e.g. {', '.join(sorted({s for _, s in items})[:4])}")
    print(f"DONE: scanned {seen} order(s), {len(lines)} line(s) listed, oldest {oldest}")


if __name__ == "__main__":
    main()
