"""store_sale.py on GTV (user 2026-10-26: "Apply 3% sale on all GTV products except the 10-20 gbp range products") - the one-click store sale (user 2026-10-15): PERCENT off every priced listing through PUT /v2/listings/by-sku (sale fields only),
cancel = the past window, state in store_sale.json which the nightly sale guard respects, and the margin floor (no sale price under
(cost + shipping) x (1 + min profit) / (1 - fee))."""
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sale_guard
import store_sale as ss


def rec(sku, price, stock=5, sale=None, end=None):
    return {"sku": sku, "price": price, "stock": stock, "sale_price": sale, "sale_end_date": end}


def test_sale_price_is_the_percentage_off_rounded_to_pence_never_below_the_floor():
    assert ss.sale_price_for("9.99", 10) == 8.99 and ss.sale_price_for(100, 12.5) == 87.5 and ss.sale_price_for("0.02", 90) == 0.01
    assert ss.sale_price_for(None, 10) is None and ss.sale_price_for("", 10) is None and ss.sale_price_for("0", 10) is None
    assert ss.percent_value("10") == 10 and ss.percent_value("0.5") is None and ss.percent_value("95") is None and ss.percent_value("") is None


def test_window_starts_yesterday_and_ends_on_the_last_day():
    start, end = ss.window("2026-11-01", today=date(2026, 10, 15))
    assert (start, end) == ("2026-10-14 00:00:00", "2026-11-01 23:59:59")
    assert ss.window("", today=date(2026, 10, 15))[1] == "2026-11-14 23:59:59"          # 30 days by default


def test_floor_price_is_the_stores_own_price_formula_at_the_minimum_profit():
    assert ss.floor_price("10", "2", "18", 10) == 16.10                 # 12 x 1.10 / 0.82
    assert ss.floor_price("12", "", None, 10) == 16.10                  # blank fee = the default 18
    assert ss.floor_price("50", 0, "8.4", 25) == 68.23                  # 50 x 1.25 / 0.916
    assert ss.floor_price("", "", "18", 10) is None and ss.floor_price("0", "0", "18", 10) is None
    assert ss.min_profit_value("10") == 10 and ss.min_profit_value("off") is None and ss.min_profit_value(" OFF ") is None


def test_plan_apply_skips_priceless_out_of_stock_under_scope_and_listings_already_on_this_sale():
    listings = [rec("A", "10.00"), rec("B", None), rec("C", "20.00", stock=0), rec("D", "10.00", sale="9.00", end="2026-11-01 23:59:59"),
                rec("E", "10.00", sale="9.50", end="2026-11-01 23:59:59"), rec("", "5")]
    items, rows, c = ss.plan_apply(listings, 10, "2026-10-14 00:00:00", "2026-11-01 23:59:59", scope="in_stock")
    assert [it["sku"] for it in items] == ["A", "E"] and c == {"sale on": 1, "skip: no price": 1, "skip: out of stock": 1, "already on this sale": 1, "sale changed": 1}
    assert items[0] == {"sku": "A", "boost_marketing_commission": 0, "sale_price": "9.00", "sale_start_date": "2026-10-14 00:00:00", "sale_end_date": "2026-11-01 23:59:59"}
    assert [r[8] for r in rows] == ["sale on", "skip: no price", "skip: out of stock", "already on this sale", "sale changed"]
    items_all, _, c_all = ss.plan_apply(listings, 10, "2026-10-14 00:00:00", "2026-11-01 23:59:59", scope="all")
    assert [it["sku"] for it in items_all] == ["A", "C", "E"] and c_all["skip: no price"] == 1


