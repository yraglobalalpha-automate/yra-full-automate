"""delivery_templates.py - every listing on the delivery template of its supplier (user 2026-10-13): eBay rows -> the eBay template, Amazon rows -> the Amazon
template, AliExpress rows (the semi sheet) -> the AliExpress template, everything else (legacy) stays where it is; with CLEAR_HANDLING a sheet-sourced
listing's own handling time is taken off."""
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import delivery_templates as dt


def test_classify_reads_the_supplier_off_the_link():
    assert dt.classify("https://www.ebay.co.uk/itm/123456789012") == "ebay"
    assert dt.classify("https://www.amazon.co.uk/dp/B0ABCDEFGH") == "amazon"
    assert dt.classify("https://amzn.eu/d/abc") == "amazon"
    assert dt.classify("https://www.aliexpress.com/item/1005.html") == "ali"
    assert dt.classify("") == "none" and dt.classify("https://example.com/x") == "other"


def test_only_ebay_and_amazon_listings_have_a_wanted_template():
    assert dt.wanted_template("ebay", "54810", "54809") == "54810"
    assert dt.wanted_template("amazon", "54810", "54809") == "54809"
    for cls in ("ali", "none", "other", "legacy"):
        assert dt.wanted_template(cls, "54810", "54809") is None
    assert dt.wanted_template("ebay", "", "54809") is None                       # no id configured = nothing moves


def listing(sku, template, price="9.99", stock=3, handling=0):
    return {"sku": sku, "delivery_template_id": template, "price": price, "stock": stock, "handling_time": handling}


def test_plan_moves_only_what_is_off_its_suppliers_template():
    listings = [listing("E1", 52506), listing("E2", 54810), listing("A1", 52506), listing("A2", 54810), listing("L1", 52506), listing("", 52506)]
    classes = {"E1": "ebay", "E2": "ebay", "A1": "amazon", "A2": "amazon"}
    moves = dt.plan(listings, classes, "54810", "54809")
    assert [(m[0], m[2], m[3]) for m in moves] == [("E1", "52506", "54810"), ("A1", "52506", "54809"), ("A2", "54810", "54809")]
    assert moves[0][4:6] == ("9.99", 3) and moves[0][7] is None                 # the listing's own price and stock travel; handling untouched


def test_clear_handling_takes_the_own_handling_time_off_sheet_sourced_listings_only():
    listings = [listing("E1", 54810, handling=2), listing("E2", 54810, handling="0"), listing("A1", 54809, handling="5"), listing("L1", 52506, handling=7)]
    classes = {"E1": "ebay", "E2": "ebay", "A1": "amazon"}
    moves = dt.plan(listings, classes, "54810", "54809", clear_handling=True)
    assert [(m[0], m[3], m[6], m[7]) for m in moves] == [("E1", "54810", 2, 0), ("A1", "54809", 5, 0)]      # L1: supplier unknown, E2: nothing to clear
    assert dt.plan(listings, classes, "54810", "54809", clear_handling=False) == []


def test_a_listing_off_its_template_and_with_its_own_handling_moves_both_in_one_go():
    moves = dt.plan([listing("E1", 52506, handling=4)], {"E1": "ebay"}, "54810", "54809", clear_handling=True)
    assert moves == [("E1", "ebay", "52506", "54810", "9.99", 3, 4, 0)]


def test_census_counts_templates_and_handling_per_supplier_class():
    listings = [listing("E1", 52506, handling=0), listing("E2", 54810, handling=0), listing("L1", 52506, handling=5)]
    lines = dt.census(listings, {"E1": "ebay", "E2": "ebay"})
    assert "listings: 3" in lines[0] and "'52506': 2" in lines[0] and "'5': 1" in lines[0]
    assert any(l.strip().startswith("ebay") and "2 listings" in l and "'54810': 1" in l for l in lines)
    assert any(l.strip().startswith("legacy") and "1 listings" in l for l in lines)


def test_put_listings_sends_the_listings_own_price_and_stock_plus_only_the_fields_asked_for():
    sent = {}

    def _send(method, url, what=None, json=None, timeout=None):
        sent.update(method=method, url=url, body=json)
        return SimpleNamespace(status_code=200, text="ok",
                               json=lambda: {"results": [{"sku": "E1", "status": "ok"}, {"sku": "E2", "error": "SKU does not exist"}, {"sku": "E3", "status": "ok"}]})

    onbuy = SimpleNamespace(_send=_send, site_id=2000, seller_id=7)
    status, outcome, _ = dt.put_listings(onbuy, [("E1", 9.99, 3, "54810", None), ("E2", 5.0, 0, "54810", 0), ("E3", 1.0, 1, None, 0)])
    assert status == 200 and outcome == {"E1": "", "E2": "SKU does not exist", "E3": ""}
    assert sent["method"] == "PUT" and sent["url"].endswith("/listings/by-sku")
    assert sent["body"]["listings"][0] == {"sku": "E1", "price": 9.99, "stock": 3, "boost_marketing_commission": 0, "delivery_template_id": 54810}
    assert sent["body"]["listings"][1] == {"sku": "E2", "price": 5.0, "stock": 0, "boost_marketing_commission": 0, "delivery_template_id": 54810, "handling_time": 0}
    assert sent["body"]["listings"][2] == {"sku": "E3", "price": 1.0, "stock": 1, "boost_marketing_commission": 0, "handling_time": 0}
    assert dt.put_templates is dt.put_listings


