"""Selling price calculation.

price = (cost + shipping) x (1 + profit%/100) / (1 - commission%/100)

Exactly three ingredients (user policy, restated 2026-09-11): the cost
base, the profit percentage its range decides, and OnBuy's commission for
the product's category - nothing else.

**The commission is a share of the SELLING price, not of cost** (user
policy 2026-09-01). OnBuy charges it on what the customer pays, so adding
it to a cost-side markup under-collected it. Dividing by (1 - fee) is the
algebra that fixes it: from S x (1 - fee) = cost x (1 + profit), the
amount retained after commission is exactly cost x (1 + profit%) at ANY
fee rate - so the profit schedule holds whether the category pays 7% or
20%, and the fee never stacks on top of the profit.

Profit by cost range (user schedule 2026-09-11, stored as PLAIN PROFIT
percentages - the older "total markup" notation that folded a flat 20%
fee assumption into the numbers is retired):

  cost + shipping  under GBP 5    -> 100% profit
  cost + shipping  GBP 5 to 10    ->  80% profit
  cost + shipping  GBP 10 to 50   ->  40% profit
  cost + shipping  above GBP 50   ->  20% profit

Cheap products carried too little absolute profit at a flat markup - a
GBP 3 item earned pennies after the fee. The ranges apply to the same
base the profit multiplies (cost + shipping). Range edges: the first
bound is strict ("under 5"), every later range's upper bound is inclusive
- exactly GBP 10 falls in the 80% range, exactly GBP 50 in the 40% range;
strictly above 50 gets 20%. This applies to already-listed products too:
every sweep recalculates and raises any price below the formula
(max(existing, formula) in generate_xml.py) - only a manually-set price
ABOVE the formula is left alone, per the never-lower rule - and a price
the automation set under a SUPERSEDED schedule follows the formula down
(see _SUPERSEDED_PROFIT_BANDS).
"""

import os

# What OnBuy actually deducts runs ABOVE the tier's nominal rate: it charges
# 20% VAT on its own commission and takes both out of the sale. Read off the
# order records (GET /v2/orders, 2026-10-07): `sales_fee_ex_VAT` is exactly
# the listed rate x the price (15% -> 10.78 on a 71.84 sale, 7% -> 6.82 on
# 97.41) and `sales_fee_inc_VAT` is exactly that x 1.2 (12.94 and 8.18) - so
# a 15% category really costs 18% of the price, a 7% one 8.4%, a 20% one 24%.
# (The first policy, 2026-09-17, added a flat 1.5 points to every rate: right
# for 7% by accident, 1.5 points short for the common 15% category.)
# FeeRule keeps the nominal rate (it mirrors OnBuy's own tier data); the VAT
# is applied only where fees are COMPUTED, so the Fee % columns show the real
# effective rate (18.00 for a 15% category, not 15.00 or the old 16.50).
FEE_VAT_PERCENT = float(os.getenv("FEE_VAT_PERCENT") or "20")
# Extra points on top of the VAT-inclusive rate - a spare knob, 0 by default.
FEE_UPLIFT_PERCENT = float(os.getenv("FEE_UPLIFT_PERCENT") or "0")
# The model in force 2026-09-17 .. 2026-10-07 (nominal + 1.5 points). Kept so a
# price the automation set under it is still recognised as its own
# (generate_xml._formula_priced) and a Fee % cell it wrote still reads as its own.
LEGACY_FEE_UPLIFT_PERCENT = 1.5


def effective_rate(nominal_pct, legacy=False):
    """The share of the selling price OnBuy deducts for a listed commission
    of `nominal_pct` percent (legacy=True: the 2026-09-17 model, nominal + 1.5)."""
    nominal_pct = float(nominal_pct)
    if legacy:
        return nominal_pct + LEGACY_FEE_UPLIFT_PERCENT
    return nominal_pct * (1.0 + FEE_VAT_PERCENT / 100.0) + FEE_UPLIFT_PERCENT