def test_the_margin_floor_keeps_thin_listings_out_or_caps_them_at_the_floor():
    # price 17.45 at cost 12 / fee 18: floor at 10% profit = 16.10; a 10% sale (15.71) is under it, a 5% sale (16.58) is not
    listings = [rec("A", "17.45"), rec("B", "17.45"), rec("C", "30.00")]
    costs = {"A": (12.0, 0.0, 18.0), "C": (12.0, 0.0, 18.0)}
    items, rows, c = ss.plan_apply(listings, 10, "s", "2026-11-01 23:59:59", "all", costs, 10, "skip")
    assert [it["sku"] for it in items] == ["C"] and [r[8] for r in rows] == ["skip: below the margin floor", "skip: no cost on the sheet", "sale on"]
    assert rows[0][5:8] == ["12.00", 18.0, "16.10"] and rows[1][5:8] == ["", "", ""] and c == {"skip: below the margin floor": 1, "skip: no cost on the sheet": 1, "sale on": 1}
    items, rows, _ = ss.plan_apply(listings, 10, "s", "2026-11-01 23:59:59", "all", costs, 10, "cap")
    assert [(it["sku"], it["sale_price"]) for it in items] == [("A", "16.10"), ("C", "27.00")] and rows[0][8] == "capped at the floor"
    items, rows, _ = ss.plan_apply(listings, 5, "s", "2026-11-01 23:59:59", "all", costs, 10, "skip")
    assert [(it["sku"], it["sale_price"]) for it in items] == [("A", "16.58"), ("C", "28.50")]        # 5% clears the floor
    # cap never gives less than 1% off: floor 17.40 on a 17.45 price = skipped even in cap mode
    items, rows, _ = ss.plan_apply([rec("A", "17.45")], 10, "s", "2026-11-01 23:59:59", "all", {"A": (12.97, 0.0, 18.0)}, 10, "cap")
    assert items == [] and rows[0][8] == "skip: below the margin floor"
    # floor off (min_profit None) = no cost needed
    items, _, _ = ss.plan_apply([rec("B", "17.45")], 10, "s", "2026-11-01 23:59:59", "all", costs, None, "skip")
    assert [it["sku"] for it in items] == ["B"]


def test_costs_come_from_the_sheet_columns_with_the_default_fee_for_a_blank_cell():
    values = [["SKU", "Cost Price (£)", "Shipping Cost (£)", "Fee %"], ["111", "£12.50", "1.00", "18.00"], ["222", "9", "", ""], ["333", "", "2", "18"],
              ["4,44", "3", "0", "8.4"], ["111", "99", "", ""], ["", "5", "", ""]]
    assert ss.costs_from_values(values) == {"111": (12.5, 1.0, 18.0), "222": (9.0, 0.0, ss.DEFAULT_FEE_PERCENT), "444": (3.0, 0.0, 8.4)}
    assert ss.costs_from_values([["SKU", "Title"], ["1", "x"]]) == {} and ss.costs_from_values([]) == {}


def test_plan_cancel_ends_every_sale_with_the_past_window():
    items, rows = ss.plan_cancel([rec("A", "10.00", sale="9.00"), rec("B", "10.00"), rec("C", "10.00", sale="0")])
    assert items == [{"sku": "A", "boost_marketing_commission": 0, "sale_price": "9.00", "sale_start_date": sale_guard.PAST_START, "sale_end_date": sale_guard.PAST_END}]
    assert rows[0][8] == "sale off"


def test_state_is_active_until_its_last_day(tmp_path):
    path = tmp_path / "store_sale.json"
    ss.save_state({"percent": 10, "sale_end_date": "2026-11-01 23:59:59", "min_profit_percent": 10, "floor_mode": "skip"}, str(path))
    state = ss.load_state(str(path))
    assert ss.state_active(state, today=date(2026, 11, 1)) and not ss.state_active(state, today=date(2026, 11, 2)) and not ss.state_active(None)
    assert state["min_profit_percent"] == 10 and state["floor_mode"] == "skip"
    assert ss.clear_state(str(path)) and ss.load_state(str(path)) is None and not ss.clear_state(str(path))


def test_send_counts_per_sku_answers(monkeypatch):
    calls = []

    def put_chunk(onbuy, items, sleep=None):
        calls.append(len(items))
        return [{"sku": it["sku"], "error": "SKU does not exist"} if it["sku"] == "B" else {"sku": it["sku"]} for it in items], None

    monkeypatch.setattr(sale_guard, "put_chunk", put_chunk)
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    ok, failed, errors = ss.send(SimpleNamespace(), [ss.sale_item(s, 9.0, "x", "y") for s in "ABC"], chunk=2)
    assert (ok, failed, dict(errors), calls) == (2, 1, {"SKU does not exist": 1}, [2, 1])


