"""Put every listing on the delivery template of its supplier (user 2026-10-13): eBay-sourced listings on the eBay template, Amazon-sourced on the Amazon
template, AliExpress-sourced (the semi store's CSV uploads, named by the semi sheet - user 2026-10-14) on the AliExpress template; everything else (the
legacy catalogue, no sheet row anywhere) stays where it is. The templates carry the handling and delivery times the user set on the
OnBuy dashboard; this tool only ASSIGNS listings to them - and, with CLEAR_HANDLING=1, takes a listing's OWN handling time off (sets it to 0) so the
template alone decides (user 2026-10-13: "move them according to their respective suppliers").

MODE=plan (default): sweep the listings (or reuse the nightly's shared sweep, LISTINGS_CACHE), join them to the sheet (first product tab = eBay rows by their
link, Amazon tab = Amazon rows), print the template / handling-time census, write handling_times.csv (the listings with their own handling time) and say
how many listings would move - writes nothing to OnBuy.
MODE=test: ONE PUT /v2/listings/by-sku for TEST_SKU carrying the listing's own price and stock plus delivery_template_id (and handling_time 0 when
CLEAR_HANDLING=1), then a filtered read-back: proves whether the API accepts the field (only that listing changes).
MODE=apply: batched PUTs (500 per call; the listing's own price and stock, only the template / handling change) for every listing that is off its
supplier's template or (CLEAR_HANDLING) carries its own handling time, then a read-back of a sample. MAX_CHANGES caps one run.
Env: TEMPLATE_EBAY, TEMPLATE_AMAZON, TEMPLATE_ALI (template ids - test/apply refuse without one), CLEAR_HANDLING, SHEET_NAME, SEMI_SHEET_NAME (the semi
store's sheet: its rows are AliExpress-sourced by their Supplier column / link; blank = full sheet only; unreadable = warning, the Ali rule just does not
apply that run), TEST_SKU, MAX_CHANGES (3000), SAMPLE (12), LISTINGS_CACHE. Prints SKUs, template ids, handling times and counts - never a cost."""
import csv
import json
import os
import time
from collections import Counter

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import listing_sweep
import listings_cache
import sheet_tabs
from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
MODE = (os.getenv("MODE") or "plan").strip().lower()
TEMPLATE_EBAY = (os.getenv("TEMPLATE_EBAY") or "").strip()
TEMPLATE_AMAZON = (os.getenv("TEMPLATE_AMAZON") or "").strip()
TEMPLATE_ALI = (os.getenv("TEMPLATE_ALI") or "").strip()
SEMI_SHEET_NAME = (os.getenv("SEMI_SHEET_NAME") or "").strip()      # the semi store's sheet (CSV uploads, AliExpress rows) - its SKUs are sheet-sourced too
CLEAR_HANDLING = (os.getenv("CLEAR_HANDLING") or "").strip().lower() in ("1", "yes", "true")
TEST_SKU = (os.getenv("TEST_SKU") or "").strip()
MAX_CHANGES = int(os.getenv("MAX_CHANGES") or "3000")
SAMPLE = int(os.getenv("SAMPLE") or "12")
BATCH = 500
SHEET_SOURCED = ("ebay", "amazon", "ali")   # the classes whose supplier we know - the only ones whose handling time may be cleared


def classify(url):
    """The supplier a sheet row's link points at: ebay | amazon | ali | none | other."""
    u = str(url or "").strip().lower()
    if not u:
        return "none"
    if "/itm/" in u or "ebay." in u:
        return "ebay"
    if "amazon." in u or "/dp/" in u or "amzn." in u:
        return "amazon"
    if "aliexpress" in u:
        return "ali"
    return "other"


def wanted_template(cls, ebay=TEMPLATE_EBAY, amazon=TEMPLATE_AMAZON, ali=TEMPLATE_ALI):
    """The template a listing of this supplier class belongs on, or None (leave it where it is)."""
    if cls == "ebay" and ebay:
        return str(ebay)
    if cls == "amazon" and amazon:
        return str(amazon)
    if cls == "ali" and ali:
        return str(ali)
    return None


def own_handling(it):
    """The listing's own handling time in days, or 0 when it has none (the template decides)."""
    return num((it or {}).get("handling_time"), int) or 0


