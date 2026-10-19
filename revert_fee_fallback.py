"""Revert the flat-fee price inflation of 2026-10-16..19 (user 2026-10-19: "Build the YRA revert and run").

What happened: the OnBuy fee tier tables were missing from the repository from 2026-10-15 12:28Z to 2026-10-19 06:25Z, so every row the syncs visited
was priced with the FLAT fee (20% + VAT = 24%) instead of its category rate (15% -> 18%, 7% -> 8.4% ...): ~8% too high on 15% categories, ~17-20% on 7%
ones, and the Fee % cell was written as 24.00. The nightlies pushed those prices to OnBuy. The tables are back; the sync would now lower each row
again when it next visits it (2-3 days). This tool does exactly that, for all of them now, and nothing else:

A row is reverted when ALL of these hold:
  - the sync's own decision for the row (pricing_probe.decision -> generate_xml.decide_price, read off the sheet's cells and the Supabase mirror)
    says the price is the automation's own and follows the formula ("follows", or "amazon" = re-derived from the cost) - never a "kept" or "manual" price,
    never a row with a typed Profit % / Fee % override, never a frozen wrong-content listing;
  - the row carries the fallback's signature: its Fee % cell reads 24.00, or its current price equals the flat-fee formula for its band within a penny;
  - the decided price differs from the current one by more than a penny.
It writes the cells the sync itself would write (Selling Price, Profit %, Fee %, Price Check Flag), guarded against edits made meanwhile, after backing
the old values up to a sheet tab "Fee fallback reverted <date>". It is NOT the evening tool: a price the automation did not set is never touched.
APPLY=yes writes; anything else is a dry run. MAX_CHANGES (default 7000) stops an unexpectedly big plan. Prints prices and percentages only.
Next: Audit Price Drift with fix=yes (or the nightly) pushes the sheet's prices onto OnBuy."""
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
from pricing_probe import SHEET_NAME, decision  # noqa: E402  - the sync's own decision, read off the sheet's cells
from retry_utils import with_retry  # noqa: E402

APPLY = (os.getenv("APPLY") or "").strip().lower() in ("1", "yes", "true")
MAX_CHANGES = int(os.getenv("MAX_CHANGES") or "7000")
FLAT_FEE_CELL = float(os.getenv("FLAT_FEE_CELL") or "24")       # what the fallback wrote into Fee %
FLAT_FEE_NOMINAL = float(os.getenv("FLAT_FEE_NOMINAL") or "20")  # the listed rate it priced with (VAT is added by pricing)
TOL = 0.011
CHUNK = 150
GUARDED = ("Selling Price (£)", "Profit %", "Fee %")
BACKUP_HEADER = ["Tab", "Row", "SKU", "Title", "Old Selling Price (£)", "New Selling Price (£)", "Ratio old/new", "Old Profit %", "New Profit %",
                 "Old Fee %", "New Fee %", "Stock", "Sync Status", "Why"]