def test_the_sale_guard_leaves_an_active_store_sale_alone(tmp_path):
    path = tmp_path / "store_sale.json"
    assert sale_guard.store_sale_state(str(path)) is None
    path.write_text(json.dumps({"percent": 10, "sale_end_date": "2026-11-01 23:59:59"}), encoding="utf-8")
    assert sale_guard.store_sale_state(str(path), today="2026-10-15")["percent"] == 10
    assert sale_guard.store_sale_state(str(path), today="2026-11-02") is None                 # expired = the guard ends leftovers again


END = "2026-11-25 23:59:59"
START = "2026-10-25 00:00:00"


def test_exclude_range_is_two_prices_or_nothing():
    assert ss.exclude_range("10", "20") == (10.0, 20.0) and ss.exclude_range(" 10.5 ", "20") == (10.5, 20.0) and ss.exclude_range(10, 10) == (10.0, 10.0)
    assert ss.exclude_range("", "") is None and ss.exclude_range(None, None) is None and ss.exclude_range("  ", None) is None
    for bad in (("10", ""), ("", "20"), ("20", "10"), ("x", "20"), ("-1", "5")):
        try:
            ss.exclude_range(*bad)
        except SystemExit as exc:
            assert "EXCLUDE_PRICE_FROM" in str(exc)
        else:
            raise AssertionError(f"{bad} should be refused")
    assert ss.describe_exclude(None) == "none" and ss.describe_exclude((10.0, 20.0)) == "GBP 10 to GBP 20 (both included)"


def test_the_excluded_price_range_keeps_listings_out_with_both_ends_included():
    listings = [rec("A", "9.99"), rec("B", "10.00"), rec("C", "15.00"), rec("D", "20.00"), rec("E", "20.01"), rec("F", "100.00")]
    items, rows, c = ss.plan_apply(listings, 3, START, END, "all", None, None, "skip", exclude=(10.0, 20.0))
    assert [it["sku"] for it in items] == ["A", "E", "F"]
    assert [r[8] for r in rows] == ["sale on", "skip: price in the excluded range", "skip: price in the excluded range", "skip: price in the excluded range",
                                    "sale on", "sale on"]
    assert c == {"sale on": 3, "skip: price in the excluded range": 3}
    assert [it["sale_price"] for it in items] == ["9.69", "19.41", "97.00"]                  # 3% off, to the penny
    # no exclusion = everyone, and the exclusion beats a missing cost / the floor in the report (the reason shown is the range)
    assert len(ss.plan_apply(listings, 3, START, END)[0]) == 6
    costs = {"A": (1.0, 0.0, 18.0)}
    _, rows, _ = ss.plan_apply([rec("B", "12.00")], 3, START, END, "all", costs, 10, "skip", exclude=(10.0, 20.0))
    assert rows[0][8] == "skip: price in the excluded range"


def test_a_sale_price_that_is_not_below_the_price_is_skipped():
    items, rows, c = ss.plan_apply([rec("A", "0.10"), rec("B", "0.34"), rec("C", "0.01")], 3, START, END)
    assert [it["sku"] for it in items] == ["B"] and [r[8] for r in rows] == ["skip: too cheap for a sale", "sale on", "skip: too cheap for a sale"]
    assert items[0]["sale_price"] == "0.33"