def effective_min_fee(rule, legacy=False):
    """The per-sale minimum commission as OnBuy deducts it (VAT on it too)."""
    if not rule.min_fee:
        return 0.0
    return rule.min_fee if legacy else rule.min_fee * (1.0 + FEE_VAT_PERCENT / 100.0)

MIN_PROFIT_PERCENT = 20
# The flat assumption the formula grew up with. OnBuy's real commission is
# per category (7% - 20%, most 15%, see fees.py); this stays the fallback
# for rows without a category tier and for stores not yet in category mode.
PLATFORM_FEE_PERCENT = 20  # OnBuy commission - charged on the SELLING price

# (upper cost bound inclusive, PROFIT %) - checked in order; None = no
# bound. Each line traces to the policy decision that set it.
PROFIT_BANDS = (
    (5.0, 100),    # under GBP 5 (80% -> 100%, 2026-09-11)
    (10.0, 80),    # GBP 5-10 inclusive (2026-09-11 schedule)
    (50.0, 40),    # over GBP 10 up to 50 inclusive (2026-09-11 schedule)
    (None, 20),    # above GBP 50 (40%/25% -> 20%, 2026-09-11)
)


def _band_lookup(bands, total_cost):
    # First range is strict-below (exactly 5 -> next range); every later
    # range's upper bound is inclusive - same edge rules as documented above.
    if total_cost < bands[0][0]:
        return bands[0][1]
    for bound, profit in bands[1:]:
        if bound is None or total_cost <= bound:
            return profit


def profit_percent(total_cost):
    """The profit percentage the cost range decides."""
    return _band_lookup(PROFIT_BANDS, total_cost)


# Superseded profit schedules, oldest first - kept so the sync's
# automation-set test (generate_xml._formula_priced) still recognises a
# price set under an older schedule, letting a schedule change reprice
# existing rows instead of freezing them at the old level (max() alone
# never lowers). Append the outgoing schedule whenever PROFIT_BANDS
# changes. (Schedules from the retired total-markup era are recorded here
# as the profit they actually produced.)
_SUPERSEDED_PROFIT_BANDS = (
    # until 2026-09-11 am: 80/80/40/40 with 30% above GBP 100
    ((5.0, 80), (10.0, 80), (30.0, 40), (100.0, 40), (None, 30)),
    # 2026-09-11 am: the above-GBP-100 profit cut to 25%, superseded the
    # same day by the full range rewrite
    ((5.0, 80), (10.0, 80), (30.0, 40), (100.0, 40), (None, 25)),
)


def legacy_profit_percents(total_cost):
    """Profit percentages a superseded schedule gave this cost, excluding
    the current range's own value - empty for costs whose profit never
    moved."""
    if total_cost <= 0:
        return []
    current = profit_percent(total_cost)
    out = []
    for bands in _SUPERSEDED_PROFIT_BANDS:
        profit = _band_lookup(bands, total_cost)
        if profit != current and profit not in out:
            out.append(profit)
    return out


# ---- category commission tiers (2026-09-09) ---------------------------------
# OnBuy's commission is a tier per category, not a flat 20%: GET
# /v2/commission-tiers lists 29 for the UK site (7% consumer electronics and
# large appliances, 8% games consoles, 13% DIY, 15% for most, 20% jewellery),
# some with a price threshold and a second rate above it ("Electronic
# Accessories: 15% up to GBP 100, 8% above"), all with a GBP 0.25 minimum.
# fees.py loads the tables; a FeeRule is what the formula divides by.
#
# FEE_TIER_MODE says how the second rate applies once a price passes the
# threshold: "marginal" (default) = only to the part above it, the usual
# reading of "X% up to GBP T, Y% thereafter"; "step" = to the whole price.
# Confirm against the Fees page of the seller portal before relying on a
# tiered category; a flat tier is unaffected.
FEE_TIER_MODE = (os.getenv("FEE_TIER_MODE") or "marginal").strip().lower()


