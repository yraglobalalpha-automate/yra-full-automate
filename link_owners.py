"""Who OWNS a supplier product when several sheet rows point at it (2026-10-05).

One supplier product = one listing (user 2026-09-21): of the rows that share a supplier identity ('ebay:<item>[:<variation>]'
or 'amazon:<ASIN>') exactly one owns it and the others freeze as duplicates. The owner is the first row - in sheet order,
product tabs in the order eBay tab, then Amazon tab - that is already LIVE on OnBuy (OnBuy Product Created = TRUE); when none is
live, the first row in sheet order.

Why: the Amazon tab used "the first row PROCESSED this run keeps it", and a run's batch is ordered oldest-pushed first, i.e.
never-listed rows first - so a copy that was never listed claimed the ASIN before the live original was reached and froze it
(live products stuck without price or stock updates: Arden 29, YRA ~10, OpenMaal ~10, GTV ~40 rows on 2026-10-05). The eBay tab
used the first row in sheet order, which freezes a live later copy whenever an earlier row was never listed. Both now follow this
one deterministic rule, the same one removal_plan.py uses to pick the row that stays.
"""


def build(entries):
    """entries: iterable of (tab, row, ident, live) in sheet order. Rows without an identity are ignored.
    -> (counts {ident: rows sharing it}, owner {ident: (tab, row)})."""
    counts, owner, owner_live = {}, {}, {}
    for tab, row, ident, live in entries:
        if not ident:
            continue
        counts[ident] = counts.get(ident, 0) + 1
        if ident not in owner:
            owner[ident], owner_live[ident] = (tab, row), bool(live)
        elif live and not owner_live[ident]:
            owner[ident], owner_live[ident] = (tab, row), True
    return counts, owner
