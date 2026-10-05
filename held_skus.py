"""Listings that show ANOTHER product's content are HELD at stock 0 (2026-10-05).

Two sources, both plain text files next to the code (one SKU per line, '#' starts a comment, a missing file is just empty):
  hold_at_zero_skus.txt  SKUs known to show another product but which the nightly name check cannot flag (same brand and half the
                         same words as the product they wrongly show) - maintained by hand, a line is removed once the content is repaired;
  held_at_zero.txt       every listing zero_stock_mismatched.py found showing another product in THIS nightly job - it appends to the file
                         so the price/stock audit that runs after it knows them too (the file only ever exists in the CI workspace).

Why: the nightly price/stock FIX pushed the sheet's stock back onto every drifted listing, so the stock the zero step had taken off
a wrong-content listing came straight back the next night, and the listing sold (YRA 993578879973: a wrong order on 2026-08-23, a
second one on 2026-10-03). The audit now never pushes the sheet's price/stock onto a held listing and takes any stock it still shows off.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ("hold_at_zero_skus.txt", "held_at_zero.txt")
HANDOFF = "held_at_zero.txt"


def _forms(sku):
    sku = str(sku or "").strip()
    return {sku, sku.lstrip("0")} - {""}


def load_skus(path):
    """SKUs of a one-per-line file, both with and without leading zeros; a missing file is empty."""
    out = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                sku = line.split("#", 1)[0].strip()
                if sku:
                    out |= _forms(sku)
    except OSError:
        pass
    return out


def held_set(base_dir=HERE, files=FILES):
    held = set()
    for name in files:
        held |= load_skus(os.path.join(base_dir, name))
    return held


def is_held(sku, held):
    return bool(_forms(sku) & held)


def remember(skus, base_dir=HERE, name=HANDOFF):
    """Append SKUs to the hand-off file the audit reads later in the same job; returns how many were written."""
    skus = sorted({str(s).strip() for s in skus if str(s or "").strip()})
    if skus:
        with open(os.path.join(base_dir, name), "a", encoding="utf-8") as fh:
            fh.write("".join(s + "\n" for s in skus))
    return len(skus)


def split_held(live, sheet, held):
    """live: {sku: (price, stock, updated_at)}; sheet: {sku: (price, stock, tab, row)}.

    -> (to_zero, rest). to_zero = [(sku, price, live_stock, tab, row)] for every held SKU that is on the sheet and still shows stock on
    OnBuy; price is the listing's own (a held SKU never gets the sheet's price), the sheet's when the listing has none, and a SKU with
    no usable price is left alone. rest = live without any held SKU, so the price/stock drift check never touches a held listing."""
    to_zero, rest = [], {}
    for sku, rec in live.items():
        if not is_held(sku, held):
            rest[sku] = rec
            continue
        price, stock = rec[0], rec[1]
        if sku not in sheet or not stock or stock <= 0:
            continue
        price = price if price and price > 0 else sheet[sku][0]
        if not price or price <= 0:
            continue
        to_zero.append((sku, price, stock, sheet[sku][2], sheet[sku][3]))
    return to_zero, rest