def test_a_listing_kept_out_but_carrying_this_sale_gets_it_ended_and_a_foreign_sale_is_left_alone():
    ours = rec("A", "15.00", sale="14.55", end=END)          # its price moved into 10-20 after the sale started
    foreign = rec("B", "15.00", sale="13.00", end="2026-10-31 23:59:59")
    plain = rec("C", "15.00")
    cheap = rec("D", "0.10", sale="0.10", end=END)
    items, rows, c = ss.plan_apply([ours, foreign, plain, cheap], 3, START, END, "all", None, None, "skip", exclude=(10.0, 20.0))
    assert [it["sku"] for it in items] == ["A", "D"]
    assert items[0] == {"sku": "A", "boost_marketing_commission": 0, "sale_price": "14.55", "sale_start_date": sale_guard.PAST_START, "sale_end_date": sale_guard.PAST_END}
    assert [r[8] for r in rows] == ["skip: price in the excluded range (sale ended)", "skip: price in the excluded range",
                                    "skip: price in the excluded range", "skip: too cheap for a sale (sale ended)"]
    assert rows[0][4] == ""                                                                  # no sale wanted on a listing that is kept out
    # the same for a listing that fell under its margin floor
    costs = {"E": (12.0, 0.0, 18.0)}
    items, rows, _ = ss.plan_apply([rec("E", "17.45", sale="16.93", end=END)], 3, START, END, "all", costs, 20, "skip")
    assert rows[0][8] == "skip: below the margin floor (sale ended)" and len(items) == 1 and items[0]["sale_end_date"] == sale_guard.PAST_END


def test_the_plan_reports_the_listings_on_sale_and_the_discount():
    listings = [rec("A", "100.00"), rec("B", "50.00"), rec("C", "15.00"), rec("D", None), rec("E", "40.00", sale="38.80", end=END)]
    _, rows, _ = ss.plan_apply(listings, 3, START, END, "all", None, None, "skip", exclude=(10.0, 20.0))
    assert ss.total_discount(rows) == (3, 5.7)                    # A 3.00 + B 1.50 + E (already on the sale) 1.20; C excluded, D priceless
    assert ss.total_discount([]) == (0, 0.0)


def test_a_refresh_uses_the_running_sales_own_settings_not_todays_environment():
    state = {"percent": 3, "scope": "in_stock", "sale_start_date": START, "sale_end_date": END, "min_profit_percent": 12, "floor_mode": "cap",
             "exclude_price_from": 10.0, "exclude_price_to": 20.0}
    cfg = ss.settings_from_state(state)
    assert cfg == {"percent": 3.0, "ranges": [], "start": START, "end": END, "min_profit": 12.0, "floor_mode": "cap", "scope": "in_stock", "exclude": (10.0, 20.0)}
    old = {"percent": 10, "sale_start_date": START, "sale_end_date": END, "min_profit_percent": "off", "floor_mode": "weird"}     # a state written before the exclusion existed
    cfg = ss.settings_from_state(old, scope="all")
    assert cfg["exclude"] is None and cfg["min_profit"] is None and cfg["floor_mode"] == "skip" and cfg["scope"] == "all"


def test_the_state_file_carries_the_exclusion(tmp_path):
    path = tmp_path / "store_sale.json"
    ss.save_state({"percent": 3, "sale_end_date": END, "exclude_price_from": 10.0, "exclude_price_to": 20.0}, str(path))
    state = ss.load_state(str(path))
    assert ss.exclude_range(state.get("exclude_price_from"), state.get("exclude_price_to")) == (10.0, 20.0)


def test_the_workflow_is_plan_by_default_commits_the_state_and_the_nightly_refreshes_with_the_sheet():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / "store_sale.yml").read_text(encoding="utf-8")
    assert 'default: "plan"' in text and "group: onbuy-api" in text and "contents: write" in text and "python -u store_sale.py" in text
    assert "git add -A -- store_sale.json" in text and "inputs.mode == 'apply' || inputs.mode == 'cancel'" in text
    assert "MIN_PROFIT_PERCENT: ${{ inputs.min_profit }}" in text and "FLOOR_MODE: ${{ inputs.floor_mode }}" in text and 'default: "10"' in text
    assert "EXCLUDE_PRICE_FROM: ${{ inputs.exclude_price_from }}" in text and "EXCLUDE_PRICE_TO: ${{ inputs.exclude_price_to }}" in text
    assert "PRICE_RANGES: ${{ inputs.price_ranges }}" in text and "price_ranges:" in text
    assert "GOOGLE_CREDENTIALS: ${{ secrets.GOOGLE_CREDENTIALS }}" in text and "SEMI_SHEET_NAME" in text and "YRA_Full_Feed_Master" in text
    nightly = (root / ".github/workflows/nightly_reconcile.yml").read_text(encoding="utf-8")
    assert "MODE: refresh" in nightly and nightly.index("python -u sale_guard.py") < nightly.index("python -u store_sale.py")
    refresh = nightly[nightly.index("MODE: refresh"):nightly.index("python -u store_sale.py")]
    assert "GOOGLE_CREDENTIALS" in refresh and (False == ("steps.gate.outputs.go" in nightly[nightly.index("Store sale refresh"):]))
    assert "__SHEET__" not in Path(root / "store_sale.py").read_text(encoding="utf-8")


