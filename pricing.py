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

MIN_PROFIT_PERCENT = 20
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


def calculate_selling_price(
    cost_price,
    shipping_cost=0.0,
    *,
    min_profit_percent=MIN_PROFIT_PERCENT,
    platform_fee_percent=PLATFORM_FEE_PERCENT,
):
    if cost_price <= 0:
        return 0.0

    total_cost = cost_price + shipping_cost
    profit = profit_percent(total_cost)
    # The fee is a DIVISOR, never a markup - see the module docstring. The
    # clamp keeps a nonsense override (>= 100% commission) from inverting
    # the price or dividing by zero mid-run; 95% is already far outside any
    # real OnBuy category.
    fee = min(max(float(platform_fee_percent), 0.0), 95.0) / 100.0
    return round(total_cost * (1 + profit / 100) / (1 - fee), 2)
