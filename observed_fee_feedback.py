"""Orders -> the commission OnBuy really takes per SKU -> a Fee % override on the rows the sheet under-charges (2026-10-22).

Why: OnBuy's commission follows the category of ITS catalogue product, not the category in our sheet. Two Makstore 3D printers filed under
"Printers & Accessories > Printers" (Consumer Electronics, 7% -> Fee % 8.40) were charged 15% ex VAT (18% inc VAT) on orders T6QR55D and
T6Q6FJQ, so each sale carried about 10% less margin than planned. No API read exposes a product's category, but every order states the
commission OnBuy took - the orders are the ground truth this tool feeds back:
  1. GET /v2/orders back DAYS days (default 90, every status): single-SKU orders with a fee give the observed ex-VAT rate
     (sales_fee_ex_VAT as a share of subtotal + delivery); per SKU the median over its orders,
  2. each SKU is joined to its sheet row (first product tab + Amazon tab) and the row's category tier (fees.py),
  3. where the observed rate exceeds the tier's flat rate by more than 0.6 points, the plan is Fee % = observed rate x 1.2 (the VAT OnBuy adds
     to its commission, as the sync shows it: 15 -> 18.00). decide_price reads a Fee % that is not the automation's own value as a per-row
     override: it drives the price formula and is never overwritten, and even_margins leaves such rows alone. Rows already showing that rate
     (or more) are skipped; a tiered category (two rates) and a row without a tier are reported only,
  4. APPLY=yes writes those Fee % cells (the old values first go to a backup tab "Fee overrides <date>"); otherwise it prints the plan.
Rows where OnBuy charged LESS than the sheet's tier are listed as well (we over-price them) but never lowered here.
Prints no cost cell. Env: APPLY, DAYS (90), MAX_PAGES (30), SHEET_NAME, ONLY_SKUS (comma-separated), MIN_ORDERS (1)."""
import json
import os
import re
import statistics
import time
from datetime import datetime, timedelta, timezone

os.environ.setdefault("FEE_MODE", "category")

import gspread  # noqa: E402
from oauth2client.service_account import ServiceAccountCredentials  # noqa: E402

import fees  # noqa: E402
import pricing  # noqa: E402
import sheet_tabs  # noqa: E402
from onbuy_client import BASE_URL, OnBuyClient  # noqa: E402
from retry_utils import with_retry  # noqa: E402

APPLY = (os.getenv("APPLY") or "").strip().lower() in ("1", "yes", "true")
DAYS = int(os.getenv("DAYS") or "90")
MAX_PAGES = int(os.getenv("MAX_PAGES") or "30")
SHEET_NAME = os.getenv("SHEET_NAME") or "__SHEET__"
ONLY = {s.strip() for s in (os.getenv("ONLY_SKUS") or "").split(",") if s.strip()}
MIN_ORDERS = int(os.getenv("MIN_ORDERS") or "1")
TOLERANCE = 0.6
CANCELLED = ("Cancelled by Seller", "Cancelled by Customer", "Refunded")
BACKUP_HEADER = ["SKU", "Tab", "Row", "Old Fee %", "New Fee %", "Observed ex-VAT %", "Orders", "Category", "Written at (UTC)"]
MONEY_KEY = re.compile(r"(price|fee|commission|vat|tax|total|discount|delivery|quantity|amount|subtotal)", re.I)
NOT_MONEY = re.compile(r"(id\b|_id|reference|capture|transaction|address|name|email|phone|tracking|postcode|country|tag|service)", re.I)


def key(sku):
    s = str(sku or "").strip()
    return s.lstrip("0") or s