def test_the_guards_main_ends_nothing_while_a_store_sale_is_active_and_ends_leftovers_once_it_has_expired(tmp_path, monkeypatch, capsys):
    """The nightly runs sale_guard.main() with store_sale.json in the repo root: an active sale = no ending update at all; expired = the old behaviour."""
    monkeypatch.chdir(tmp_path)

    class Stub:
        def authenticate(self):
            return True

    ended = []
    monkeypatch.setattr(sale_guard, "OnBuyClient", Stub)
    monkeypatch.setattr(sale_guard, "get_listings", lambda onbuy: [rec("A", "10.00", sale="9.70", end=END), rec("B", "10.00")])
    monkeypatch.setattr(sale_guard, "end_sales", lambda onbuy, on_sale, dry_run, max_fix: ended.append([r["sku"] for r in on_sale]) or {"planned": len(on_sale)})
    for name in ("SKU_FILE", "SWEEP_FILE", "DUMP"):
        monkeypatch.setattr(sale_guard, name, "")
    monkeypatch.setattr(sale_guard, "DRY_RUN", True)
    (tmp_path / "store_sale.json").write_text(json.dumps({"percent": 3, "sale_end_date": "2999-12-31 23:59:59"}), encoding="utf-8")
    sale_guard.main()
    out = capsys.readouterr().out
    assert ended == [] and "STORE SALE ACTIVE - 3% off until 2999-12-31" in out and "1 of 2 listings carry a sale price" in out
    (tmp_path / "store_sale.json").write_text(json.dumps({"percent": 3, "sale_end_date": "2000-01-01 23:59:59"}), encoding="utf-8")        # expired
    sale_guard.main()
    assert ended == [["A"]]
    (tmp_path / "store_sale.json").unlink()                                                                                                # no state at all
    sale_guard.main()
    assert ended == [["A"], ["A"]]


# ---------------------------------------------------------------- price ranges (user 2026-10-26: "select the price ranges of products to apply sale on them")
RANGES = [(0.0, 9.99, 5.0), (10.0, 20.0, 0.0), (20.01, None, 3.0)]


def test_ranges_parse_from_text_and_json_and_refuse_nonsense():
    assert ss.parse_ranges("0-9.99:5; 10-20:0; 20.01-:3") == RANGES
    assert ss.parse_ranges("0-9.99:5\n10-20:0\n£20.01 - : 3 %") == RANGES
    assert ss.parse_ranges('[[0, 9.99, 5], [10, 20, 0], [20.01, null, 3]]') == RANGES
    assert ss.parse_ranges("-4.99:7") == [(None, 4.99, 7.0)] and ss.parse_ranges("") == [] and ss.parse_ranges("  ;  ") == []
    assert ss.normalize_ranges([["", "", 2.5]]) == [(None, None, 2.5)]
    for bad in ("5", "a-b:3", "10-5:3", "0-9:95", "0-9:0.5", "0-9:-1", "[[1,2]]", "[1,2,3]", "[not json"):
        try:
            ss.parse_ranges(bad)
        except SystemExit:
            continue
        raise AssertionError(f"{bad!r} should be refused")