def plan(listings, classes, ebay=TEMPLATE_EBAY, amazon=TEMPLATE_AMAZON, clear_handling=False, ali=TEMPLATE_ALI):
    """-> [(sku, cls, current template, wanted template, price, stock, current handling, wanted handling or None)] for every listing that is off its
    supplier's template or, with clear_handling, carries its own handling time while its supplier is known (sheet-sourced)."""
    out = []
    for it in listings:
        it = it or {}
        sku = str(it.get("sku") or "").strip()
        if not sku:
            continue
        cls = classes.get(sku, "legacy")
        want = wanted_template(cls, ebay, amazon, ali)
        cur = str(it.get("delivery_template_id") or "").strip()
        cur_h = own_handling(it)
        want_h = 0 if clear_handling and cls in SHEET_SOURCED and cur_h != 0 else None
        if (want and cur != want) or want_h is not None:
            out.append((sku, cls, cur, want or cur, it.get("price"), it.get("stock"), cur_h, want_h))
    return out


def census(listings, classes):
    """Printable facts: templates and handling times overall and per supplier class."""
    lines = [f"listings: {len(listings)} | delivery_template_id: {dict(Counter(str((it or {}).get('delivery_template_id')) for it in listings).most_common())} | "
             f"handling_time: {dict(Counter(str((it or {}).get('handling_time')) for it in listings).most_common())}"]
    by_cls = {}
    for it in listings:
        sku = str((it or {}).get("sku") or "").strip()
        by_cls.setdefault(classes.get(sku, "legacy"), []).append(it)
    for cls in ("ebay", "amazon", "ali", "other", "none", "legacy"):
        its = by_cls.get(cls) or []
        if not its:
            continue
        lines.append(f"  {cls:7s} {len(its):6d} listings | templates {dict(Counter(str(i.get('delivery_template_id')) for i in its).most_common())} | "
                     f"handling {dict(Counter(str(i.get('handling_time')) for i in its).most_common())}")
    return lines


def put_listings(onbuy, items):
    """One PUT /v2/listings/by-sku for [(sku, price, stock, template or None, handling or None)] -> (status, {sku: error text or ''}, raw text).
    A None template / handling leaves that field out of the request."""
    listings = []
    for s, p, st, t, h in items:
        rec = {"sku": s, "price": p, "stock": st, "boost_marketing_commission": 0}
        if t:
            rec["delivery_template_id"] = int(t)
        if h is not None:
            rec["handling_time"] = int(h)
        listings.append(rec)
    payload = {"site_id": onbuy.site_id, "seller_id": onbuy.seller_id, "listings": listings}
    resp = onbuy._send("PUT", f"{BASE_URL}/listings/by-sku", what=f"onbuy template update({len(items)})", json=payload, timeout=120)
    outcome = {}
    try:
        body = resp.json()
        results = body.get("results") if isinstance(body, dict) else body
        for r in results or []:
            r = r or {}
            if r.get("sku") is not None:
                outcome[str(r["sku"]).strip()] = str(r.get("error") or "").strip()
    except ValueError:
        pass
    return resp.status_code, outcome, resp.text[:600]


put_templates = put_listings        # the name the first version used


def sendable_moves(moves):
    """(the moves whose listing has a price above zero - the only ones a PUT can carry, count of the rest). A listing at 0.00 is one OnBuy never put
    live: the by-SKU endpoint answers "SKU does not exist" for it (2026-10-15), so it is set aside instead of failing every night."""
    sendable = [m for m in moves if (num(m[4], float) or 0) > 0]
    return sendable, len(moves) - len(sendable)


