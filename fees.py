"""OnBuy's commission per category, from the API's own tier table.

OnBuy does not charge a flat 20%: GET /v2/commission-tiers lists the
tiers for the site (7% consumer electronics, 8% games consoles, 13% DIY,
15% most things, 20% jewellery ...), some with a price threshold and a
second rate above it ("Electronic Accessories: 15% up to GBP 100, 8%
above"), all with a GBP 0.25 minimum - and every category record carries
the tier it belongs to (commission_tier_id). refresh_fees.py writes both
tables to CSV; this module answers "what does OnBuy take on a product in
this category" for the price formula (pricing.py) and the Buy Box floor.

FEE_MODE (repo variable): "category" prices against the real tier;
anything else keeps the flat 20% assumption the formula grew up with, so
the switch is deliberate and per store. FEE_TIER_MODE lives in pricing.py.
"""
import csv
import io
import logging
import os

import pricing

logger = logging.getLogger("onbuy_sync")

_DIR = os.path.dirname(os.path.abspath(__file__))
TIERS_CSV = os.path.join(_DIR, "onbuy_commission_tiers.csv")
CATEGORY_TIERS_CSV = os.path.join(_DIR, "onbuy_category_tiers.csv")
CATEGORIES_CSV = os.path.join(_DIR, "onbuy_categories_only.csv")
FEE_MODE = (os.getenv("FEE_MODE") or "flat").strip().lower()


def _pct(value):
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


def _money(value):
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


class Fees:
    def __init__(self, tiers, by_category, path_to_id):
        self.tiers = tiers                  # tier id -> FeeRule
        self.by_category = by_category      # category id (str) -> FeeRule
        self.path_to_id = path_to_id        # category path (lower) -> category id (str)

    def rule_for_category_id(self, category_id):
        if category_id is None:
            return None
        return self.by_category.get(str(category_id).strip())

    def rule_for_category_path(self, path):
        cid = self.path_to_id.get(str(path or "").strip().lower())
        return self.rule_for_category_id(cid) if cid else None

    @property
    def standard(self):
        for rule in self.tiers.values():
            if rule.name.lower().startswith("standard"):
                return rule
        return None


def load(tiers_path=TIERS_CSV, map_path=CATEGORY_TIERS_CSV, categories_path=CATEGORIES_CSV):
    """The tables as written by refresh_fees.py. Raises FileNotFoundError
    when a table is missing - callers decide whether that is fatal."""
    tiers = {}
    with io.open(tiers_path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            tid = str(r.get("Tier ID") or "").strip()
            lower = _pct(r.get("Lower %"))
            if not tid or lower is None:
                continue
            tiers[tid] = pricing.FeeRule(
                name=str(r.get("Name") or "").strip(),
                lower_pct=lower,
                upper_pct=_pct(r.get("Upper %")),
                threshold=_money(r.get("Threshold (£)")),
                min_fee=_money(r.get("Min Fee (£)")) or 0.0,
            )
    by_category = {}
    with io.open(map_path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            cid = str(r.get("Category ID") or "").strip()
            rule = tiers.get(str(r.get("Commission Tier ID") or "").strip())
            if cid and rule is not None:
                by_category[cid] = rule
    path_to_id = {}
    if os.path.exists(categories_path):
        with io.open(categories_path, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                path = str(r.get("OnBuy Category Path") or "").strip().lower()
                cid = str(r.get("Category ID") or "").strip()
                if path and cid:
                    path_to_id[path] = cid
    return Fees(tiers, by_category, path_to_id)


_cache = {"loaded": False, "fees": None}


def get():
    """The loaded tables in category mode, else None (flat 20% applies).
    Logged once per run either way, so a run's log always says which."""
    if _cache["loaded"]:
        return _cache["fees"]
    _cache["loaded"] = True
    if FEE_MODE != "category":
        logger.info("Fees: FEE_MODE=%s - the flat %s%% commission assumption applies", FEE_MODE, pricing.PLATFORM_FEE_PERCENT)
        return None
    try:
        # Pass the current module paths explicitly - load()'s defaults are
        # bound at definition time, so tests that monkeypatch these globals
        # (and any future reassignment) must reach load() through the call.
        fees = load(TIERS_CSV, CATEGORY_TIERS_CSV, CATEGORIES_CSV)
    except FileNotFoundError as exc:
        logger.error("Fees: FEE_MODE=category but a tier table is missing (%s) - falling back to the flat %s%%",
                     exc, pricing.PLATFORM_FEE_PERCENT)
        return None
    logger.info("Fees: category mode - %d tiers, %d categories mapped, tier mode %s",
                len(fees.tiers), len(fees.by_category), pricing.FEE_TIER_MODE)
    _cache["fees"] = fees
    return fees


def enabled():
    return get() is not None


def rule_for_category_id(category_id):
    fees = get()
    return fees.rule_for_category_id(category_id) if fees else None


def rule_for_category_path(path):
    fees = get()
    return fees.rule_for_category_path(path) if fees else None
