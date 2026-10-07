"""Delete listings whose OnBuy page shows ANOTHER product - the OnBuy listing AND the sheet row (user rule 2026-10-07).

A listing that shows a different product from its sheet row, and that OnBuy will not let us rewrite (the page belongs to a catalogue
product the barcode is bound to), can never be right again; zeroing it only stops sales until the next restock. The standing rule is
to remove it, not to wait for a correction. This does both halves in one guarded run:

  1. every approved SKU is checked AGAIN against OnBuy just now (one filtered read each): it goes only while its live listing's name
     still shares less than KEEP_AT of its words with the row's title - a page that has since been fixed, a row that was re-pointed,
     a SKU that is gone from OnBuy or sits on several rows is HELD BACK, never deleted;
  2. each removed row is copied whole to a BACKUP_TAB (so the team can re-add the product under a NEW SKU), then deleted from its tab
     (rows located by SKU in a fresh read, as guarded_remove_rows does);
  3. onbuy_delete_list.txt names the SKUs whose rows are gone; the workflow deletes those listings from OnBuy in batches afterwards
     (delete_listings_batch.py).

Open orders do NOT hold a removal back (user: "despite getting the wrong order"): the order is the user's to cancel or refund.
Supabase is not touched - the SKU registry keeps the barcode, so nobody can re-use it silently.

Env: SKUS (comma-separated, exact), DRY_RUN (default 1), MAX_REMOVE (40), BACKUP_TAB, KEEP_AT (0.5), SHEET_NAME.
"""
import csv
import json
import os
import time

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import adoption_guard
import guarded_remove_rows as grr
import sheet_tabs
from onbuy_client import OnBuyClient
from retry_utils import with_retry

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
MAX_REMOVE = int(os.getenv("MAX_REMOVE") or "40")
KEEP_AT = float(os.getenv("KEEP_AT") or "0.5")
WANT = list(dict.fromkeys(s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()))


def read_listings(onbuy, skus):
    """({sku: OnBuy record or None}, {sku: error text}) - a read that FAILED holds its SKU back; it never counts as 'no listing'."""
    listings, errors = {}, {}
    for sku in skus:
        try:
            listings[sku] = onbuy.get_listing(sku)
        except Exception as exc:  # noqa: BLE001 - one bad read must not stop the others
            errors[sku] = str(exc)[:120]
        time.sleep(0.3)
    return listings, errors


def plan_wrong_content(approved, rows, listings, keep_at=KEEP_AT, errors=None):
    """approved: SKUs asked for. rows: sheet rows (dicts with tab, row, sku, title, stock, ...). listings: {sku: OnBuy record or None}.
    errors: {sku: text} for reads that failed. -> {"remove": [row dict + reason + overlap + onbuy_name], "held": [(sku, reason)]}"""
    errors = errors or {}
    by_sku = {}
    for r in rows:
        if r["sku"]:
            by_sku.setdefault(r["sku"], []).append(r)
    remove, held = [], []
    for sku in approved:
        rs = by_sku.get(sku, [])
        if not rs:
            held.append((sku, "not on the sheet"))
            continue
        if len(rs) > 1:
            held.append((sku, f"on {len(rs)} sheet rows - not touched"))
            continue
        if sku in errors:
            held.append((sku, f"the OnBuy read failed ({errors[sku]}) - not touched"))
            continue
        lst = listings.get(sku)
        if not lst:
            held.append((sku, "no live OnBuy listing under this SKU - nothing to delete"))
            continue
        name = str(lst.get("name") or "").strip()
        title = str(rs[0].get("title") or "").strip()
        if not name or not title:
            held.append((sku, "the listing name or the row title is empty - cannot compare"))
            continue
        overlap = adoption_guard.title_similarity(name, title)
        if overlap >= keep_at:
            held.append((sku, f"the OnBuy page now matches the row (word overlap {overlap:.2f}) - not wrong content"))
            continue
        remove.append(dict(rs[0], reason=f"wrong content: OnBuy shows '{name[:60]}'", overlap=overlap, onbuy_name=name,
                           onbuy_opc=str(lst.get("product_encoded_id") or lst.get("opc") or ""),
                           onbuy_stock=lst.get("stock")))
    return {"remove": remove, "held": held}


def open_book():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    return with_retry(lambda: gspread.authorize(creds).open(grr.SHEET_NAME), what="sheet open", max_attempts=3)