def test_handling_csv_lists_only_the_listings_with_their_own_handling_time_longest_first(tmp_path):
    listings = [listing("E1", 54810, handling=2), listing("E2", 54810, handling=0), listing("L1", 52506, handling="7")]
    path = tmp_path / "h.csv"
    assert dt.write_handling_csv(listings, {"E1": "ebay", "E2": "ebay"}, str(path)) == 2
    rows = path.read_text(encoding="utf-8").splitlines()
    assert rows[0].startswith("sku,supplier,handling_time_days") and rows[1].startswith("L1,legacy,7") and rows[2].startswith("E1,ebay,2")


def test_num_tolerates_strings_and_blanks():
    assert dt.num("243.83", float) == 243.83 and dt.num("6", int) == 6 and dt.num("6.0", int) == 6 and dt.num("", int) is None and dt.num(None, float) is None
    assert dt.own_handling({"handling_time": "5"}) == 5 and dt.own_handling({"handling_time": None}) == 0 and dt.own_handling({}) == 0


def test_aliexpress_listings_go_on_the_aliexpress_template_when_one_is_configured():
    assert dt.wanted_template("ali", "35109", "22069", "43190") == "43190"
    assert dt.wanted_template("ali", "35109", "22069", "") is None                     # no id = the Ali rule is off
    listings = [listing("S1", 22069, handling=5), listing("S2", 43190, handling=0), listing("S3", 43190, handling="5"), listing("L1", 22069, handling=5)]
    moves = dt.plan(listings, {"S1": "ali", "S2": "ali", "S3": "ali"}, "35109", "22069", clear_handling=True, ali="43190")
    assert [(m[0], m[2], m[3], m[6], m[7]) for m in moves] == [("S1", "22069", "43190", 5, 0), ("S3", "43190", "43190", 5, 0)]   # L1: supplier unknown
    assert dt.plan(listings, {"S1": "ali"}, "35109", "22069", ali="") == []            # no Ali template configured: nothing moves


def test_semi_sheet_rows_are_classified_by_their_supplier_column_or_their_link():
    values = [["SKU", "Supplier URL", "Supplier", "Status"],
              ["111", "https://www.aliexpress.com/item/1.html", "AliExpress", "ACTIVE"],
              ["222", "https://www.ebay.co.uk/itm/2", "", "ACTIVE"],
              ["333", "https://www.aliexpress.com/item/3.html", "", "ACTIVE"],
              ["", "https://www.aliexpress.com/item/4.html", "AliExpress", "ACTIVE"],
              ["111", "https://www.ebay.co.uk/itm/9", "eBay", "ACTIVE"],
              ["4,44"]]
    book = SimpleNamespace(sheet1=SimpleNamespace(title="Sheet1", get_all_values=lambda: values))
    assert dt.semi_classes(book) == {"111": "ali", "222": "ebay", "333": "ali", "444": "none"}


def test_the_full_sheets_row_wins_and_an_unreadable_semi_sheet_is_only_a_warning(capsys):
    semi_values = [["SKU", "Supplier URL", "Supplier"], ["E1", "https://www.aliexpress.com/item/1.html", "AliExpress"], ["S1", "https://www.aliexpress.com/item/2.html", ""]]
    book = SimpleNamespace(sheet1=SimpleNamespace(title="Sheet1", get_all_values=lambda: semi_values))
    client = SimpleNamespace(open=lambda name: book)
    assert dt.add_semi_classes({"E1": "ebay"}, client, "Makstore_Feed_Master") == {"E1": "ebay", "S1": "ali"}

    def _boom(name):
        raise RuntimeError("no access")

    assert dt.add_semi_classes({"E1": "ebay"}, SimpleNamespace(open=_boom), "Makstore_Feed_Master") == {"E1": "ebay"}
    assert "WARNING: semi sheet" in capsys.readouterr().out
    assert dt.add_semi_classes({"E1": "ebay"}, SimpleNamespace(open=_boom), "") == {"E1": "ebay"}      # no semi sheet configured = nothing read


def test_moves_without_a_numeric_price_are_set_aside_before_batching():
    listings = [listing("E1", 22069), listing("E2", 22069, price=None), listing("E3", 22069, price=""), listing("E4", 22069, price="0")]
    moves = dt.plan(listings, {s: "ebay" for s in ("E1", "E2", "E3", "E4")}, "35109", "22069")
    sendable, priceless = dt.sendable_moves(moves)
    assert [m[0] for m in sendable] == ["E1"] and priceless == 3                # 0.00 = a never-live listing the PUT refuses; None / blank likewise
    assert dt.sendable_moves([]) == ([], 0)


def test_the_workflow_is_plan_by_default_and_serialised_with_the_other_onbuy_jobs():
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "delivery_templates.yml").read_text(encoding="utf-8")
    assert "group: onbuy-api" in text and 'default: "plan"' in text and "python -u delivery_templates.py" in text
    assert "CLEAR_HANDLING: ${{ inputs.clear_handling }}" in text


def test_a_manual_run_takes_the_template_ids_from_its_inputs_then_the_variables_then_the_nightly_fallback():
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "delivery_templates.yml").read_text(encoding="utf-8")
    assert "TEMPLATE_EBAY: ${{ inputs.template_ebay || vars.DELIVERY_TEMPLATE_EBAY }}" in text
    assert "TEMPLATE_AMAZON: ${{ inputs.template_amazon || vars.DELIVERY_TEMPLATE_AMAZON }}" in text
    assert "TEMPLATE_ALI: ${{ inputs.template_ali || vars.DELIVERY_TEMPLATE_ALI }}" in text
    for key in ("template_ebay", "template_amazon", "template_ali"):
        assert re.search(r"      %s:\n        description: [^\n]*\n        required: false\n        default: \"\"" % key, text), key        # an empty default lets the variable apply
