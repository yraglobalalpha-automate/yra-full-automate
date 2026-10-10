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
    listed_in_stock_ok (2026-10-10, the Makstore "delete these" order: brand-blocked
    live listings and never-listed rows that carry a supplier stock figure) lets an
    explicit list remove rows that are in stock; protection, open orders and a SKU
    on several rows still hold.
 B. LIVE DUPLICATES: a row whose supplier product already belongs to an
    EARLIER row (the keeper - the same "first row wins" rule the sync uses)
    and whose own listing is live. Held back unless the keeper survives and
    is live too; if the keeper is itself being removed, the copy goes only
    when it is out of stock as well (never the last in-stock row of a
    product). Flag text such as "...already used on row 520" is NOT used -
    row numbers in old flags go stale when rows are deleted - the groups are
    recomputed from the current links.
 C. DEAD COPIES (2026-10-05, opt-in): a row that shares a supplier product with a
    LIVE owner row (the owner = the first live row, else the first row - the sync's
    link_owners rule), was never listed itself and so only holds the product's claim
    as a frozen "Failed: ... already used" row. Never touches a live row; held back
    unless the owner is live (and Synced when required).

Two optional guards (2026-10-05): require_keeper_synced - a duplicate only goes
while its original row's Sync Status starts with "Synced" (the "live" flag alone
can be a stuck product); exclude_skus - SKUs that are never removed (open
orders, protected listings), whichever rule would take them.
"""


def _in_stock(row):
    return (row.get("stock") or 0) > 0


def owner(group):
    """The row that keeps a supplier product: the first LIVE row, else the first row (the sync's link_owners rule)."""
    return next((r for r in group if r["created"]), group[0])


def plan(rows, listed_skus, include_live_dups=True, approved_dup_skus=None, require_keeper_synced=False,
         exclude_skus=None, include_dead_copies=False, listed_in_stock_ok=False):
    """-> {"remove": [row + reason], "held": [(row, reason)], "absent": [sku]}"""
    listed = set(listed_skus)
    never = set(exclude_skus or ())
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
        if sku in never:
            held.append((rs[0], "excluded (open order / protected SKU) - not touched"))
            continue
        if len(rs) > 1:
            held.append((rs[0], "SKU is on more than one sheet row - not touched"))
            continue
        r = rs[0]
        if _in_stock(r) and not listed_in_stock_ok:
            held.append((r, f"back in stock (stock {r['stock']}) since the list was made"))
            continue
        remove[key(r)] = dict(r, reason="listed (in stock - explicit order)" if _in_stock(r) else "listed (out of stock)")
        a_ok.add(key(r))

    groups = {}
    for r in rows:
        if r["ident"]:
            groups.setdefault(r["ident"], []).append(r)
    approved = None if approved_dup_skus is None else set(approved_dup_skus)

    # ---- B: live duplicates
    if include_live_dups:
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
                if dup["sku"] in never:
                    held.append((dup, "excluded (open order / protected SKU) - not touched"))
                    continue
                if require_keeper_synced and not str(keeper.get("sync") or "").startswith("Synced"):
                    held.append((dup, f"its original row is not Synced ({str(keeper.get('sync') or 'blank')[:40]})"))
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

    # ---- C: dead copies (a never-listed row behind a live owner)
    if include_dead_copies:
        for ident, grp in groups.items():
            if len(grp) < 2:
                continue
            own = owner(grp)
            if not own["created"]:
                continue                           # no live owner: nothing here is a dead copy
            for dead in grp:
                if dead is own or dead["created"] or key(dead) in remove:
                    continue
                if not dead["sku"] or len(by_sku.get(dead["sku"], [])) > 1:
                    held.append((dead, "dead copy with a missing/repeated SKU - not touched"))
                    continue
                if approved is not None and dead["sku"] not in approved:
                    held.append((dead, "dead copy that was not in the approved list"))
                    continue
                if dead["sku"] in never:
                    held.append((dead, "excluded (open order / protected SKU) - not touched"))
                    continue
                if require_keeper_synced and not str(own.get("sync") or "").startswith("Synced"):
                    held.append((dead, f"its owner row is not Synced ({str(own.get('sync') or 'blank')[:40]})"))
                    continue
                remove[key(dead)] = dict(dead, reason=f"dead copy of {own['tab']} row {own['row']} (never listed)")

    return {"remove": sorted(remove.values(), key=lambda r: (r["tab"], r["row"])),
            "held": held, "absent": absent}
