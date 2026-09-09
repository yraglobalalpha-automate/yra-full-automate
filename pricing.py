"""Selling price calculation.

price = (cost + shipping) x (1 + profit%/100) / (1 - platform_fee%/100)

**The platform fee is a share of the SELLING price, not of cost** (user
policy 2026-09-01). OnBuy charges its commission on what the customer pays,
so adding the fee to a cost-side markup under-collected it: at the 60% band
a GBP 20 item was priced GBP 32, OnBuy took 20% OF 32 (GBP 6.40, not the
GBP 4.00 the markup allowed for), and the intended 40% profit arrived as
28%. Dividing by (1 - fee) is the algebra that fixes it: from
S x (1 - fee) = cost x (1 + profit), the amount retained after commission
is exactly cost x (1 + profit%) at ANY fee rate.

Tiered profit by product cost (user policy 2026-07-21, rewritten
2026-08-05, fee split out of the markup 2026-09-01). MARGIN_BANDS still
stores each band's historical TOTAL markup; the profit portion is that
total minus the standard 20% fee, so the policy schedule's intent is
unchanged and only the fee arithmetic moved:

  cost + shipping  under GBP 5    -> 80% profit (100% band) -> x2.25
  cost + shipping  GBP 5 to 10    -> 80% profit (100% band) -> x2.25
  cost + shipping  GBP 10 to 30   -> 40% profit ( 60% band) -> x1.75
  cost + shipping  GBP 30 to 100  -> 40% profit ( 60% band) -> x1.75
  cost + shipping  over GBP 100   -> 30% profit ( 50% band) -> x1.625

The x multipliers above assume the standard 20% commission; a category
with a different fee gets its own divisor, so the band's profit portion
survives fee-heavy categories instead of being eaten by them (that is what
the old `extra_fee` stacking was reaching for, now exact).

Cheap products carried too little absolute profit at a flat markup - a
GBP 3 item earned pennies after the fee. The bands apply to the same base
the markup multiplies (cost + shipping). Band edges: the first bound is
strict ("under 5"), every later band's upper bound is inclusive - exactly
GBP 10 falls in the 100% band, exactly GBP 30 and exactly GBP 100 in the
60% band; strictly above 100 gets 50%. This applies to already-listed
products too: every sweep recalculates and raises any price below the
formula (max(existing, formula) in generate_xml.py) - only a manually-set
price ABOVE the formula is left alone, per the never-lower rule.
"""

import os

MIN_PROFIT_PERCENT = 20
# The flat assumption the formula grew up with. OnBuy's real commission is
# per category (7% - 20%, most 15%, see fees.py); this stays the fallback
# for rows without a category tier and for stores not yet in category mode.
PLATFORM_FEE_PERCENT = 20  # OnBuy commission - charged on the SELLING price

# (upper cost bound inclusive, total markup %) - checked in order; None = no
# bound. Adjacent bands may share a rate - they are kept separate so each
# line traces to the policy decision that set it. These are TOTAL markups
# (profit + the standard fee); profit_percent() strips the fee out.
MARGIN_BANDS = (
    (5.0, 100),    # under GBP 5 (2026-07-21)
    (10.0, 100),   # GBP 5-10 inclusive (80% -> 100%, 2026-08-05)
    (30.0, 60),    # over GBP 10 up to 30 inclusive (2026-08-05)
    (100.0, 60),   # over GBP 30 up to 100 inclusive (40% -> 60%, 2026-08-05)
    (None, 50),    # above GBP 100 (40% -> 50%, 2026-08-05)
)


def total_markup_percent(total_cost):
    # First band is strict-below (exactly 5 -> next band); every later
    # band's upper bound is inclusive - same edge rules as documented above.
    if total_cost < MARGIN_BANDS[0][0]:
        return MARGIN_BANDS[0][1]
    for bound, markup in MARGIN_BANDS[1:]:
        if bound is None or total_cost <= bound:
            return markup


def profit_percent(total_cost):
    """The band's profit share of cost, with the standard fee taken out of
    the historical total markup."""
    return max(0, total_markup_percent(total_cost) - PLATFORM_FEE_PERCENT)


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


def fee_amount(price, rule=None, mode=None):
    """What OnBuy takes on a sale at `price` under the rule (the flat
    default when rule is None)."""
    if price <= 0:
        return 0.0
    if rule is None:
        return price * PLATFORM_FEE_PERCENT / 100.0
    mode = (mode or FEE_TIER_MODE)
    r1 = rule.lower_pct / 100.0
    if not rule.tiered or price <= rule.threshold:
        fee = price * r1
    elif mode == "step":
        fee = price * rule.upper_pct / 100.0
    else:
        fee = rule.threshold * r1 + (price - rule.threshold) * rule.upper_pct / 100.0
    return max(fee, rule.min_fee)


def price_for_retained(retained, rule=None, mode=None):
    """The lowest price that leaves `retained` after OnBuy's commission -
    the fee-as-divisor algebra of the module docstring, extended to a
    tiered rule and to the minimum fee."""
    if retained <= 0:
        return 0.0
    if rule is None:
        return retained / (1 - PLATFORM_FEE_PERCENT / 100.0)
    mode = (mode or FEE_TIER_MODE)
    r1 = rule.lower_pct / 100.0
    price = retained / (1 - r1)
    if rule.tiered and price > rule.threshold:
        r2 = rule.upper_pct / 100.0
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
    if rule.min_fee and (price - retained) < rule.min_fee:
        price = retained + rule.min_fee
    return price


def price_for_profit(total_cost, profit_pct, rule=None, platform_fee_percent=None):
    """Price that retains total_cost x (1 + profit%) after commission."""
    if total_cost <= 0:
        return 0.0
    retained = total_cost * (1 + profit_pct / 100.0)
    if rule is not None and platform_fee_percent is None:
        return round(price_for_retained(retained, rule), 2)
    fee = min(max(float(PLATFORM_FEE_PERCENT if platform_fee_percent is None else platform_fee_percent), 0.0), 95.0) / 100.0
    return round(retained / (1 - fee), 2)


def effective_fee_percent(price, rule=None):
    """The commission as a share of the price - what "Fee %" records."""
    if price <= 0:
        return 0.0
    return fee_amount(price, rule) / price * 100.0


def calculate_selling_price(
    cost_price,
    shipping_cost=0.0,
    *,
    min_profit_percent=MIN_PROFIT_PERCENT,
    platform_fee_percent=None,
    fee_rule=None,
):
    """The band's profit on cost + shipping, retained after OnBuy's
    commission: the category's real tier when fee_rule is given, else the
    flat platform_fee_percent (default: the standard 20% assumption)."""
    if cost_price <= 0:
        return 0.0

    total_cost = cost_price + shipping_cost
    retained = total_cost * (1 + profit_percent(total_cost) / 100)
    if fee_rule is not None and platform_fee_percent is None:
        return round(price_for_retained(retained, fee_rule), 2)
    # The fee is a DIVISOR, never a markup - see the module docstring. The
    # clamp keeps a nonsense override (>= 100% commission) from inverting
    # the price or dividing by zero mid-run; 95% is already far outside any
    # real OnBuy category.
    fee = min(max(float(PLATFORM_FEE_PERCENT if platform_fee_percent is None else platform_fee_percent), 0.0), 95.0) / 100.0
    return round(retained / (1 - fee), 2)
