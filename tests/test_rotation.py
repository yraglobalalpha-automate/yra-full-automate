"""Risk-weighted re-check order (2026-10-01): low stock is checked more
often, but nothing starves, and flagged LIVE rows are never deprioritised."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rotation

NOW = datetime(2026, 10, 1, 12, 0, 0)
FMT = "%Y-%m-%d %H:%M:%S"


def parse_time(v):
    try:
        return datetime.strptime(str(v).strip(), FMT)
    except (TypeError, ValueError):
        return datetime(2000, 1, 1)


def row(stock, age_h, sync="Synced", created="TRUE"):
    ts = "" if age_h is None else (NOW - timedelta(hours=age_h)).strftime(FMT)
    return {"Stock": stock, "Last Checked Time": ts, "Sync Status": sync, "OnBuy Product Created": created}


def order(rows, **kw):
    items = list(enumerate(rows))
    return [i for i, _r in rotation.rotation_order(items, NOW, parse_time, **kw)]


def test_low_stock_outranks_default_stock_at_equal_age():
    assert order([row(5, 20), row(1, 20), row(3, 20)]) == [1, 2, 0]


def test_a_stale_default_stock_row_outranks_a_fresh_one_unit_row():
    # no starvation: a 1-unit row checked 10h ago (score 20) vs a stock-5 row unchecked for 60h (score 60)
    assert order([row(1, 10), row(5, 60)])[0] == 1


def test_out_of_stock_rows_wait_behind_in_stock_rows():
    assert order([row(0, 40), row(5, 20)])[0] == 1


def test_needs_human_row_that_is_not_live_is_deprioritised():
    dup = row(5, 40, sync="Failed: supplier link already used on row 5 (Sheet1)", created="")
    normal = row(5, 20)
    assert order([dup, normal])[0] == 1


def test_live_frozen_row_keeps_a_normal_weight():
    # a frozen duplicate that is LIVE on OnBuy is on sale - it must not be starved
    live_dup = row(5, 40, sync="Failed: supplier link already used on row 5 (Sheet1)", created="TRUE")
    normal = row(5, 20)
    assert order([live_dup, normal])[0] == 0


def test_never_checked_row_comes_first():
    assert order([row(1, 5), row(5, None)])[0] == 1


def test_rows_past_the_max_age_jump_the_queue():
    old = row(7, 80)                                   # 6-10 band, weight 0.85 -> score 68
    busy = row(1, 36)                                  # weight 2.0 -> score 72, beats it unless the cap applies
    assert order([old, busy], max_age_hours=0)[0] == 1     # without the guarantee the busier row wins
    assert order([old, busy], max_age_hours=72)[0] == 0    # with it, the >72h row goes first


def test_steady_state_has_bounded_staleness():
    """40% capacity per run, mixed stock levels: no row may drift past a bound."""
    stocks = [1] * 10 + [2] * 10 + [5] * 60 + [8] * 20
    ages = [float(i % 7) for i in range(len(stocks))]
    rows = [row(s, a) for s, a in zip(stocks, ages)]
    cap = 40
    worst = 0.0
    for step in range(60):                              # each step = 1 hour
        items = list(enumerate(rows))
        picked = [i for i, _r in rotation.rotation_order(items, NOW, parse_time, max_age_hours=0)][:cap]
        for i in range(len(rows)):
            if i in picked:
                rows[i]["Last Checked Time"] = NOW.strftime(FMT)
            else:
                prev = parse_time(rows[i]["Last Checked Time"])
                rows[i]["Last Checked Time"] = (prev - timedelta(hours=1)).strftime(FMT)
        if step > 30:
            worst = max(worst, max((NOW - parse_time(r["Last Checked Time"])).total_seconds() / 3600 for r in rows))
    assert worst < 9, f"a row went {worst}h without a check at 40% capacity"


def test_rows_without_a_sku_are_split_out_of_the_batch():
    rows = [{"SKU": "123"}, {"SKU": ""}, {"SKU": "  "}, {}, {"SKU": 456}, {"SKU": None}]
    kept, dropped = rotation.split_no_sku(list(enumerate(rows)))
    assert [i for i, _r in kept] == [0, 4]
    assert [i for i, _r in dropped] == [1, 2, 3, 5]


def test_describe_counts():
    items = list(enumerate([row(3, 1), row(0, 1), row(5, 1, sync="BRAND BLOCKED (x)", created="")]))
    assert rotation.describe(items) == (1, 1, 1)
