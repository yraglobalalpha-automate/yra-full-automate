"""Ported from Makstore 2026-10-23 for the dashboard's pricing card. Apply the profit schedule EVENLY to every priced row (user 2026-10-12: "the old already listed listings ... have more margins than we fixed ...
I want the profit margins to be applied evenly on all the product rows in Makstore and Arden").

Why rows were uneven: the first tab (eBay) never LOWERS a price that no formula explains (decide_price: max(existing, formula) - the never-lower rule of 2026-08), so every
old listing priced under an earlier schedule kept its old, higher margin for good (row 32 showed 74% profit where the schedule says 25%), and rows not yet visited
since a band change (15 -> 25 for cost + shipping above GBP 50) still sat at the old level. The sync itself keeps that rule (a price a person raises on purpose
still sticks); this tool is the ONE-OFF that puts every row on the schedule now.

For every row with a cost it asks the sync's own decision (pricing_probe.decision -> generate_xml.decide_price) what the formula price is and acts on the Selling Price:
  lower  the price is above the formula (and below OUTLIER_RATIO x it)       -> formula price
  raise  the price is below the formula                                       -> formula price
  hold   the price is OUTLIER_RATIO x the formula or more (a cost misread or a price typo - lowering it could sell at a loss): listed, never touched
  ok     already on the formula (within a penny)
  skip   no cost / no price, a typed Profit % or Fee % (the row's own override stays exactly as typed), a frozen wrong-content listing (protected_skus.txt),
         an Amazon-tab price raised by hand (the user's 2026-10-07 rule: it stays)
A Buy Box-defended SKU (BuyBox tab) is evened like any other row: only its SHEET price moves; the defense keeps repricing the live listing below it as before.
It writes Selling Price (£), Profit % and Fee % exactly like the sync does (so the next visit reads them as its own, never as overrides) and the Price Check Flag.
OnBuy follows through the audit (audit_price_drift fix=yes pushes the sheet's price onto drifted listings) - this tool touches the Sheet only.

APPLY=yes writes; anything else is a dry run (prints aggregates + the holds, writes nothing). Before writing, every changed row's old values go to a backup tab
"Prices evened <date>". MAX_CHANGES (default 800) stops a run whose plan is unexpectedly big. Prints prices and percentages only - never a cost (public logs).
Env: APPLY, TABS (comma-separated, default: the first product tab + Amazon), OUTLIER_RATIO (default 2.0), MAX_CHANGES, SHEET_NAME (via pricing_probe).
"""
import json
import os
import statistics
import sys
import time
from collections import Counter

os.environ.setdefault("FEE_MODE", "category")

import gspread  # noqa: E402
from oauth2client.service_account import ServiceAccountCredentials  # noqa: E402

import held_skus  # noqa: E402
import pricing  # noqa: E402
import sheet_tabs  # noqa: E402
import supabase_db  # noqa: E402
from generate_xml import _pct_text, drop_edited_cells  # noqa: E402  - the sync's own helpers
from pricing_probe import SHEET_NAME, decision, matched  # noqa: E402  - the sync's own decision, read off the sheet's cells
from retry_utils import with_retry  # noqa: E402

APPLY = (os.getenv("APPLY") or "").strip().lower() in ("1", "yes", "true")
OUTLIER_RATIO = float(os.getenv("OUTLIER_RATIO") or "2.0")
MAX_CHANGES = int(os.getenv("MAX_CHANGES") or "800")
TOL = 0.011                      # the sync's own "same price" tolerance
CHUNK = 150
GUARDED = ("Selling Price (£)", "Profit %", "Fee %")
BACKUP_HEADER = ["Tab", "Row", "SKU", "Title", "Action", "Old Selling Price (£)", "New Selling Price (£)", "Ratio old/new", "Old Profit %", "New Profit %",
                 "Old Fee %", "New Fee %", "Stock", "Sync Status", "Note"]


def col_letter(n):
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def plan_row(d, *, amazon=False, frozen=False, outlier_ratio=OUTLIER_RATIO, tol=TOL):
    """(action, note) for one row, from the sync's decision `d` (None = the row has no cost). See the module docstring."""
    if d is None:
        return "skip", "no cost"
    if frozen:
        return "skip", "frozen wrong-content listing"
    if d["profit_override"] is not None or d["fee_override"] is not None:
        return "skip", "typed Profit % / Fee % override"
    formula, existing = d["formula_price"], d["existing"]
    if formula <= 0 or existing <= 0:
        return "skip", "no price to compare"
    if abs(existing - formula) <= tol:
        return "ok", ""
    if existing > formula:
        if amazon and d["how"] == "manual":
            return "skip", "Amazon price raised by hand (kept, user 2026-10-07)"
        ratio = existing / formula
        if ratio >= outlier_ratio:
            return "hold", f"x{ratio:.2f} the schedule - a cost misread or a price typo, review before lowering"
        return "lower", f"x{ratio:.3f}"
    return "raise", f"x{existing / formula:.3f}"


