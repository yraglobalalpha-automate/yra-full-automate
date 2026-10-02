"""What may be removed from the sheet, and what is held back (2026-10-02).

Pure planning - no Sheet/OnBuy access, so every rule is unit-tested. The rows
passed in are the product tabs in sheet order (eBay tab first, then Amazon):
dicts with tab, row, sku, ident (the supplier product id: 'ebay:<item>' or
'amazon:<ASIN>', "" when the link has none), created (bool: the OnBuy
listing exists), stock (int or None).

Two kinds of removal, both explicit and both guarded against the sheet having
moved on since the list was made:

 A. LISTED rows (the user's out-of-stock list). Held back when the row is in
    stock again, when its SKU is on more than one row, or - not on the sheet
    at all - reported as absent (the OnBuy side may still hold a listing).
 B. LIVE DUPLICATES: a row whose supplier product already belongs to an
    EARLIER row (the keeper - the same "first row wins" rule the sync uses)
    and whose own listing is live. Held back unless the keeper survives and
    is live too; if the keeper is itself being removed, the copy goes only
    when it is out of stock as well (never the last in-stock row of a
    product). Flag text such as "...already used on row 520" is NOT used -
    row numbers in old flags go stale when rows are deleted - the groups are
    recomputed from the current links.
"""


def _in_stock(row):
    return (row.get("stock") or 0) > 0


def plan(rows, listed_skus, include_live_dups=True, approved_dup_skus=None):
    """-> {"remove": [row + reason], "held": [(row, reason)], "absent": [sku]}"""
    listed = set(listed_skus)
    by_sku = {}
    for r in rows:
        if r["sku"]:
            by_sku.setdefault(r["sku"], []).append(r)

    remove, held = {}, []
    key = lambda r: (r["tab"], r["row"])

    # ---- A: the listed rows
    absent = sorted(s for s in listed if s not in by_sku)
    a_ok = set()
    for sku in sorted(listed & set(by_sku)):
        rs = by_sku[sku]
        if len(rs) > 1:
            held.append((rs[0], "SKU is on more than one sheet row - not touched"))
            continue
        r = rs[0]
        if _in_stock(r):
            held.append((r, f"back in stock (stock {r['stock']}) since the list was made"))
            continue
        remove[key(r)] = dict(r, reason="listed (out of stock)")
        a_ok.add(key(r))

    # ---- B: live duplicates
    if include_live_dups:
        groups = {}
        for r in rows:
            if r["ident"]:
                groups.setdefault(r["ident"], []).append(r)
        approved = None if approved_dup_skus is None else set(approved_dup_skus)
        for ident, grp in groups.items():
            if len(grp) < 2:
                continue
            keeper = grp[0]
            for dup in grp[1:]:
                if not dup["created"]:
                    continue                       # not live: not a live duplicate
                if key(dup) in remove:
                    remove[key(dup)]["reason"] = "listed (out of stock) and a live duplicate"
                    continue
                if not dup["sku"] or len(by_sku.get(dup["sku"], [])) > 1:
                    held.append((dup, "duplicate with a missing/repeated SKU - not touched"))
                    continue
                if approved is not None and dup["sku"] not in approved:
                    held.append((dup, "live duplicate that was not in the approved duplicate list"))
                    continue
                if key(keeper) in a_ok:
                    if _in_stock(dup):
                        held.append((dup, "its original row is being removed but this copy is in stock"))
                        continue
                elif not keeper["created"]:
                    held.append((dup, "its original row is not live - this copy is the only live listing"))
                    continue
                elif _in_stock(dup) and not _in_stock(keeper):
                    held.append((dup, "its original row shows no stock but this copy does - the data disagree"))
                    continue
                remove[key(dup)] = dict(dup, reason=f"live duplicate of {keeper['tab']} row {keeper['row']}")

    return {"remove": sorted(remove.values(), key=lambda r: (r["tab"], r["row"])),
            "held": held, "absent": absent}
