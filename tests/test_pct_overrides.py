"""resolve_pct_cell / _pct_value: what counts as the automation's own
Fee % / Profit % value (blank, freshly computed, or last written to the
mirror) versus a manual override a person typed - which must survive
untouched run after run. Extracted from source, same pattern as
test_carry_forward (no gspread import needed)."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "generate_xml.py"


def _funcs(*names):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    ns = {}
    for name in names:
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
    return [ns[n] for n in names]


_pct_value, resolve_pct_cell = _funcs("_pct_value", "resolve_pct_cell")


def test_pct_value_parsing():
    assert _pct_value("15") == 15.0
    assert _pct_value(" 12.5% ") == 12.5
    assert _pct_value(7) == 7.0
    assert _pct_value("") is None
    assert _pct_value(None) is None
    assert _pct_value("abc") is None
    assert _pct_value("120") is None                 # a fee past 100% is nonsense
    assert _pct_value("150", hi=500) == 150.0        # a profit past 100% is legitimate
    assert _pct_value("-5") is None


def test_blank_cell_is_the_automations_to_fill():
    assert resolve_pct_cell("", [15.0], "15") is None
    assert resolve_pct_cell(None, [15.0], None) is None


def test_matching_computed_value_is_not_an_override():
    assert resolve_pct_cell("15.00", [15.0, None], None) is None
    assert resolve_pct_cell("7", [7.0], "7") is None
    # within rounding of what the automation would write
    assert resolve_pct_cell("40.00", [40.0], "40") is None


def test_stale_automation_value_recognised_via_mirror():
    # basis changed (fee 20 -> 7): the cell still holds the old automation
    # value, which the mirror remembers - not an override, overwrite it.
    assert resolve_pct_cell("20.00", [7.0], "20") is None
    # profit band moved (40 -> 80) but the mirror knows 40 was ours
    assert resolve_pct_cell("40.00", [80.0], "40", hi=500) is None


def test_typed_number_is_an_override():
    assert resolve_pct_cell("10", [15.0, 8.0], "15") == 10.0
    assert resolve_pct_cell("50", [40.0], "40", hi=500) == 50.0
    # override survives even when the mirror holds the automation's value
    assert resolve_pct_cell("12.5", [15.0], "15") == 12.5


def test_override_beyond_sanity_is_ignored():
    assert resolve_pct_cell("250", [15.0], "15") is None          # fee cap
    assert resolve_pct_cell("250", [40.0], "40", hi=500) == 250.0  # profit ok