def num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("£", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def money_fields(node):
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
    for k in ("date", "created", "created_at", "date_created"):
        v = str(o.get(k) or "").strip()
        if v:
            try:
                return datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                try:
                    return datetime.fromisoformat(v[:19])
                except ValueError:
                    continue
    return None


def col_letter(n):
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def read_orders(client):
    """key(sku) -> [(observed ex-VAT %, status, base, order id, date)] from single-SKU orders with a fee."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=DAYS)
    out, seen, offset, oldest = {}, 0, 0, None
    for page in range(MAX_PAGES):
        params = {"site_id": client.site_id, "limit": 100, "offset": offset, "sort[created]": "desc", "filter[status]": "all"}
        resp = client._send("GET", f"{BASE_URL}/orders", what="orders page", params=params, timeout=90)
        if resp.status_code == 429:
            print("rate limited - waiting 90 s")
            time.sleep(90)
            continue
        if resp.status_code != 200:
            print(f"orders page {page + 1}: HTTP {resp.status_code} {resp.text[:200]}")
            break
        body = resp.json()
        results = body.get("results") if isinstance(body, dict) else body
        if not isinstance(results, list) or not results:
            break
        for o in results:
            seen += 1
            t = order_time(o)
            if t and (oldest is None or t < oldest):
                oldest = t
            by_sku = {}
            for d in walk(o):
                if d.get("sku") is None or not ("name" in d or "quantity" in d or "unit_price" in d):
                    continue
                k = str(d.get("sku")).strip()
                if k not in by_sku or len(d) > len(by_sku[k]):
                    by_sku[k] = d
            if len(by_sku) != 1:
                continue
            sku = next(iter(by_sku))
            top = money_fields(o)
            sub, dlv = top.get("price_subtotal"), top.get("price_delivery") or 0.0
            fee_ex = top.get("sales_fee_ex_VAT", top.get("fee"))
            base = (sub + dlv) if sub else None
            if not fee_ex or not base:
                continue
            out.setdefault(key(sku), []).append((fee_ex / base * 100, str(o.get("status") or ""), base, str(o.get("order_id") or ""),
                                                 str(o.get("date") or "")[:10]))
        print(f"orders page {page + 1}: {seen} order(s) read, oldest {oldest}")
        if (oldest and oldest < cutoff) or len(results) < 100:
            break
        offset += 100
        time.sleep(1.0)
    return out


def read_sheet(book):
    """key(sku) -> row record from the first product tab + the Amazon tab (first occurrence wins)."""
    rows, tabs = {}, [sheet_tabs.product_sheet(book)]
    try:
        tabs.append(book.worksheet(sheet_tabs.AMAZON_TAB))
    except Exception as exc:  # noqa: BLE001 - a store without an Amazon tab
        print(f"(no Amazon tab: {str(exc)[:80]})")
    for ws in tabs:
        values = with_retry(lambda ws=ws: ws.get_all_values(), what=f"read {ws.title}", max_attempts=3)
        if not values:
            continue
        header = [str(h).strip() for h in values[0]]
        ix = {h: i for i, h in enumerate(header) if h}
        if "SKU" not in ix or "Fee %" not in ix:
            print(f"tab {ws.title!r} lacks SKU / Fee % columns - skipped")
            continue

        def cell(r, k):
            return str(r[ix[k]]).strip() if k in ix and ix[k] < len(r) else ""
        for n, r in enumerate(values[1:], start=2):
            sku = cell(r, "SKU")
            if not sku:
                continue
            rows.setdefault(key(sku), {"ws": ws, "ix": ix, "n": n, "sku": sku, "cat": cell(r, "Category"), "cid": cell(r, "Category ID"),
                                       "fee_cell": cell(r, "Fee %"), "price": num(cell(r, "Selling Price (£)")), "stock": num(cell(r, "Stock")),
                                       "profit_cell": cell(r, "Profit %")})
    return rows


def main():
    client = OnBuyClient()
    if not client.authenticate():
        raise SystemExit("OnBuy auth failed")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    orders = read_orders(client)
    rows = read_sheet(book)
    print(f"{'APPLY' if APPLY else 'DRY RUN'} | {DAYS} days | SKUs with usable orders: {len(orders)} | sheet rows: {len(rows)}")

    plan, over, tiered, no_tier, already, ok, not_in_sheet = [], [], [], [], [], 0, 0
    extra_paid = 0.0
    for k, obs in sorted(orders.items()):
        if ONLY and k not in {key(s) for s in ONLY}:
            continue
        if len(obs) < MIN_ORDERS:
            continue
        row = rows.get(k)
        if row is None:
            not_in_sheet += 1
            continue
        observed = statistics.median(v[0] for v in obs)
        obs_r = round(observed * 2) / 2          # OnBuy's rates are whole or half points; pennies round the share
        rule = fees.rule_for_category_id(row["cid"]) or fees.rule_for_category_path(row["cat"])
        kept = [v for v in obs if v[1] not in CANCELLED]
        rec = {"row": row, "observed": obs_r, "orders": len(obs), "kept": len(kept), "rule": rule,
               "ids": ", ".join(sorted({v[3] for v in obs})[:3])}
        if rule is None:
            no_tier.append(rec)
            continue
        if rule.upper_pct is not None:
            tiered.append(rec)
            continue
        diff = obs_r - rule.lower_pct
        if diff > TOLERANCE:
            target = pricing.effective_rate(obs_r)
            rec["target"] = f"{target:.2f}"
            rec["extra"] = sum((v[0] - rule.lower_pct) / 100 * v[2] for v in kept if v[0] > rule.lower_pct)
            extra_paid += rec["extra"]
            cur = num(row["fee_cell"])
            if cur is not None and cur >= target - 0.05:
                already.append(rec)
            else:
                plan.append(rec)
        elif diff < -TOLERANCE:
            over.append(rec)
        else:
            ok += 1

    def line(rec, arrow=True):
        r, rule = rec["row"], rec["rule"]
        tier = f"{rule.name} {rule.lower_pct:g}%" + (f"/{rule.upper_pct:g}% above {rule.threshold:g}" if rule and rule.upper_pct is not None else "") if rule else "no tier"
        return (f"   {r['sku']} | {r['ws'].title} row {r['n']} | {r['cat'][:60] or '-'} | sheet tier {tier} | OnBuy charged {rec['observed']:g}% ex VAT "
                f"({rec['orders']} order(s), {rec['kept']} kept; e.g. {rec['ids']}) | Fee % {r['fee_cell'] or '-'}"
                + (f" -> {rec['target']}" if arrow and rec.get("target") else "") + f" | price {r['price']} | stock {r['stock']:g}" if r["stock"] is not None else "")

    print(f"\nUNDER-CHARGED - OnBuy takes more than the sheet's tier: {len(plan)} row(s) to override" + (f" (+{len(already)} already overridden)" if already else ""))
    for rec in sorted(plan, key=lambda x: -x.get("extra", 0))[:150]:
        print(line(rec))
    print(f"   extra commission already paid on the kept orders of these SKUs: GBP {extra_paid:.2f} ex VAT (GBP {extra_paid * 1.2:.2f} inc VAT)")
    if over:
        print(f"\nOVER-CHARGED by the sheet - OnBuy takes less than the tier (prices could be lower; nothing written): {len(over)} row(s)")
        for rec in over[:40]:
            print(line(rec, arrow=False))
    if tiered:
        print(f"\nTIERED categories (two rates; not compared): {len(tiered)}")
        for rec in tiered[:15]:
            print(line(rec, arrow=False))
    if no_tier:
        print(f"\nNO TIER on the row (category missing or unknown): {len(no_tier)}")
        for rec in no_tier[:15]:
            print(line(rec, arrow=False))
    print(f"\nrows matching their tier: {ok} | ordered SKUs not in the sheet (team / legacy listings): {not_in_sheet}")

    if not plan:
        print("nothing to write")
        return
    if not APPLY:
        print("DRY RUN - nothing written. Re-run with apply=yes to write the Fee % overrides.")
        return

    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    title, names, n = f"Fee overrides {stamp}", {w.title for w in book.worksheets()}, 1
    while title in names:
        n += 1
        title = f"Fee overrides {stamp} ({n})"
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    body = [[rec["row"]["sku"], rec["row"]["ws"].title, rec["row"]["n"], rec["row"]["fee_cell"], rec["target"], f"{rec['observed']:g}",
             rec["orders"], rec["row"]["cat"], now] for rec in plan]
    bk = book.add_worksheet(title=title, rows=len(body) + 20, cols=len(BACKUP_HEADER))
    with_retry(lambda: bk.update("A1", [BACKUP_HEADER], value_input_option="RAW"), what="backup header", max_attempts=3)
    for i in range(0, len(body), 500):
        with_retry(lambda ch=body[i:i + 500]: bk.append_rows(ch, value_input_option="RAW"), what="backup rows", max_attempts=3)
    print(f"backup tab {title!r}: {len(body)} row(s)")

    written = skipped = 0
    for ws in {rec["row"]["ws"] for rec in plan}:
        recs = [rec for rec in plan if rec["row"]["ws"] is ws]
        ix = recs[0]["row"]["ix"]
        col = ix["Fee %"] + 1
        fresh = with_retry(lambda ws=ws, col=col: ws.col_values(col), what=f"re-read Fee % {ws.title}", max_attempts=3)
        updates = []
        for rec in recs:
            r = rec["row"]
            current = fresh[r["n"] - 1] if r["n"] - 1 < len(fresh) else ""
            if str(current).strip() != r["fee_cell"]:
                skipped += 1
                print(f"   {r['sku']}: Fee % changed while this ran ({r['fee_cell']!r} -> {current!r}) - left alone")
                continue
            updates.append({"range": f"{col_letter(col)}{r['n']}", "values": [[rec["target"]]]})
        for i in range(0, len(updates), 1500):
            part = updates[i:i + 1500]
            with_retry(lambda part=part: ws.batch_update(part), what=f"write Fee % {ws.title}", max_attempts=3)
        written += len(updates)
    print(f"WRITTEN: {written} Fee % override(s), {skipped} skipped. The sync re-prices these rows from the new fee at its next visit "
          f"(or run the audit to push them now).")


if __name__ == "__main__":
    main()