def load_tabs(book):
    """-> (product tabs, {tab: header row}, every sheet row with its Title)."""
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        amz = book.worksheet(sheet_tabs.AMAZON_TAB)
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    headers_by_tab, rows = {}, []
    for ws in tabs:
        headers, rs = grr.load_tab(ws)
        if "SKU" not in headers:
            raise SystemExit(f"tab {ws.title!r} has no SKU header - refusing to continue")
        ix = {h: i for i, h in enumerate(headers) if h}
        for r in rs:
            r["title"] = str(r["cells"][ix["Title"]]).strip() if "Title" in ix and ix["Title"] < len(r["cells"]) else ""
        headers_by_tab[ws.title] = headers
        rows += rs
    return tabs, headers_by_tab, rows


def main():
    if not WANT:
        raise SystemExit("SKUS required")
    book = open_book()
    tabs, headers_by_tab, rows = load_tabs(book)
    print(f"sheet rows read: { {t: sum(1 for r in rows if r['tab'] == t) for t in headers_by_tab} } | {len(WANT)} SKU(s) asked for | "
          f"{'DRY RUN' if DRY_RUN else 'LIVE RUN'}")

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    listings, errors = read_listings(onbuy, WANT)

    out = plan_wrong_content(WANT, rows, listings, errors=errors)
    remove, held = out["remove"], out["held"]
    print(f"\nWILL REMOVE {len(remove)} row(s) and their OnBuy listings:")
    for r in sorted(remove, key=lambda x: (x["tab"], x["row"])):
        print(f"  {r['tab']:7s} row {r['row']:5d} | {r['sku']} | sheet stock {r['stock']} | OnBuy stock {r['onbuy_stock']} OPC {r['onbuy_opc']} | "
              f"overlap {r['overlap']:.2f}\n           row  : {r['title'][:95]}\n           page : {r['onbuy_name'][:95]}")
    print(f"\nHELD BACK {len(held)}:")
    for sku, why in held:
        print(f"  {sku} - {why}")

    with open("onbuy_delete_list.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(sorted({r["sku"] for r in remove})) + "\n")
    with open("removal_plan.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["action", "tab", "row", "sku", "sheet_title", "onbuy_name", "onbuy_opc", "overlap", "reason"])
        for r in remove:
            w.writerow(["REMOVE", r["tab"], r["row"], r["sku"], r["title"], r["onbuy_name"], r["onbuy_opc"], f"{r['overlap']:.2f}", r["reason"]])
        for sku, why in held:
            w.writerow(["HELD", "", "", sku, "", "", "", "", why])

    if DRY_RUN:
        print("\nDRY RUN - nothing changed.")
        return
    if not remove:
        print("nothing to remove")
        return
    if len(remove) > MAX_REMOVE:
        raise SystemExit(f"plan removes {len(remove)} rows, above MAX_REMOVE={MAX_REMOVE} - refusing")

    grr.write_backup(book, headers_by_tab, remove)
    total_deleted, skipped = 0, []
    for ws in tabs:
        skus = [r["sku"] for r in remove if r["tab"] == ws.title]
        if skus:
            d, sk = grr.delete_rows(book, ws.title, skus)
            total_deleted += d
            skipped += [(ws.title, s, n) for s, n in sk]
    print(f"\nDELETED {total_deleted} of {len(remove)} planned row(s) from the sheet")
    left = []
    for ws in tabs:
        col = with_retry(lambda ws=ws: ws.col_values(headers_by_tab[ws.title].index("SKU") + 1), what="verify", max_attempts=3)
        have = {str(v).replace(",", "").strip() for v in col[1:]}
        left += [r["sku"] for r in remove if r["tab"] == ws.title and r["sku"] in have]
    print(f"VERIFY: {len(left)} removed SKU(s) still on the sheet" + (f": {left[:20]}" if left else ""))
    keep = set(left) | {s for _t, s, _n in skipped}
    if keep:
        with open("onbuy_delete_list.txt", "w", encoding="utf-8") as fh:
            fh.write("\n".join(sorted({r["sku"] for r in remove} - keep)) + "\n")
        print(f"{len(keep)} SKU(s) kept out of the OnBuy delete list because their row is still on the sheet")


if __name__ == "__main__":
    main()
