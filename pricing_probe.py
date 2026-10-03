"""READ-ONLY (2026-10-02): how will the sync price each row? Runs the sync's REAL decision
(generate_xml.decide_price) over the sheet's cells and the Supabase mirror, without running the sync.

ROWS = row numbers / ranges ("6294,6298" or "10-20") for per-row detail, or `all` for whole-tab
aggregates: how many Fee % / Profit % cells still read as manual overrides, how many prices the
sync would move (down / up, by how much) and why. TAB = worksheet (default the first product tab).

Prints prices and percentages only - never a cost (the repositories, and so their run logs, are
public). Writes nothing anywhere.
"""
import json
import os
import re
import statistics

os.environ.setdefault("FEE_MODE", "category")

import gspread  # noqa: E402
from oauth2client.service_account import ServiceAccountCredentials  # noqa: E402

import fees  # noqa: E402
import pricing  # noqa: E402
import sheet_tabs  # noqa: E402
import supabase_db  # noqa: E402
from generate_xml import _pct_value, _shipping_value, _to_float, decide_price  # noqa: E402

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
TAB = (os.getenv("SHEET_TAB") or "").strip()
ROWS = (os.getenv("ROWS") or "").strip()
LIST_UP = (os.getenv("LIST_UP") or "").strip().lower() in ("1", "yes", "true")
CHUNK = 150


def parse_rows(spec):
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))] if values else 0.0


def decision(cells, mirror, amazon):
    """The sync's decision for one row, from the sheet's cells (None when the row has no cost)."""
    cost = _to_float(cells("Cost Price (£)"))
    if cost <= 0:
        return None
    rule = fees.rule_for_category_path(cells("Category")) if fees.enabled() else None
    existing = _to_float(cells("Selling Price (£)"))
    d = decide_price(supplier="Amazon" if amazon else "eBay", cost_price=cost,
                     shipping_cost=_shipping_value(cells("Shipping Cost (£)")), fee_rule=rule,
                     existing_price=existing, fee_cell=cells("Fee %"), profit_cell=cells("Profit %"), prev=mirror)
    d["rule"], d["existing"] = rule, existing
    d["cost"], d["ship"] = cost, _shipping_value(cells("Shipping Cost (£)"))
    d["prev_cost"], d["prev_ship"] = _to_float(mirror.get("Cost Price (£)")), _to_float(mirror.get("Shipping Cost (£)"))
    d["fee_cell"] = cells("Fee %")
    return d


def band_of(total, rule):
    fn = getattr(pricing, "profit_percent_for", None)              # GTV's band depends on the row's fee rule
    return fn(total, rule=rule) if fn else pricing.profit_percent(total)


def legacy_of(total, rule):
    try:
        return list(pricing.legacy_profit_percents(total, rule=rule))
    except TypeError:
        return list(pricing.legacy_profit_percents(total))


