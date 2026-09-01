"""One-off (2026-08-31, user request): revive the "Price below minimum"
suspended listings by pushing a valid price/stock per SKU.

Value rule (2026-09-01, user policy): ONLY SKUs with a full-auto sheet
row carrying a Supplier URL are pushed, using the sheet's price/stock -
a revival without a managed row goes stale again and is pointless.
Export-only SKUs are reported, never pushed.

Probe first: LIMIT=5 tests whether OnBuy accepts by-SKU updates on
suspended listings at all (the pipeline's suspended-locked class says
edits are rejected until reactivation - support's 1,000-per-call note
suggests it may work now). Batched via update_listings_by_sku_batch,
500 per call. Writes NOTHING to the sheet; read-only against it.
"""
import csv
import io
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(message)s")

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import OnBuyClient
from retry_utils import with_retry

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
LIMIT = int(os.getenv("LIMIT") or "0")  # 0 = all
# The export carries junk GBP0.01 placeholder prices on 80 rows (the very
# below-minimum values that got them suspended) - never push a price under
# this floor; such rows go to the NO-SOURCE worklist instead.
MIN_PRICE = float(os.getenv("MIN_PRICE") or "1.00")
CSV_PATH = os.getenv("CSV_PATH") or "suspended_yra.csv"
SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"


def fnum(v):
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def main():
    sus = list(csv.DictReader(io.open(CSV_PATH, encoding="utf-8-sig", newline="")))
    print(f"suspended export rows: {len(sus)}")

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        __import__("json").loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME).sheet1,
                       what="sheet open", max_attempts=3)
    hdrs = [str(h).strip() for h in sheet.row_values(1)]
    sku_col = hdrs.index("SKU") + 1
    # Bracketed consistent read (see generate_xml.py, 2026-08-31).
    for _ in range(3):
        col_a = with_retry(lambda: sheet.col_values(sku_col), what="sku col", max_attempts=3)
        rows = with_retry(lambda: sheet.get_all_records(), what="sheet read", max_attempts=3)
        col_b = with_retry(lambda: sheet.col_values(sku_col), what="sku col recheck", max_attempts=3)
        if col_a == col_b:
            break
        print("Sheet changed during the read - re-reading")
    else:
        raise SystemExit("sheet still being edited - aborting")
    by_sku = {}
    for i, r in enumerate(rows):
        if i + 1 < len(col_a):
            key = str(col_a[i + 1]).replace(",", "").strip()
            if key:
                by_sku[key] = r

    # 2026-09-01 policy (user): ONLY revive SKUs that live in the full-auto
    # sheet WITH a supplier link - a revival without a managed row goes
    # stale again. Export SKUs may lack leading zeros the sheet keeps (or
    # vice versa) - digit-core fallback join, unique matches only.
    core = {}
    for k in by_sku:
        core.setdefault(k.lstrip("0") or k, []).append(k)
    plan = []
    skipped_no_row, skipped_no_url, skipped_no_price = [], [], []
    for r in sus:
        sku = r["sku"].strip()
        srow = by_sku.get(sku)
        if srow is None:
            cands = core.get(sku.lstrip("0") or sku) or []
            if len(cands) == 1:
                srow = by_sku[cands[0]]
        if srow is None:
            skipped_no_row.append(sku)
            continue
        if not str(srow.get("Supplier URL") or "").strip():
            skipped_no_url.append(sku)
            continue
        sp = fnum(srow.get("Selling Price (£)"))
        sst = int(fnum(srow.get("Stock")))
        if sp < MIN_PRICE:
            skipped_no_price.append(sku)
            continue
        plan.append((sku, sp, max(sst, 0), "sheet"))
    from collections import Counter
    print(f"pushable (sheet+link): {len(plan)} | skipped: not-in-sheet {len(skipped_no_row)}, "
          f"link-missing {len(skipped_no_url)}, sheet-price-missing {len(skipped_no_price)}")
    for s in skipped_no_url:
        print(f"  NO-LINK {s}")
    for s in skipped_no_price:
        print(f"  NO-PRICE {s}")
    if LIMIT:
        plan = plan[:LIMIT]
        print(f"LIMIT: probing first {len(plan)}")
    if DRY_RUN:
        for sku, p, st, src in plan[:15]:
            print(f"  would push {sku}: price {p:.2f} stock {st} [{src}]")
        print("DRY RUN - nothing pushed")
        return

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    ok = failed = 0
    fail_reasons = Counter()
    for c in range(0, len(plan), 500):
        chunk = plan[c:c + 500]
        # client expects [(sku, price, stock), ...] tuples
        listings = [(s, round(p, 2), st) for s, p, st, _ in chunk]
        try:
            res = onbuy.update_listings_by_sku_batch(listings)
        except Exception as exc:
            print(f"batch {c} call failed outright: {str(exc)[:160]}")
            failed += len(chunk)
            fail_reasons[str(exc)[:60]] += len(chunk)
            continue
        # client returns the raw per-item result LIST
        items = res if isinstance(res, list) else (res.get("results", []) if isinstance(res, dict) else [])
        seen = {}
        for it in items:
            it = it or {}
            seen[str(it.get("sku") or "").strip()] = str(it.get("error") or "").strip()
        for s, p, st, _ in chunk:
            err = seen.get(s, "no per-item answer")
            if err:
                failed += 1
                fail_reasons[err[:60]] += 1
                if failed <= 12:
                    print(f"  BOUNCED {s}: {err[:100]}")
            else:
                ok += 1
    print(f"DONE: {ok} accepted, {failed} bounced")
    for reason, n in fail_reasons.most_common(8):
        print(f"  reason x{n}: {reason}")


if __name__ == "__main__":
    main()