class FeeRule:
    def __init__(self, name, lower_pct, upper_pct=None, threshold=None, min_fee=0.0):
        self.name = name
        self.lower_pct = float(lower_pct)
        self.upper_pct = float(upper_pct) if upper_pct is not None else None
        self.threshold = float(threshold) if threshold else None
        self.min_fee = float(min_fee or 0.0)

    @property
    def tiered(self):
        return self.upper_pct is not None and self.threshold is not None

    def __repr__(self):
        if self.tiered:
            return f"FeeRule({self.name}: {self.lower_pct:g}% to £{self.threshold:g}, {self.upper_pct:g}% above)"
        return f"FeeRule({self.name}: {self.lower_pct:g}%)"


def fee_amount(price, rule=None, mode=None, legacy=False):
    """What OnBuy takes on a sale at `price` under the rule (the flat
    default when rule is None) - commission plus the VAT on it."""
    if price <= 0:
        return 0.0
    if rule is None:
        return price * effective_rate(PLATFORM_FEE_PERCENT, legacy) / 100.0
    mode = (mode or FEE_TIER_MODE)
    r1 = effective_rate(rule.lower_pct, legacy) / 100.0
    if not rule.tiered or price <= rule.threshold:
        fee = price * r1
    elif mode == "step":
        fee = price * effective_rate(rule.upper_pct, legacy) / 100.0
    else:
        fee = rule.threshold * r1 + (price - rule.threshold) * effective_rate(rule.upper_pct, legacy) / 100.0
    return max(fee, effective_min_fee(rule, legacy))


def price_for_retained(retained, rule=None, mode=None, legacy=False):
    """The lowest price that leaves `retained` after OnBuy's commission -
    the fee-as-divisor algebra of the module docstring, extended to a
    tiered rule and to the minimum fee."""
    if retained <= 0:
        return 0.0
    if rule is None:
        return retained / (1 - effective_rate(PLATFORM_FEE_PERCENT, legacy) / 100.0)
    mode = (mode or FEE_TIER_MODE)
    r1 = effective_rate(rule.lower_pct, legacy) / 100.0
    price = retained / (1 - r1)
    if rule.tiered and price > rule.threshold:
        r2 = effective_rate(rule.upper_pct, legacy) / 100.0
        if mode == "step":
            above = retained / (1 - r2)
            # A falling tier (20% then 5%) can leave a gap where no price is
            # consistent with either rate; the first price past the
            # threshold is the smallest that still retains the target.
            price = above if above > rule.threshold else rule.threshold + 0.01
        else:
            # P - [T*r1 + (P - T)*r2] = retained  ->  P = (retained + T*(r1 - r2)) / (1 - r2)
            price = (retained + rule.threshold * (r1 - r2)) / (1 - r2)
    # The divisor algebra above assumed the percentage fee; if that fee
    # (price - retained) is below the floor, the minimum fee binds instead,
    # and the price that leaves `retained` after a flat GBP fee is simply
    # retained + min_fee. (fee_amount clamps to the min, so compare the raw
    # gap, not fee_amount's result.)
    _min_fee = effective_min_fee(rule, legacy)
    if _min_fee and (price - retained) < _min_fee:
        price = retained + _min_fee
    return price


