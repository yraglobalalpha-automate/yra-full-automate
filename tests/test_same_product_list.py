"""same_product_skus.txt: listings checked by hand whose live page IS the row's product (2026-10-10)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import held_skus

ROOT = Path(__file__).resolve().parents[1]
LIST = ROOT / "same_product_skus.txt"


def test_both_nightly_scripts_honour_the_list():
    for name in ("scan_content_mismatch.py", "zero_stock_mismatched.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "held_skus.is_held(sku, SAME_PRODUCT)" in text, name
        assert 'same_product_skus.txt' in text, name


@pytest.mark.skipif(not LIST.exists(), reason="this store has no same-product list")
def test_the_list_parses_and_has_no_repeats():
    lines = [ln.split("#", 1)[0].strip() for ln in LIST.read_text(encoding="utf-8").splitlines()]
    skus = [ln for ln in lines if ln]
    assert skus and len(skus) == len(set(skus))
    assert held_skus.load_skus(str(LIST)) >= set(skus)


@pytest.mark.skipif(not LIST.exists(), reason="this store has no same-product list")
def test_listings_deleted_as_wrong_content_are_not_on_the_list():
    skus = held_skus.load_skus(str(LIST))
    assert not ({"4855008105181-zzz-17", "0000245681316-zzz-65"} & skus)
