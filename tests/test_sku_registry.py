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