def price_for_margin_of_price(base_cost, margin_pct, rule=None, mode=None):
    """The lowest price whose profit AFTER commission is at least
    margin_pct of the PRICE itself: S - fee(S) - base >= margin*S. The Buy
    Box floor's definition (user 2026-09-28): margin measured on the sale,
    not on cost - 15% of cost was only ~11% of the sale. None when the fee
    plus the margin leaves (almost) nothing of the price."""
    if base_cost <= 0:
        return None
    m = margin_pct / 100.0
    if rule is None:
        denom = 1 - effective_rate(PLATFORM_FEE_PERCENT) / 100.0 - m
        return base_cost / denom if denom > 0.02 else None
    mode = (mode or FEE_TIER_MODE)
    r1 = effective_rate(rule.lower_pct) / 100.0
    d1 = 1 - r1 - m
    if d1 <= 0.02:
        return None
    price = base_cost / d1
    if rule.tiered and price > rule.threshold:
        r2 = effective_rate(rule.upper_pct) / 100.0
        d2 = 1 - r2 - m
        if d2 <= 0.02:
            return None
        if mode == "step":
            above = base_cost / d2
            price = above if above > rule.threshold else rule.threshold + 0.01
        else:
            # S(1-m) - [T*r1 + (S-T)*r2] = base -> S = (base + T*(r1-r2)) / (1 - m - r2)
            price = (base_cost + rule.threshold * (r1 - r2)) / d2
    # If the percentage fee this price implies is under the rule's minimum
    # fee, the flat minimum binds instead: S - min_fee - base >= m*S.
    _min_fee = effective_min_fee(rule)
    if _min_fee and (price * (1 - m) - base_cost) < _min_fee:
        price = (base_cost + _min_fee) / (1 - m)
    return price


def price_for_profit(total_cost, profit_pct, rule=None, platform_fee_percent=None, legacy=False):
    """Price that retains total_cost x (1 + profit%) after commission.
    `platform_fee_percent` is a LISTED commission rate (a typed Fee % cell, the
    flat 20): the VAT OnBuy adds to it is applied here, like for a rule's rates."""
    if total_cost <= 0:
        return 0.0
    retained = total_cost * (1 + profit_pct / 100.0)
    if rule is not None and platform_fee_percent is None:
        return round(price_for_retained(retained, rule, legacy=legacy), 2)
    fee = min(max(effective_rate(PLATFORM_FEE_PERCENT if platform_fee_percent is None else platform_fee_percent, legacy), 0.0), 95.0) / 100.0
    return round(retained / (1 - fee), 2)


def effective_fee_percent(price, rule=None, legacy=False):
    """The commission (VAT included) as a share of the price - what "Fee %" records."""
    if price <= 0:
        return 0.0
    return fee_amount(price, rule, legacy=legacy) / price * 100.0


def effective_profit_percent(price, total_cost, rule=None, platform_fee_percent=None, legacy=False):
    """The profit a sale at `price` leaves as a share of the cost base (cost + shipping), after OnBuy's commission (VAT
    included) - the inverse of price_for_profit, and what the Profit % cell shows for a price a person set above the
    formula. `rule` / `platform_fee_percent` pick the fee exactly as price_for_profit does."""
    if price <= 0 or total_cost <= 0:
        return 0.0
    if rule is not None and platform_fee_percent is None:
        fee = fee_amount(price, rule, legacy=legacy)
    else:
        rate = PLATFORM_FEE_PERCENT if platform_fee_percent is None else platform_fee_percent
        fee = price * min(max(effective_rate(rate, legacy), 0.0), 95.0) / 100.0
    return ((price - fee) / total_cost - 1.0) * 100.0


def calculate_selling_price(
    cost_price,
    shipping_cost=0.0,
    *,
    min_profit_percent=MIN_PROFIT_PERCENT,
    platform_fee_percent=None,
    fee_rule=None,
    legacy=False,
):
    """The range's profit on cost + shipping, retained after OnBuy's
    commission: the category's real tier when fee_rule is given, else the
    flat platform_fee_percent (default: the standard 20% assumption)."""
    if cost_price <= 0:
        return 0.0

    total_cost = cost_price + shipping_cost
    retained = total_cost * (1 + profit_percent(total_cost) / 100)
    if fee_rule is not None and platform_fee_percent is None:
        return round(price_for_retained(retained, fee_rule, legacy=legacy), 2)
    # The fee is a DIVISOR, never a markup - see the module docstring. The
    # clamp keeps a nonsense override (>= 100% commission) from inverting
    # the price or dividing by zero mid-run; 95% is already far outside any
    # real OnBuy category.
    fee = min(max(effective_rate(PLATFORM_FEE_PERCENT if platform_fee_percent is None else platform_fee_percent, legacy), 0.0), 95.0) / 100.0
    return round(retained / (1 - fee), 2)