def matched(d):
    """Which formula the sheet's CURRENT price equals - (cost basis, profit %, fee reading) - for the big-mover breakdown."""
    out = []
    typed = _pct_value(d["fee_cell"])
    for label, c, sh in (("now", d["cost"], d["ship"]), ("mirror", d["prev_cost"], d["prev_ship"])):
        if c <= 0:
            continue
        t = c + sh
        for p in [band_of(t, d["rule"])] + legacy_of(t, d["rule"]):
            if typed is not None and abs(pricing.price_for_profit(t, p, platform_fee_percent=typed) - d["existing"]) < 0.011:
                out.append(f"{label} cost, {p:g}% profit, misread fee")
            if abs(pricing.price_for_profit(t, p, rule=d["rule"]) - d["existing"]) < 0.011:
                out.append(f"{label} cost, {p:g}% profit, category rule")
    return out or ["no formula matches"]


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(SHEET_NAME)
    ws = book.worksheet(TAB) if TAB else sheet_tabs.product_sheet(book)
    amazon = ws.title == sheet_tabs.AMAZON_TAB
    values = ws.get_all_values()
    header = [str(h).strip() for h in values[0]]
    ix = {h: i for i, h in enumerate(header) if h}
    print(f"tab {ws.title!r} ({'Amazon' if amazon else 'eBay'} pricing) | top band profit now {pricing.TOP_BAND_PROFIT:g}% "
          f"| uplift {pricing.FEE_UPLIFT_PERCENT:g} points | fee mode {'category' if fees.enabled() else 'flat'}"
          if hasattr(pricing, "TOP_BAND_PROFIT") else
          f"tab {ws.title!r} ({'Amazon' if amazon else 'eBay'} pricing) | uplift {pricing.FEE_UPLIFT_PERCENT:g} points "
          f"| fee mode {'category' if fees.enabled() else 'flat'}")

    def cells_of(r):
        return lambda k: (str(r[ix[k]]).strip() if k in ix and ix[k] < len(r) else "")

    if ROWS.lower() == "all":
        sku_rows = [(n, r) for n, r in enumerate(values[1:], start=2) if cells_of(r)("SKU")]
        mirror = {}
        skus = [cells_of(r)("SKU") for _, r in sku_rows]
        for i in range(0, len(skus), CHUNK):
            try:
                mirror.update(supabase_db.fetch_existing_fields(skus[i:i + CHUNK]))
            except Exception as exc:  # noqa: BLE001 - the preview still works without the mirror, just less exactly
                print(f"mirror read failed for a chunk ({str(exc)[:100]}) - those rows are previewed without it")
        how, misread, fee_ovr, profit_ovr, down, up, human = {}, 0, 0, 0, [], [], 0
        priced = 0
        fee_vals, profit_vals = {}, {}
        big, up_rows = [], []
        for n, r in sku_rows:
            c = cells_of(r)
            d = decision(c, mirror.get(c("SKU"), {}), amazon)
            if d is None:
                continue
            priced += 1
            how[d["how"]] = how.get(d["how"], 0) + 1
            misread += bool(d["misread"])
            fee_ovr += d["fee_override"] is not None
            profit_ovr += d["profit_override"] is not None
            if d["fee_override"] is not None:
                fee_vals[d["fee_override"]] = fee_vals.get(d["fee_override"], 0) + 1
            if d["profit_override"] is not None:
                profit_vals[d["profit_override"]] = profit_vals.get(d["profit_override"], 0) + 1
            if d["existing"] > 0:
                change = (d["selling_price"] / d["existing"] - 1) * 100
                if change < -8.0:
                    big.append((n, c("SKU"), d, change))
                if change < -0.05:
                    down.append(change)
                elif change > 0.05:
                    up.append(change)
                    if change > 5.0:
                        up_rows.append(n)
                if d["how"] == "kept" and d["existing"] > d["formula_price"] + 0.011:
                    human += 1
        print(f"rows with a cost: {priced} | decisions {how} | prices set under the Fee % misread: {misread}")
        print(f"cells that still read as a MANUAL override: Fee % {fee_ovr}, Profit % {profit_ovr}")
        for label, vals in (("Fee %", fee_vals), ("Profit %", profit_vals)):
            top = sorted(vals.items(), key=lambda kv: -kv[1])[:10]
            print(f"  most common typed {label} values (value: rows): {dict(top)}")
        print(f"prices kept ABOVE the formula (taken as set by a person): {human}")
        if down:
            print(f"prices that move DOWN: {len(down)} | mean {statistics.mean(down):.2f}%, median {statistics.median(down):.2f}%, "
                  f"p10 {pct(down, 0.1):.2f}%, min {min(down):.2f}%")
        if big:
            labels, ratios = {}, []
            for _, _, d, _ in big:
                for lab in matched(d)[:2]:
                    labels[lab] = labels.get(lab, 0) + 1
                if d["prev_cost"] > 0:
                    ratios.append(d["cost"] / d["prev_cost"])
            print(f"BIG DROPS (more than 8% down): {len(big)} | the price equals: {dict(sorted(labels.items(), key=lambda kv: -kv[1])[:8])}")
            if ratios:
                print(f"  cost now / cost the mirror saw: min {min(ratios):.2f}, median {statistics.median(ratios):.2f}, "
                      f"max {max(ratios):.2f} (over {len(ratios)} rows with a mirror cost)")
            for n, sku, d, change in sorted(big, key=lambda b: b[3])[:8]:
                ratio = f"{d['cost'] / d['prev_cost']:.2f}" if d["prev_cost"] > 0 else "no mirror cost"
                print(f"  row {n} SKU {sku}: price {d['existing']:.2f} -> {d['selling_price']:.2f} ({change:.1f}%) | decision {d['how']} | "
                      f"band now {d['band_now']} | cost ratio {ratio} | Fee % cell {d['fee_cell']!r} | rule {d['rule']!r} | "
                      f"matches: {matched(d)[:2]}")
        if LIST_UP and up_rows:
            print("ROWS THAT MOVE UP BY MORE THAN 5%: " + ",".join(str(n) for n in up_rows))
        if up:
            print(f"prices that move UP: {len(up)} | mean +{statistics.mean(up):.2f}%, median +{statistics.median(up):.2f}%, "
                  f"p90 +{pct(up, 0.9):.2f}%, max +{max(up):.2f}%")
        return

    for n in parse_rows(ROWS):
        if n - 1 >= len(values):
            continue
        r = values[n - 1]
        c = cells_of(r)
        sku = c("SKU")
        mirror = supabase_db.fetch_existing_fields([sku]).get(sku, {}) if sku else {}
        d = decision(c, mirror, amazon)
        if d is None:
            print(f"row {n} SKU {sku}: no cost - nothing to price")
            continue
        item = re.search(r"/itm/(\d+)", c("Supplier URL")) or re.search(r"/dp/([A-Z0-9]{10})", c("Supplier URL"))
        ratio = f"{d['cost'] / d['prev_cost']:.2f}" if d["prev_cost"] > 0 else "n/a"
        print(f"  supplier item {item.group(1) if item else '?'} | cost now / cost the mirror saw: {ratio} | title: {c('Title')[:70]!r}")
        print(f"row {n} SKU {sku}: cells Fee % {c('Fee %')!r} Profit % {c('Profit %')!r} | mirror Fee % {mirror.get('Fee %')!r} "
              f"Profit % {mirror.get('Profit %')!r} | rule {d['rule']!r} | read as: fee override {d['fee_override']}, "
              f"profit override {d['profit_override']} | decision {d['how']}"
              f"{' (price set under the Fee % misread)' if d['misread'] else ''} | price now {d['existing']:.2f} -> "
              f"{d['selling_price']:.2f} (formula {d['formula_price']:.2f})")


if __name__ == "__main__":
    main()