def new_cells(d, stock):
    """The cells the sync itself would write for a row priced exactly at the formula: price, Profit % (the band's), Fee % (the real
    effective rate at that price) and the Price Check Flag (Normal for a row in stock - the price IS the formula)."""
    price = round(d["formula_price"], 2)
    return {"Selling Price (£)": price,
            "Profit %": _pct_text(d["band_now"]),
            "Fee %": f"{pricing.effective_fee_percent(price, d['rule']):.2f}",
            "Price Check Flag": "" if stock == 0 else "Normal"}


def _num(text):
    s = str(text if text is not None else "").replace("£", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _bucket(ratio):
    for hi, label in ((1.05, "under 5% off"), (1.10, "5-10%"), (1.25, "10-25%"), (1.5, "25-50%"), (2.0, "50-100%")):
        if ratio < hi:
            return label
    return "100%+"


def read_defended(book):
    """SKUs on the BuyBox tab (row 1 header, row 2 the run-summary meta row, SKUs from row 3), as the audit reads them."""
    try:
        bb = book.worksheet("BuyBox")
    except gspread.exceptions.WorksheetNotFound:
        return set()
    except Exception as exc:  # noqa: BLE001 - advisory, never fatal
        print(f"BuyBox tab read failed ({str(exc)[:100]}) - defended-SKU exclusion off")
        return set()
    out = {str(v).replace(",", "").strip() for v in bb.col_values(1)[2:]}
    out.discard("")
    return out


def analyse_tab(ws, defended, frozen):
    """Plan every row of one tab. Returns (rows, snapshot): rows = [{n, sku, title, action, note, d, stock, status, cells}], snapshot = (header index, values)."""
    amazon = ws.title == sheet_tabs.AMAZON_TAB
    values = ws.get_all_values()
    header = [str(h).strip() for h in values[0]]
    ix = {h: i for i, h in enumerate(header) if h}

    def cells_of(r):
        return lambda k: (str(r[ix[k]]).strip() if k in ix and ix[k] < len(r) else "")

    sku_rows = [(n, r) for n, r in enumerate(values[1:], start=2) if cells_of(r)("SKU")]
    mirror = {}
    skus = [cells_of(r)("SKU") for _, r in sku_rows]
    for i in range(0, len(skus), CHUNK):
        try:
            mirror.update(supabase_db.fetch_existing_fields(skus[i:i + CHUNK]))
        except Exception as exc:  # noqa: BLE001 - the plan still works without the mirror, just less exactly
            print(f"mirror read failed for a chunk ({str(exc)[:100]}) - those rows are planned without it")
    out = []
    for n, r in sku_rows:
        c = cells_of(r)
        sku = c("SKU")
        d = decision(c, mirror.get(sku, {}), amazon)
        action, note = plan_row(d, amazon=amazon, frozen=held_skus.is_held(sku, frozen))
        is_defended = sku.replace(",", "").strip() in defended
        out.append({"n": n, "sku": sku, "title": c("Title"), "action": action, "note": note + (" | Buy Box-defended" if is_defended and action != "skip" else ""),
                    "defended": is_defended, "d": d,
                    "stock": int(_num(c("Stock"))), "status": c("Sync Status"),
                    "old": {k: c(k) for k in ("Selling Price (£)", "Profit %", "Fee %", "Price Check Flag")}})
    return out, (ix, values)


def report(ws_title, rows, amazon):
    acts = Counter(r["action"] for r in rows)
    print(f"\n=== {ws_title} ({'Amazon' if amazon else 'eBay'} pricing): {len(rows)} rows with a SKU | " + ", ".join(f"{k} {v}" for k, v in sorted(acts.items())))
    skips = Counter(r["note"] for r in rows if r["action"] == "skip")
    for note, n in skips.most_common():
        extra = ""
        if note == "no cost":
            # rows with nothing to price from; the ones that are IN STOCK WITH A PRICE are live on the sheet's say-so and their margin cannot be checked
            live = sum(1 for r in rows if r["action"] == "skip" and r["note"] == "no cost" and r["stock"] > 0 and _num(r["old"]["Selling Price (£)"]) > 0)
            extra = f" ({live} of them in stock with a price - their margin cannot be checked)"
        print(f"  skip: {n} x {note}{extra}")
    for act in ("lower", "raise"):
        sel = [r for r in rows if r["action"] == act]
        if not sel:
            continue
        ratios = [(r["d"]["existing"] / r["d"]["formula_price"] if act == "lower" else r["d"]["formula_price"] / r["d"]["existing"]) for r in sel]
        buckets = Counter(_bucket(x) for x in ratios)
        live = sum(1 for r in sel if r["stock"] > 0)
        defended_n = sum(1 for r in sel if r.get("defended"))
        print(f"  {act.upper()}: {len(sel)} rows ({live} in stock, {defended_n} Buy Box-defended) | median {act} {abs(statistics.median(ratios) - 1) * 100:.1f}%, "
              f"mean {abs(statistics.mean(ratios) - 1) * 100:.1f}%, max {abs(max(ratios) - 1) * 100:.1f}% | by size: {dict(buckets)}")
        if act == "lower":
            labels = Counter(matched(r["d"])[0] for r in sel)
            print(f"    what the old price equals: {dict(labels.most_common(6))}")
            for r in sorted(sel, key=lambda r: -(r["d"]["existing"] / r["d"]["formula_price"]))[:8]:
                d = r["d"]
                print(f"    e.g. row {r['n']} SKU {r['sku']} {r['title'][:38]!r}: {d['existing']:.2f} -> {d['formula_price']:.2f} "
                      f"(band {d['band_now']:g}%, was showing {r['old']['Profit %']!r}, stock {r['stock']})")
    for r in [r for r in rows if r["action"] == "hold"][:60]:
        d = r["d"]
        print(f"  HOLD row {r['n']} SKU {r['sku']} {r['title'][:44]!r}: {d['existing']:.2f} vs schedule {d['formula_price']:.2f} ({r['note']}) | stock {r['stock']} | {r['status'][:30]!r}")


def backup_rows(ws_title, rows):
    out = []
    for r in rows:
        if r["action"] not in ("lower", "raise", "hold"):
            continue
        d = r["d"]
        nc = new_cells(d, r["stock"]) if r["action"] != "hold" else {}
        out.append([ws_title, r["n"], r["sku"], r["title"][:90], r["action"], r["old"]["Selling Price (£)"], nc.get("Selling Price (£)", ""),
                    f"{d['existing'] / d['formula_price']:.3f}", r["old"]["Profit %"], nc.get("Profit %", ""), r["old"]["Fee %"], nc.get("Fee %", ""),
                    r["stock"], r["status"][:60], r["note"]])
    return out


def apply_tab(ws, rows, snapshot):
    """Write the changed rows' cells (guarded against edits made since the snapshot). Returns (written rows, skipped because edited)."""
    ix, values = snapshot
    targets = [r for r in rows if r["action"] in ("lower", "raise")]
    if not targets:
        return 0, 0
    guarded = {col_letter(ix[c] + 1): c for c in GUARDED if c in ix}
    sku_col = ix["SKU"] + 1
    fresh_sku = with_retry(lambda: ws.col_values(sku_col), what="sku column before the write", max_attempts=3)
    fresh_cols = {c: with_retry(lambda c=c: ws.col_values(ix[c] + 1), what=f"{c} column before the write", max_attempts=3) for c in guarded.values()}
    snap_sku = [str(row[ix["SKU"]]) if ix["SKU"] < len(row) else "" for row in values]

    def sku_now(n):
        return str(fresh_sku[n - 1]).replace(",", "").strip() if n - 1 < len(fresh_sku) else ""

    # rows inserted / deleted / sorted since the snapshot: a row number that no longer holds its SKU is never written (redo it next run)
    moved = {r["n"] for r in targets if sku_now(r["n"]) != str(r["sku"]).replace(",", "").strip()}
    if moved:
        print(f"  sheet rows moved while this tool ran - {len(moved)} row(s) skipped: {sorted(moved)[:15]}")
    updates = []
    for r in targets:
        if r["n"] in moved:
            continue
        cells = new_cells(r["d"], r["stock"])
        for name, value in cells.items():
            if name in ix:
                updates.append({"range": f"{col_letter(ix[name] + 1)}{r['n']}", "values": [[value]]})

    def fresh_value(c, n):
        col = fresh_cols[c]
        return col[n - 1] if n - 1 < len(col) else ""

    def snap_value(c, n):
        row = values[n - 1] if n - 1 < len(values) else []
        return row[ix[c]] if ix[c] < len(row) else ""

    kept, dropped = drop_edited_cells(updates, guarded, snap_sku, snap_value, fresh_sku, fresh_value)
    if dropped:
        print(f"  edited while this tool ran - {len(dropped)} cell write(s) skipped, the new value stays: "
              + "; ".join(f"{s} {c}={v}" for s, c, v in dropped[:15]))

    def row_of(u):
        return int(str(u["range"]).lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))

    # a row whose price / Profit % / Fee % was edited meanwhile is left entirely alone: never a new price next to an old Profit %
    kept_ranges = {u["range"] for u in kept}
    edited_rows = {row_of(u) for u in updates if u["range"] not in kept_ranges}
    final = [u for u in kept if row_of(u) not in edited_rows]
    pairs = [(u["range"], u["values"]) for u in final]
    for i in range(0, len(pairs), 1500):
        part = pairs[i:i + 1500]
        with_retry(lambda part=part: ws.batch_update([{"range": rng, "values": val} for rng, val in part]), what="sheet batch update", max_attempts=3)
    written = len({row_of(u) for u in final} & {r["n"] for r in targets})
    return written, len(targets) - written


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    wanted = [t.strip() for t in (os.getenv("TABS") or "").split(",") if t.strip()]
    if wanted:
        tabs = [book.worksheet(t) for t in wanted]
    else:
        tabs = [sheet_tabs.product_sheet(book)]
        try:
            amz = book.worksheet(sheet_tabs.AMAZON_TAB)
            if amz.title != tabs[0].title:
                tabs.append(amz)
        except gspread.exceptions.WorksheetNotFound:
            pass
    defended = read_defended(book)
    frozen = held_skus.load_skus(os.path.join(os.path.dirname(os.path.abspath(__file__)), "protected_skus.txt"))
    print(f"{'APPLY' if APPLY else 'DRY RUN'} | top band profit {getattr(pricing, 'TOP_BAND_PROFIT', pricing.PROFIT_BANDS[-1][1]):g}% | outlier ratio {OUTLIER_RATIO:g} | cap {MAX_CHANGES} | "
          f"defended SKUs {len(defended)} | frozen SKUs {len(frozen)}")
    results = []
    for ws in tabs:
        rows, snap = analyse_tab(ws, defended, frozen)
        report(ws.title, rows, ws.title == sheet_tabs.AMAZON_TAB)
        results.append((ws, rows, snap))
    changes = sum(1 for _, rows, _ in results for r in rows if r["action"] in ("lower", "raise"))
    holds = sum(1 for _, rows, _ in results for r in rows if r["action"] == "hold")
    print(f"\nTOTAL: {changes} price(s) to change, {holds} held for review")
    if not APPLY:
        print("DRY RUN - nothing written. Re-run with apply=yes to write the prices.")
        return
    if changes > MAX_CHANGES:
        print(f"STOP: {changes} changes exceed the cap of {MAX_CHANGES} - re-run with a higher max_changes if that is intended.")
        sys.exit(2)
    if not changes:
        print("nothing to change")
        return
    # 1) the old values first: a failed write must never leave them unrecorded
    title = f"Prices evened {time.strftime('%Y-%m-%d', time.gmtime())}"
    names = {w.title for w in book.worksheets()}
    n = 1
    while title in names:
        n += 1
        title = f"Prices evened {time.strftime('%Y-%m-%d', time.gmtime())} ({n})"
    body = [row for ws, rows, _ in results for row in backup_rows(ws.title, rows)]
    bk = book.add_worksheet(title=title, rows=len(body) + 20, cols=len(BACKUP_HEADER))
    with_retry(lambda: bk.update("A1", [BACKUP_HEADER], value_input_option="RAW"), what="backup header", max_attempts=3)
    for i in range(0, len(body), 500):
        with_retry(lambda ch=body[i:i + 500]: bk.append_rows(ch, value_input_option="RAW"), what="backup rows", max_attempts=3)
    print(f"backup tab {title!r}: {len(body)} row(s)")
    # 2) the prices
    total_written = total_skipped = 0
    for ws, rows, snap in results:
        w, s = apply_tab(ws, rows, snap)
        total_written += w
        total_skipped += s
        print(f"{ws.title}: wrote {w} row(s), skipped {s} edited meanwhile")
    print(f"DONE: {total_written} row(s) put on the schedule, {total_skipped} skipped (edited meanwhile), {holds} held for review. "
          "Next: Audit Price Drift with fix=yes pushes the new prices onto OnBuy.")


if __name__ == "__main__":
    main()
