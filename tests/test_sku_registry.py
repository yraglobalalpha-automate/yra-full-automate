"""SKU registry helpers (2026-09-19): identity parsing + the title
similarity the registry freeze rule relies on. Extracted from source via
AST because generate_xml imports gspread/oauth2client (unavailable here) -
same pattern as test_opc_import."""
import ast
import io
import os
import re

SRC = io.open(os.path.join(os.path.dirname(__file__), "..", "generate_xml.py"),
              encoding="utf-8").read()


def _extract(*names):
    tree = ast.parse(SRC)
    keep = [n for n in tree.body if isinstance(n, (ast.FunctionDef,)) and n.name in names]
    mod = ast.Module(body=keep, type_ignores=[])
    ns = {"re": re}
    exec(compile(mod, "generate_xml_extract", "exec"), ns)
    return ns


NS = _extract("_title_similar")


def test_title_similar_same_product_reworded():
    s = NS["_title_similar"]
    assert s("Ninja SLUSHi Frozen Drinks Maker, 5 Presets",
             "SLUSHi Frozen Drinks Maker, Create Slush") >= 0.5


def test_title_similar_different_products():
    s = NS["_title_similar"]
    assert s("Gawfolk 34 Inch Ultrawide Gaming Monitor",
             "Baby Car Camera HD 1080P Mirror") < 0.5


def test_title_similar_blank_fails_open_low():
    s = NS["_title_similar"]
    assert s("", "anything") == 0.0


def test_registry_freeze_rule_in_source():
    # The freeze must key off the registry title and skip the mirror write.
    assert "SKU is registered to" in SRC
    assert "if not amazon_flag:\n            supabase_rows.append" in repr(SRC) or \
           "if not amazon_flag:" in SRC


def _identity_ns():
    import types
    tree = ast.parse(SRC)
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_supplier_identity"]
    mod = ast.Module(body=keep, type_ignores=[])
    kc = types.SimpleNamespace(parse_asin=lambda u: (re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", u or "").group(1)
                                                    if re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", u or "") else ""))
    ns = {"re": re, "keepa_client": kc,
          "supplier_of": lambda u: "Amazon" if "amazon." in str(u) else "eBay"}
    exec(compile(mod, "generate_xml_extract", "exec"), ns)
    return ns


def test_supplier_identity_amazon_and_ebay():
    f = _identity_ns()["_supplier_identity"]
    assert f("https://www.amazon.co.uk/dp/B0GHLYTLQC") == "amazon:B0GHLYTLQC"
    assert f("https://www.ebay.co.uk/itm/358403598935") == "ebay:358403598935"
    assert f("") == ""


def test_supplier_identity_ebay_variants_distinct():
    f = _identity_ns()["_supplier_identity"]
    a = f("https://www.ebay.co.uk/itm/358403598935?var=111")
    b = f("https://www.ebay.co.uk/itm/358403598935?var=222")
    assert a != b and a.startswith("ebay:358403598935:")


def test_link_uniqueness_guard_in_source():
    assert "supplier link already used on row" in SRC
    assert "all_link_counts" in SRC and "all_link_first" in SRC
