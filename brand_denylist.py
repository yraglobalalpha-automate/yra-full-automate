"""Brands this store never lists (user order 2026-11-04: every Hisense and Homcom product out of YRA, and never again).

The list lives in brand_denylist.txt (one brand per line, '|' between spellings of the same brand, '#' starts a comment). The repository
variable BLOCKED_BRANDS (brands separated by commas, semicolons or new lines) replaces the file while it is set. If the file cannot be read
at all the built-in default (Hisense, Homcom) applies - the block must not disappear because a file went missing - and a file that is
there but names no brand blocks nothing.

A row belongs to a blocked brand when
  - its Brand cell names the brand (whole word, any case), or
  - its Brand cell is empty or generic ("Unbranded", "Generic", ...) and the brand is one of the first three words of the Title, not after
    a word such as "for / fits / compatible" - an accessory "for Hisense TV" sold under another brand is NOT the brand's product.

generate_xml.py treats such a row like any frozen row: the Sync Status says why, nothing is created or pushed, Stock is forced to 0 (the
sheet shows it inactive and a listing that somehow exists is zeroed by the frozen-row pass), and the catalogue mirror leaves it out.
Pure module: no sheet, no OnBuy, no network."""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DENYLIST_FILE = os.path.join(HERE, "brand_denylist.txt")
DEFAULT_NAMES = "Hisense, Homcom"
STATUS_PREFIX = "Blocked brand"
LEAD_WORDS = 3
# What the sync already treats as "no brand" (generate_xml._NON_BRAND_VALUES, after normalize_brand) plus empty placeholders.
NO_BRAND = frozenset({"", "-", "n/a", "na", "none", "null", "unbranded", "branded", "generic", "no brand", "does not apply", "not applicable"})
ACCESSORY_WORDS = frozenset({"for", "fits", "fit", "with", "compatible", "suits", "suit", "to", "replacement", "spare"})

_cache = None


def parse_names(text):
    """[(canonical name, [spellings])] from the file text or the variable text. The first spelling is the name shown in messages."""
    out = []
    for raw in re.split(r"[\r\n]+", str(text or "")):
        line = raw.split("#", 1)[0]
        for chunk in re.split(r"[,;]", line):
            spellings = [s.strip() for s in chunk.split("|") if s.strip()]
            if spellings:
                out.append((spellings[0], spellings))
    return out


def compile_names(parsed):
    """[(name, compiled regex)] - the regex matches any spelling as a whole word, any case ('HOMCOM(R)' and 'Homcom UK' match, 'Homcomfort' does not)."""
    out = []
    for name, spellings in parsed:
        body = "|".join(re.escape(s) for s in spellings)
        out.append((name, re.compile(r"(?<![A-Za-z0-9])(?:" + body + r")(?![A-Za-z0-9])", re.I)))
    return out


def load(path=None, env_value=None):
    """The blocked brands as [(name, regex)]. BLOCKED_BRANDS wins when set; else the file; an unreadable file falls back to DEFAULT_NAMES."""
    value = os.getenv("BLOCKED_BRANDS") if env_value is None else env_value
    if value and str(value).strip():
        return compile_names(parse_names(value))
    try:
        with open(path or DENYLIST_FILE, encoding="utf-8") as fh:
            return compile_names(parse_names(fh.read()))
    except OSError:
        return compile_names(parse_names(DEFAULT_NAMES))


def names():
    """The list for this process, loaded once (a workflow run is short-lived)."""
    global _cache
    if _cache is None:
        _cache = load()
    return _cache


def reset_cache():
    global _cache
    _cache = None


def blocked_name(brand, title="", listed=None):
    """The name of the blocked brand this row belongs to, '' when it belongs to none. `listed` is a load() result (default: this process's list)."""
    listed = names() if listed is None else listed
    if not listed:
        return ""
    brand_text = str(brand or "").strip()
    for name, rx in listed:
        if rx.search(brand_text):
            return name
    if brand_text.lower() in NO_BRAND:
        lead = " ".join(str(title or "").split()[:LEAD_WORDS])
        for name, rx in listed:
            m = rx.search(lead)
            if m and not any(w.strip(",.:;()").lower() in ACCESSORY_WORDS for w in lead[:m.start()].split()):
                return name
    return ""


def mentions(title, listed=None):
    """The blocked brand a title merely MENTIONS anywhere (an accessory 'for Hisense TV'), '' for none - for reports, never for a decision."""
    listed = names() if listed is None else listed
    for name, rx in listed:
        if rx.search(str(title or "")):
            return name
    return ""


def status_text(name):
    """The Sync Status a blocked row carries (kept short: the column is read by people)."""
    return f"{STATUS_PREFIX}: {name} is not listed on this store (store policy) - kept off OnBuy, stock 0"


def is_blocked_status(status):
    return str(status or "").startswith(STATUS_PREFIX)


def guard(brand, title, flag, stock, listed=None):
    """(flag, stock, name) for one row inside the sync loop: a blocked row gets the blocked-brand status as its flag (it wins over any other
    flag - it is the reason that matters) and Stock 0; every other row passes through unchanged with name ''."""
    name = blocked_name(brand, title, listed)
    if not name:
        return flag, stock, ""
    return status_text(name), 0, name