def num(v, cast):
    try:
        return cast(float(str(v).replace(",", "").strip())) if cast is int else cast(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def sheet_classes(book):
    """{sku: supplier class} from the product tabs: the first tab by its link, the Amazon tab = amazon."""
    classes = {}
    first = sheet_tabs.product_sheet(book)
    tabs = [(first, None)]
    try:
        amz = book.worksheet(sheet_tabs.AMAZON_TAB)
        if amz.title != first.title:
            tabs.append((amz, "amazon"))
    except gspread.exceptions.WorksheetNotFound:
        pass
    for ws, fixed in tabs:
        values = with_retry(ws.get_all_values, what=f"read {ws.title}", max_attempts=3)
        if not values:
            continue
        h = [str(x).strip() for x in values[0]]
        if "SKU" not in h:
            raise SystemExit(f"tab {ws.title!r} has no SKU column")
        i_s, i_u = h.index("SKU"), (h.index("Supplier URL") if "Supplier URL" in h else None)
        for r in values[1:]:
            sku = str(r[i_s]).replace(",", "").strip() if i_s < len(r) else ""
            if not sku:
                continue
            url = r[i_u] if i_u is not None and i_u < len(r) else ""
            classes.setdefault(sku, fixed or classify(url))
    return classes


def semi_classes(book):
    """{sku: supplier class} from the semi store's sheet (its first tab): the Supplier column the semi sync fills, else the row's link."""
    classes = {}
    ws = book.sheet1
    values = with_retry(ws.get_all_values, what=f"read semi {ws.title}", max_attempts=3)
    if not values:
        return classes
    h = [str(x).strip() for x in values[0]]
    if "SKU" not in h:
        raise ValueError(f"semi sheet tab {ws.title!r} has no SKU column")
    i_s = h.index("SKU")
    i_u = h.index("Supplier URL") if "Supplier URL" in h else None
    i_p = h.index("Supplier") if "Supplier" in h else None
    for r in values[1:]:
        sku = str(r[i_s]).replace(",", "").strip() if i_s < len(r) else ""
        if not sku:
            continue
        supplier = str(r[i_p]).strip().lower() if i_p is not None and i_p < len(r) else ""
        url = r[i_u] if i_u is not None and i_u < len(r) else ""
        classes.setdefault(sku, {"aliexpress": "ali", "ebay": "ebay", "amazon": "amazon"}.get(supplier) or classify(url))
    return classes


def add_semi_classes(classes, client, name=SEMI_SHEET_NAME):
    """Merge the semi sheet's SKUs into classes (the full sheet's row wins when a SKU sits in both). Unreadable sheet = a warning, nothing else."""
    if not name:
        return classes
    try:
        semi = semi_classes(with_retry(lambda: client.open(name), what="semi sheet open", max_attempts=3))
    except Exception as exc:  # noqa: BLE001 - the semi sheet is optional: without it the Ali rule simply does not apply this run
        print(f"WARNING: semi sheet {name!r} not read ({str(exc)[:160]}) - AliExpress-sourced listings cannot be recognised this run")
        return classes
    added = 0
    for sku, cls in semi.items():
        if sku not in classes:
            classes[sku] = cls
            added += 1
    print(f"semi sheet {name!r}: {len(semi)} SKUs by supplier {dict(Counter(semi.values()).most_common())}, {added} added (not in the full sheet)")
    return classes


def write_handling_csv(listings, classes, path="handling_times.csv"):
    """The listings that carry their OWN handling time (for review; never changed by the plan)."""
    own = [it for it in listings if own_handling(it) != 0]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sku", "supplier", "handling_time_days", "delivery_template_id", "price", "stock", "created_at", "onbuy_name"])
        for it in sorted(own, key=lambda i: (-own_handling(i), str(i.get("sku")))):
            sku = str(it.get("sku") or "").strip()
            w.writerow([sku, classes.get(sku, "legacy"), it.get("handling_time"), it.get("delivery_template_id"), it.get("price"), it.get("stock"),
                        str(it.get("created_at") or "")[:10], str(it.get("name") or "")[:90]])
    return len(own)


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    client = gspread.authorize(creds)
    book = with_retry(lambda: client.open(SHEET_NAME), what="sheet open", max_attempts=3)
    classes = add_semi_classes(sheet_classes(book), client)
    print(f"MODE {MODE} | templates: eBay {TEMPLATE_EBAY or '-'}, Amazon {TEMPLATE_AMAZON or '-'}, AliExpress {TEMPLATE_ALI or '-'} | "
          f"clear own handling time: {'yes' if CLEAR_HANDLING else 'no'} | sheet SKUs by supplier: {dict(Counter(classes.values()).most_common())}")
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")

    if MODE == "test":
        if not TEST_SKU or not (TEMPLATE_EBAY or TEMPLATE_AMAZON or TEMPLATE_ALI):
            raise SystemExit("test needs TEST_SKU and a template id")
        before = onbuy.get_listing(TEST_SKU)
        if not before:
            raise SystemExit(f"{TEST_SKU}: no listing")
        cls = classes.get(TEST_SKU, "legacy")
        want = wanted_template(cls) or TEMPLATE_EBAY or TEMPLATE_AMAZON or TEMPLATE_ALI
        want_h = 0 if CLEAR_HANDLING else None
        print(f"{TEST_SKU}: class {cls} | before: template {before.get('delivery_template_id')} handling {before.get('handling_time')} price {before.get('price')} "
              f"stock {before.get('stock')} | want template {want}, handling {'0' if want_h == 0 else 'unchanged'}")
        status, outcome, text = put_listings(onbuy, [(TEST_SKU, num(before.get("price"), float), num(before.get("stock"), int) or 0, want, want_h)])
        print(f"PUT -> HTTP {status} | per-SKU answer {outcome} | {text}")
        for wait in (10, 30, 60):
            time.sleep(wait)
            after = onbuy.get_listing(TEST_SKU) or {}
            print(f"read-back after {wait}s: template {after.get('delivery_template_id')} handling {after.get('handling_time')} price {after.get('price')} "
                  f"stock {after.get('stock')} updated_at {after.get('updated_at')}")
            ok_t = str(after.get("delivery_template_id")) == str(want)
            ok_h = want_h is None or own_handling(after) == 0
            if ok_t and ok_h:
                print("RESULT: the API accepted it - " + ("template and handling time moved" if want_h == 0 else "the template moved"))
                return
        print("RESULT: the listing did NOT change through the API (field ignored or refused) - the dashboard / a support ticket is the way")
        return

    raw = listings_cache.load() if os.getenv("LISTINGS_CACHE") else None
    if raw is None:
        listings, short, meta, pages = listing_sweep.sweep(onbuy)
        print(f"sweep: {len(listings)} listings in {pages} page(s), short pages {len(short)}, metadata {(meta or {}).get('metadata')}")
    else:
        listings = raw
        print(f"shared sweep: {len(listings)} listings")
    for line in census(listings, classes):
        print(line)
    print(f"listings with their own handling time: {write_handling_csv(listings, classes)} -> handling_times.csv")
    moves = plan(listings, classes, clear_handling=CLEAR_HANDLING)
    by = Counter((m[1], m[2] or "none", m[3]) for m in moves if m[2] != m[3])
    print(f"TO MOVE: {len(moves)} listing(s): " + ", ".join(f"{c} {cur}->{want}: {n}" for (c, cur, want), n in by.most_common())
          + f" | handling time cleared on {sum(1 for m in moves if m[7] is not None)}")
    with open("delivery_template_plan.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sku", "supplier", "current_template", "wanted_template", "stock", "handling_now", "handling_wanted"])
        for s, c, cur, want, _p, st, ch, wh in moves:
            w.writerow([s, c, cur, want, st, ch, "" if wh is None else wh])
    if MODE != "apply":
        print("PLAN ONLY - nothing changed.")
        return
    if not moves:
        print("nothing to move")
        return
    if len(moves) > MAX_CHANGES:
        raise SystemExit(f"{len(moves)} moves exceed MAX_CHANGES={MAX_CHANGES} - refusing")
    sendable, priceless = sendable_moves(moves)
    if priceless:
        print(f"skipped {priceless} listing(s) with no price above zero on OnBuy (never-live listings; a PUT needs a price) - they stay where they are")
    if not sendable:
        print("nothing to send")
        return
    ok = failed = 0
    errors = Counter()
    for c in range(0, len(sendable), BATCH):
        chunk = [(s, num(p, float), num(st, int) or 0, want, wh) for s, _c, _cur, want, p, st, _ch, wh in sendable[c:c + BATCH]]
        status, outcome, text = put_listings(onbuy, chunk)
        for s, _p, _st, _t, _h in chunk:
            err = outcome.get(s, "no answer")
            if err:
                failed += 1
                errors[err[:60]] += 1
            else:
                ok += 1
        print(f"batch {c // BATCH + 1}: HTTP {status}, {sum(1 for s, *_ in chunk if not outcome.get(s, 'x'))} ok of {len(chunk)}")
        time.sleep(2)
    print(f"APPLIED: {ok} ok, {failed} failed {dict(errors.most_common(5))}")
    time.sleep(60)
    sample = sendable[:SAMPLE]
    moved = 0
    for s, _c, _cur, want, _p, _st, _ch, wh in sample:
        after = onbuy.get_listing(s) or {}
        moved += str(after.get("delivery_template_id")) == str(want) and (wh is None or own_handling(after) == 0)
        time.sleep(0.5)
    print(f"read-back sample: {moved} of {len(sample)} already show the change (OnBuy applies updates with a delay - re-run MODE=plan later for the full count)")


if __name__ == "__main__":
    main()
