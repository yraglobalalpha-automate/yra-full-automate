"""Listings that show ANOTHER product's content are HELD at stock 0 (2026-10-05).

Two sources, both plain text files next to the code (one SKU per line, '#' starts a comment, a missing file is just empty):
  hold_at_zero_skus.txt  SKUs known to show another product but which the nightly name check cannot flag (same brand and half the
                         same words as the product they wrongly show) - maintained by hand, a line is removed once the content is repaired;
  held_at_zero.txt       every listing zero_stock_mismatched.py found showing another product in THIS nightly job - it appends to the file
                         so the price/stock audit that runs after it knows them too (the file only ever exists in the CI workspace).

Why: the nightly price/stock FIX pushed the sheet's stock back onto every drifted listing, so the stock the zero step had taken off
a wrong-content listing came straight back the next night, and the listing sold (wrong-item orders on 2026-08-23 and 2026-10-03).
The audit now never pushes the sheet's price/stock onto a held listing and takes any stock it still shows off.
"""
import os
import time

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


PROTECTED_FILE = "protected_skus.txt"
HOLD_FILE = "hold_at_zero_skus.txt"
_PROTECTED_HEADER = "# OnBuy content mismatch: no price/stock pushes until repaired/delisted.\n"
_HOLD_HEADER = (
    "# SKUs whose OnBuy listing shows ANOTHER product (title, description, images) or another model/spec than the sheet row. The nightly price/stock audit holds them at stock 0\n"
    "# instead of pushing the sheet's stock, and protected_skus.txt stops the hourly sync from restocking them.\n"
    "# Remove a line only after the listing's content has been repaired (and then from protected_skus.txt too).\n")


def persist_holds(items, base_dir=HERE, today=None):
    """Make the nightly's zeroes permanent (user 2026-10-12: "never restock the nightly zeroes").

    items: [(sku, note)]. Appends each SKU that is not yet on the file to protected_skus.txt (the sync never pushes price/stock for it again) AND to
    hold_at_zero_skus.txt (the audit keeps the stock off), with a dated comment line and the note, keeping each file's line endings and creating a
    missing file with its header. Returns the sorted SKUs that were new to at least one file; touches nothing when there are none."""
    today = today or time.strftime("%Y-%m-%d", time.gmtime())
    items = [(str(s).strip(), " ".join(str(n or "").split())) for s, n in items if str(s or "").strip()]
    added = set()
    for fname, header in ((PROTECTED_FILE, _PROTECTED_HEADER), (HOLD_FILE, _HOLD_HEADER)):
        path = os.path.join(base_dir, fname)
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            raw = b""
        crlf = b"\r\n" in raw
        text = raw.decode("utf-8").replace("\r\n", "\n")
        have = load_skus(path)
        new, seen = [], set()
        for sku, note in items:
            if sku in seen or is_held(sku, have):
                continue
            seen.add(sku)
            new.append((sku, note))
        if not new:
            continue
        if not text.strip():
            text = header
        block = [f"# {today} the nightly found these listings showing ANOTHER product - zeroed and frozen automatically; release only after the page or the row is fixed"]
        block += [f"{sku}  # {note[:140]}" if note else sku for sku, note in new]
        text = text.rstrip("\n") + "\n" + "\n".join(block) + "\n"
        with open(path, "wb") as fh:
            fh.write((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
        added.update(sku for sku, _ in new)
    return sorted(added)


def forget(skus, base_dir=HERE):
    """Drop the lines of these SKUs (either zero spelling) from protected_skus.txt and hold_at_zero_skus.txt: a listing that has been DELETED needs no hold
    (the nightly removes wrong-content listings since 2026-10-13). Comment lines and line endings stay. Returns how many lines went."""
    forms = set()
    for s in skus:
        s = str(s or "").strip()
        if s:
            forms |= _forms(s)
    gone = 0
    for fname in (PROTECTED_FILE, HOLD_FILE):
        path = os.path.join(base_dir, fname)
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        crlf = b"\r\n" in raw
        lines = raw.decode("utf-8").replace("\r\n", "\n").split("\n")
        keep = []
        for ln in lines:
            tok = ln.split("#", 1)[0].strip()
            if tok and _forms(tok) & forms:
                gone += 1
                continue
            keep.append(ln)
        if len(keep) != len(lines):
            text = "\n".join(keep)
            with open(path, "wb") as fh:
                fh.write((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    return gone