def col_letter(n):
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _num(text):
    s = str(text if text is not None else "").replace("£", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def fallback_signature(d, fee_cell_text, flat_cell=FLAT_FEE_CELL, flat_nominal=FLAT_FEE_NOMINAL, tol=TOL):
    """'fee cell' when the Fee % cell is the fallback's 24.00, 'flat price' when the current price is the flat-fee formula for the band, else ''."""
    if abs(_num(fee_cell_text) - flat_cell) <= 0.006:
        return "fee cell"
    total = (d.get("cost") or 0) + (d.get("ship") or 0)
    if total > 0 and d.get("band_now") is not None:
        flat = pricing.price_for_profit(total, d["band_now"], platform_fee_percent=flat_nominal)
        if abs(flat - d["existing"]) <= tol:
            return "flat price"
    return ""


def plan_row(d, fee_cell_text, *, frozen=False, tol=TOL):
    """(action, note) for one row: 'revert' only for the automation's own price carrying the fallback's signature and off the decided price."""
    if d is None:
        return "skip", "no cost"
    if frozen:
        return "skip", "frozen wrong-content listing"
    if d["profit_override"] is not None or d["fee_override"] is not None:
        return "skip", "typed Profit % / Fee % override"
    if d["how"] not in ("follows", "amazon"):
        return "skip", f"price not the automation's own ({d['how']})"
    sig = fallback_signature(d, fee_cell_text)
    if not sig:
        return "ok", "no fallback signature"
    new, old = round(d["selling_price"], 2), d["existing"]
    if new <= 0 or old <= 0:
        return "skip", "no price to compare"
    if abs(new - old) <= tol:
        return "ok", "already on the formula"
    return "revert", sig


def new_cells(d, stock):
    """The cells the sync itself would write for the decided price."""
    price = round(d["selling_price"], 2)
    return {"Selling Price (£)": price,
            "Profit %": _pct_text(d["band_now"]),
            "Fee %": f"{pricing.effective_fee_percent(price, d['rule']):.2f}",
            "Price Check Flag": "" if stock == 0 else "Normal"}


def analyse_tab(ws, frozen):
    amazon = ws.title == sheet_tabs.AMAZON_TAB
    values = with_retry(ws.get_all_values, what=f"read {ws.title}", max_attempts=3)
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
        except Exception as exc:  # noqa: BLE001 - without the mirror a 24.00 cell may read as an override = the row is skipped, never mispriced
            print(f"mirror read failed for a chunk ({str(exc)[:100]}) - those rows are planned without it")
    out = []
    for n, r in sku_rows:
        c = cells_of(r)
        sku = c("SKU")
        d = decision(c, mirror.get(sku, {}), amazon)
        action, note = plan_row(d, c("Fee %"), frozen=held_skus.is_held(sku, frozen))
        out.append({"n": n, "sku": sku, "title": c("Title"), "action": action, "note": note, "d": d, "stock": int(_num(c("Stock"))),
                    "status": c("Sync Status"), "old": {k: c(k) for k in ("Selling Price (£)", "Profit %", "Fee %", "Price Check Flag")}})
    return out, (ix, values)


def report(ws_title, rows):
    acts = Counter(r["action"] for r in rows)
    print(f"\n=== {ws_title}: {len(rows)} rows with a SKU | " + ", ".join(f"{k} {v}" for k, v in sorted(acts.items())))
    for note, n in Counter(r["note"] for r in rows if r["action"] in ("skip", "ok")).most_common():
        print(f"  {n:5d} x {note}")
    sel = [r for r in rows if r["action"] == "revert"]
    if sel:
        ratios = [r["d"]["existing"] / round(r["d"]["selling_price"], 2) for r in sel]
        down = sum(1 for x in ratios if x > 1)
        print(f"  REVERT: {len(sel)} rows ({sum(1 for r in sel if r['stock'] > 0)} in stock) | {down} go down, {len(sel) - down} go up | "
              f"median {abs(statistics.median(ratios) - 1) * 100:.1f}%, mean {abs(statistics.mean(ratios) - 1) * 100:.1f}%, max {abs(max(ratios) - 1) * 100:.1f}% | "
              f"signature {dict(Counter(r['note'] for r in sel))}")
        for r in sorted(sel, key=lambda r: -(r["d"]["existing"] / round(r["d"]["selling_price"], 2)))[:6]:
            d = r["d"]
            print(f"    e.g. row {r['n']} SKU {r['sku']} {r['title'][:38]!r}: {d['existing']:.2f} -> {d['selling_price']:.2f} (band {d['band_now']:g}%, "
                  f"Fee % cell {r['old']['Fee %']!r} -> {pricing.effective_fee_percent(round(d['selling_price'], 2), d['rule']):.2f}, stock {r['stock']})")


def backup_rows(ws_title, rows):
    out = []
    for r in rows:
        if r["action"] != "revert":
            continue
        d = r["d"]
        nc = new_cells(d, r["stock"])
        out.append([ws_title, r["n"], r["sku"], r["title"][:90], r["old"]["Selling Price (£)"], nc["Selling Price (£)"],
                    f"{d['existing'] / nc['Selling Price (£)']:.3f}", r["old"]["Profit %"], nc["Profit %"], r["old"]["Fee %"], nc["Fee %"],
                    r["stock"], r["status"][:60], r["note"]])
    return out


def apply_tab(ws, rows, snapshot):
    """Write the reverted rows' cells (guarded against edits made since the snapshot). Returns (written rows, skipped because edited/moved)."""
    ix, values = snapshot
    targets = [r for r in rows if r["action"] == "revert"]
    if not targets:
        return 0, 0
    guarded = {col_letter(ix[c] + 1): c for c in GUARDED if c in ix}
    sku_col = ix["SKU"] + 1
    fresh_sku = with_retry(lambda: ws.col_values(sku_col), what="sku column before the write", max_attempts=3)
    fresh_cols = {c: with_retry(lambda c=c: ws.col_values(ix[c] + 1), what=f"{c} column before the write", max_attempts=3) for c in guarded.values()}
    snap_sku = [str(row[ix["SKU"]]) if ix["SKU"] < len(row) else "" for row in values]

    def sku_now(n):
        return str(fresh_sku[n - 1]).replace(",", "").strip() if n - 1 < len(fresh_sku) else ""

    moved = {r["n"] for r in targets if sku_now(r["n"]) != str(r["sku"]).replace(",", "").strip()}
    if moved:
        print(f"  sheet rows moved while this tool ran - {len(moved)} row(s) skipped: {sorted(moved)[:15]}")
    updates = []
    for r in targets:
        if r["n"] in moved:
            continue
        for name, value in new_cells(r["d"], r["stock"]).items():
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
        print(f"  edited while this tool ran - {len(dropped)} cell write(s) skipped: " + "; ".join(f"{s} {c}={v}" for s, c, v in dropped[:15]))

    def row_of(u):
        return int(str(u["range"]).lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))

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
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        amz = book.worksheet(sheet_tabs.AMAZON_TAB)
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    frozen = held_skus.load_skus(os.path.join(os.path.dirname(os.path.abspath(__file__)), "protected_skus.txt"))
    print(f"{'APPLY' if APPLY else 'DRY RUN'} | fallback signature: Fee % cell {FLAT_FEE_CELL:g} or the flat {FLAT_FEE_NOMINAL:g}% formula price | cap {MAX_CHANGES} | frozen SKUs {len(frozen)}")
    results = []
    for ws in tabs:
        rows, snap = analyse_tab(ws, frozen)
        report(ws.title, rows)
        results.append((ws, rows, snap))
    changes = sum(1 for _, rows, _ in results for r in rows if r["action"] == "revert")
    print(f"\nTOTAL: {changes} price(s) to revert")
    if not APPLY:
        print("DRY RUN - nothing written. Re-run with apply=yes to write the prices.")
        return
    if changes > MAX_CHANGES:
        print(f"STOP: {changes} changes exceed the cap of {MAX_CHANGES} - re-run with a higher max_changes if that is intended.")
        sys.exit(2)
    if not changes:
        print("nothing to change")
        return
    title = f"Fee fallback reverted {time.strftime('%Y-%m-%d', time.gmtime())}"
    names = {w.title for w in book.worksheets()}
    n = 1
    while title in names:
        n += 1
        title = f"Fee fallback reverted {time.strftime('%Y-%m-%d', time.gmtime())} ({n})"
    body = [row for ws, rows, _ in results for row in backup_rows(ws.title, rows)]
    bk = book.add_worksheet(title=title, rows=len(body) + 20, cols=len(BACKUP_HEADER))
    with_retry(lambda: bk.update("A1", [BACKUP_HEADER], value_input_option="RAW"), what="backup header", max_attempts=3)
    for i in range(0, len(body), 500):
        with_retry(lambda ch=body[i:i + 500]: bk.append_rows(ch, value_input_option="RAW"), what="backup rows", max_attempts=3)
    print(f"backup tab {title!r}: {len(body)} row(s)")
    total_written = total_skipped = 0
    for ws, rows, snap in results:
        w, s = apply_tab(ws, rows, snap)
        total_written += w
        total_skipped += s
        print(f"{ws.title}: wrote {w} row(s), skipped {s} edited meanwhile")
    print(f"DONE: {total_written} row(s) reverted to the formula with the category fee, {total_skipped} skipped. "
          "Next: Audit Price Drift with fix=yes pushes the new prices onto OnBuy.")


if __name__ == "__main__":
    main()
