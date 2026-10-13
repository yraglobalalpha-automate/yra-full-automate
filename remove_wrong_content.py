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

UNATTENDED in the nightly (user 2026-10-13: "Delist every wrong listing from now and in future"): SKUS_FILE = the zero pass's hand-off file (every listing it
found showing another product) instead of SKUS; a SKU on same_product_skus.txt (checked by hand) is never removed; a page that belongs to a NEIGHBOURING row
(the CSV-era one-row shift) is removed even when it shares a few words with its own row; with KEEP_AT below the scan's 0.5 line the borderline band
(KEEP_AT .. 0.5, mostly the same product worded differently) stays frozen for a human instead of being deleted; CLEAN_HOLDS=1 drops the removed SKUs' lines
from protected_skus.txt + hold_at_zero_skus.txt. An empty hand-off file is a normal, quiet run.
"""
import csv
import json
import os
import time

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import adoption_guard
import guarded_remove_rows as grr
import held_skus
import sheet_tabs
from onbuy_client import OnBuyClient
from retry_utils import with_retry

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
MAX_REMOVE = int(os.getenv("MAX_REMOVE") or "40")
KEEP_AT = float(os.getenv("KEEP_AT") or "0.5")
SKUS_FILE = (os.getenv("SKUS_FILE") or "").strip()                 # the nightly: the zero pass's hand-off file, one SKU per line
CLEAN_HOLDS = (os.getenv("CLEAN_HOLDS") or "").strip().lower() in ("1", "yes", "true")
MATCH_LINE = 0.5                                                     # the scan's own line: at or above it the page IS the row's product
# Listings checked BY HAND: the live page IS this row's product, only worded differently - never removed, whatever the overlap says.
SAME_PRODUCT = held_skus.load_skus(os.path.join(held_skus.HERE, "same_product_skus.txt"))


def wanted_skus(env_skus=None, skus_file=None):
    """The SKUs to consider: SKUS (comma-separated) or, when that is empty, the lines of SKUS_FILE (comments allowed; a missing file = none)."""
    skus = [s.strip() for s in (env_skus if env_skus is not None else (os.getenv("SKUS") or "")).split(",") if s.strip()]
    path = skus_file if skus_file is not None else SKUS_FILE
    if not skus and path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            skus = [ln.split("#", 1)[0].strip() for ln in fh if ln.split("#", 1)[0].strip()]
    return list(dict.fromkeys(skus))


WANT = wanted_skus()


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


def shifted_from(rows, r, name, min_similarity=MATCH_LINE, reach=3):
    """The neighbouring row (within `reach` rows on the same tab) whose title the listing's name belongs to - the CSV-era one-row shift - as
    (row, similarity), or None. The best-matching neighbour wins."""
    best = None
    for other in rows:
        if other is r or other["tab"] != r["tab"] or abs(int(other["row"]) - int(r["row"])) > reach:
            continue
        t = str(other.get("title") or "").strip()
        if not t:
            continue
        sim = adoption_guard.title_similarity(name, t)
        if sim >= min_similarity and (best is None or sim > best[1]):
            best = (other, sim)
    return best


def plan_wrong_content(approved, rows, listings, keep_at=KEEP_AT, errors=None, same_product=None):
    """approved: SKUs asked for. rows: sheet rows (dicts with tab, row, sku, title, stock, ...). listings: {sku: OnBuy record or None}.
    errors: {sku: text} for reads that failed. same_product: SKUs checked by hand (never removed).
    -> {"remove": [row dict + reason + overlap + onbuy_name], "held": [(sku, reason)]}

    A listing goes when its page shares less than keep_at of its words with the row (another product), or when the page belongs to a neighbouring
    row (a shift) more than to its own. Between keep_at and the scan's match line it is a borderline - held (frozen) for a human; at or above the
    match line the page is the row's product."""
    errors = errors or {}
    same_product = same_product or set()
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
        if held_skus.is_held(sku, same_product):
            held.append((sku, "on same_product_skus.txt (checked by hand: the page is this product, worded differently) - never removed"))
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
        if overlap >= MATCH_LINE:
            held.append((sku, f"the OnBuy page now matches the row (word overlap {overlap:.2f}) - not wrong content"))
            continue
        shift = shifted_from(rows, rs[0], name) if overlap < MATCH_LINE else None
        if overlap >= keep_at and not (shift and shift[1] > overlap):
            held.append((sku, f"borderline (word overlap {overlap:.2f}: below the match line, not clearly another product) - left frozen for review"))
            continue
        why = f"wrong content: OnBuy shows '{name[:60]}'" + (f" - the product of {shift[0]['tab']} row {shift[0]['row']} (shift)" if shift else "")
        remove.append(dict(rs[0], reason=why, overlap=overlap, onbuy_name=name,
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
        if SKUS_FILE:
            print(f"no SKU in {SKUS_FILE} - nothing to remove")
            open("onbuy_delete_list.txt", "w", encoding="utf-8").close()
            return
        raise SystemExit("SKUS required")
    book = open_book()
    tabs, headers_by_tab, rows = load_tabs(book)
    print(f"sheet rows read: { {t: sum(1 for r in rows if r['tab'] == t) for t in headers_by_tab} } | {len(WANT)} SKU(s) asked for | "
          f"{'DRY RUN' if DRY_RUN else 'LIVE RUN'}")

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    listings, errors = read_listings(onbuy, WANT)

    out = plan_wrong_content(WANT, rows, listings, errors=errors, same_product=SAME_PRODUCT)
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
    if CLEAN_HOLDS:
        gone = held_skus.forget([r["sku"] for r in remove if r["sku"] not in keep])
        print(f"hold lists (protected_skus.txt + hold_at_zero_skus.txt): {gone} line(s) dropped for the removed SKUs")


if __name__ == "__main__":
    main()