def test_the_first_range_that_contains_the_price_decides_with_both_ends_included():
    r = ss.normalize_ranges([[0, 10, 4], [10, 20, 0], [15, None, 9]])
    assert [ss.range_index(p, r) for p in (0, 9.99, 10, 10.01, 14.99, 15, 20, 20.01, 500)] == [0, 0, 0, 1, 1, 1, 1, 2, 2]     # 10 and 15 belong to the first range that holds them
    assert ss.range_index(0.0, ss.normalize_ranges([[1, 2, 3]])) is None
    assert ss.label_range(None, None) == "every price" and ss.label_range(None, 4.99) == "up to GBP 4.99" and ss.label_range(20.01, None) == "GBP 20.01 and above"
    assert ss.label_range(0, 9.99) == "GBP 0 to GBP 9.99" and ss.label_range(1000, 2500.5) == "GBP 1000 to GBP 2500.5"
    assert ss.describe_rules(None, RANGES, None) == "5% off GBP 0 to GBP 9.99; no sale on GBP 10 to GBP 20; 3% off GBP 20.01 and above"
    assert ss.describe_rules(3.0, [], (10.0, 20.0)) == "3% off every listing except GBP 10 to GBP 20" and ss.describe_rules(3.0, [], None) == "3% off every listing"


def test_each_range_gives_its_own_sale_price_and_the_rest_get_none():
    listings = [rec("A", "5.00"), rec("B", "10.00"), rec("C", "20.00"), rec("D", "20.01"), rec("E", "100.00"), rec("F", "9.995"), rec("G", None)]
    items, rows, c = ss.plan_apply(listings, None, START, END, ranges=RANGES)
    assert [(it["sku"], it["sale_price"]) for it in items] == [("A", "4.75"), ("D", "19.41"), ("E", "97.00")]
    assert [r[8] for r in rows] == ["sale on", "skip: price in the excluded range", "skip: price in the excluded range", "sale on", "sale on",
                                    "skip: outside the selected price ranges", "skip: no price"]                   # 9.995 sits in the gap between 9.99 and 10
    assert [r[9:11] for r in rows[:5]] == [["5", "0"], ["", "1"], ["", "1"], ["3", "2"], ["3", "2"]]                       # the % used and the range index
    gap = ss.plan_apply([rec("H", "9.995"), rec("I", "0.50")], None, START, END, ranges=ss.normalize_ranges([[1, 5, 10]]))
    assert [r[8] for r in gap[1]] == ["skip: outside the selected price ranges"] * 2 and gap[0] == []
    # PERCENT and the single excluded range still work without ranges, exactly as before
    items, _, c = ss.plan_apply([rec("A", "15.00"), rec("B", "30.00")], 3, START, END, exclude=(10.0, 20.0))
    assert [it["sku"] for it in items] == ["B"] and c["skip: price in the excluded range"] == 1


def test_the_floor_scope_and_ending_rules_apply_per_range():
    costs = {"A": (3.0, 0.0, 18.0), "B": (3.0, 0.0, 18.0)}
    r = ss.normalize_ranges([[0, 5, 10], [5.01, None, 3]])
    # A: price 4.10, 10% off = 3.69 < floor 3.69? cost 3 x 1.10 / 0.82 = 4.02 -> under the floor; B (5.50, 3% off = 5.34 > floor 4.02) is fine
    items, rows, _ = ss.plan_apply([rec("A", "4.10"), rec("B", "5.50")], None, START, END, "all", costs, 10, "skip", ranges=r)
    assert [r_[8] for r_ in rows] == ["skip: below the margin floor", "sale on"] and [it["sku"] for it in items] == ["B"]
    items, rows, _ = ss.plan_apply([rec("A", "4.10", sale="3.69", end=END)], None, START, END, "all", costs, 10, "skip", ranges=r)
    assert rows[0][8] == "skip: below the margin floor (sale ended)" and items[0]["sale_end_date"] == sale_guard.PAST_END
    items, _, c = ss.plan_apply([rec("A", "4.10", stock=0), rec("B", "5.50", stock=0)], None, START, END, "in_stock", ranges=r)
    assert items == [] and c == {"skip: out of stock": 2}


def test_the_range_lines_the_dashboard_reads():
    listings = [rec("A", "5.00"), rec("B", "10.00"), rec("C", "20.01"), rec("D", "100.00", sale="97.00", end=END), rec("E", "7.00"), rec("F", "0.001"), rec("G", "9.99")]
    costs = {"A": (1.0, 0.0, 18.0), "E": (50.0, 0.0, 18.0)}
    items, rows, c = ss.plan_apply(listings, None, START, END, "all", costs, 10, "skip", ranges=RANGES)
    assert [r[8] for r in rows] == ["sale on", "skip: price in the excluded range", "skip: no cost on the sheet", "skip: no cost on the sheet (sale ended)",
                                    "skip: below the margin floor", "skip: too cheap for a sale", "skip: no cost on the sheet"]
    summary, outside = ss.summarize_ranges(rows, ss.normalize_ranges(RANGES))
    assert [(s["listings"], s["on_sale"], s["no_cost"], s["below_floor"], s["other"], s["ended"]) for s in summary] == [(4, 1, 1, 1, 1, 0), (1, 0, 0, 0, 1, 0), (2, 0, 2, 0, 0, 1)]
    assert outside == 0 and [s["discount"] for s in summary] == [0.25, 0.0, 0.0]
    lines = ss.range_lines(summary, outside)
    assert lines == ["RANGE 1 | GBP 0 to GBP 9.99 | 5% | 4 listings | 1 on sale | 1 no cost | 1 below the floor | 1 other | GBP 0.25 off | 0 sale(s) ended",
                     "RANGE 2 | GBP 10 to GBP 20 | kept out | 1 listings | 0 on sale | 0 no cost | 0 below the floor | 1 other | GBP 0.00 off | 0 sale(s) ended",
                     "RANGE 3 | GBP 20.01 and above | 3% | 2 listings | 0 on sale | 2 no cost | 0 below the floor | 0 other | GBP 0.00 off | 1 sale(s) ended",
                     "RANGE other | outside every selected range | 0 listings"]
    assert [it["sku"] for it in items] == ["A", "D"] and items[1]["sale_end_date"] == sale_guard.PAST_END                    # D is kept out and loses this sale
    assert ss.count_text(c) == "skip: no cost on the sheet=2; sale on=1; skip: below the margin floor=1; skip: no cost on the sheet (sale ended)=1; "                                "skip: price in the excluded range=1; skip: too cheap for a sale=1"
    assert "{" not in "".join(lines) and "{" not in ss.count_text(c) and ss.count_text(ss.Counter()) == "none"


def test_a_refresh_reads_ranges_from_the_state_and_an_old_state_without_them_still_works():
    state = {"percent": 5, "price_ranges": [[0, 9.99, 5], [10, 20, 0], [20.01, None, 3]], "sale_start_date": START, "sale_end_date": END, "min_profit_percent": 10,
             "floor_mode": "skip", "scope": "all", "exclude_price_from": None, "exclude_price_to": None, "summary": "x"}
    cfg = ss.settings_from_state(state)
    assert cfg["ranges"] == RANGES and cfg["percent"] is None and cfg["exclude"] is None and cfg["min_profit"] == 10.0
    legacy = {"percent": 3, "scope": "all", "sale_start_date": START, "sale_end_date": END, "min_profit_percent": 10.0, "floor_mode": "skip",
              "exclude_price_from": 10.0, "exclude_price_to": 20.0}                                            # what GTV's running sale wrote on 2026-10-26
    cfg = ss.settings_from_state(legacy)
    assert cfg["ranges"] == [] and cfg["percent"] == 3.0 and cfg["exclude"] == (10.0, 20.0)
    assert ss.describe_rules(cfg["percent"], cfg["ranges"], cfg["exclude"]) == "3% off every listing except GBP 10 to GBP 20"


def test_the_guard_names_the_sale_from_its_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    class Stub:
        def authenticate(self):
            return True

    monkeypatch.setattr(sale_guard, "OnBuyClient", Stub)
    monkeypatch.setattr(sale_guard, "get_listings", lambda onbuy: [rec("A", "10.00", sale="9.70", end=END)])
    monkeypatch.setattr(sale_guard, "end_sales", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the guard must end nothing")))
    for name in ("SKU_FILE", "SWEEP_FILE", "DUMP"):
        monkeypatch.setattr(sale_guard, name, "")
    (tmp_path / "store_sale.json").write_text(json.dumps({"percent": 5, "summary": "5% off GBP 0 to GBP 9.99", "sale_end_date": "2999-12-31 23:59:59"}), encoding="utf-8")
    sale_guard.main()
    assert "STORE SALE ACTIVE - 5% off GBP 0 to GBP 9.99 until 2999-12-31" in capsys.readouterr().out
