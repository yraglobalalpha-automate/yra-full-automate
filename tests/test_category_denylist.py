"""Categories OnBuy refuses as "not a lowest level category" although its API flags them listable (found
2026-10-03: 38213 and 38188 - 34 GTV + 16 Arden creates failed on them): the denylist, its use by the category
loader and the refresh, and the remap that moves the stuck rows to a real leaf."""
import ast
import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = ROOT / "generate_xml.py"


def _load_denylist_fn():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "load_category_denylist")
    ns = {"os": __import__("os")}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SRC), "exec"), ns)
    return ns["load_category_denylist"]


def _csv_ids():
    with open(ROOT / "onbuy_categories_only.csv", newline="", encoding="utf-8") as fh:
        return {str(r.get("Category ID") or "").strip(): str(r.get("OnBuy Category Path") or "").strip()
                for r in csv.DictReader(fh)}


def test_denylist_parsing(tmp_path):
    load = _load_denylist_fn()
    f = tmp_path / "deny.txt"
    f.write_text("# header\n\n38213   # a path > with > arrows\n  38188\n#99999\nnot-an-id-but-kept  # whatever\n", encoding="utf-8")
    assert load(str(f)) == {"38213", "38188", "not-an-id-but-kept"}
    assert load(str(tmp_path / "missing.txt")) == set()


def test_the_committed_denylist_names_the_two_refused_nodes():
    ids = _load_denylist_fn()(str(ROOT / "category_denylist.txt"))
    assert {"38213", "38188"} <= ids


def test_no_denied_id_is_in_the_category_file():
    # a refresh (or a hand edit) must never bring a refused node back as a listable leaf
    ids = _csv_ids()
    denied = _load_denylist_fn()(str(ROOT / "category_denylist.txt"))
    assert not (denied & set(ids)), sorted(denied & set(ids))


def test_the_loader_and_the_refresh_both_honour_the_denylist():
    assert "denied_category_ids = load_category_denylist()" in SRC.read_text(encoding="utf-8")
    assert "category_denylist.txt" in (ROOT / "refresh_categories.py").read_text(encoding="utf-8")


pytest.importorskip("gspread")                      # the remap tool imports the Google client at module level
import remap_denied_categories as remap  # noqa: E402


@pytest.mark.parametrize("title", [
    "Kids Scooter Child Kick Flashing Adjustable LED Light Up 3 Wheel Push Folding UK",
    "ZIMX NEO MAX STUNT SCOOTER - NEO PINK",
    "besrey Kids Scooter - Big Wheels Foldable Kick Scooter with Flashing LED Lights",
])
def test_stuck_scooters_go_to_childrens_scooters(title):
    assert remap.replacement_for("38213", title) == remap.KICK
    assert remap.KICK[0] == "2338"


def test_the_streaming_device_goes_to_media_streaming_devices():
    assert remap.replacement_for("38188", "Google TV Streamer (4K) - Fast Streaming Entertainment") == remap.STREAMERS
    assert remap.replacement_for("38188", "Car stereo head unit 7 inch touch screen") is None      # no rule: left for a human


def test_rows_are_recognised_by_their_category_cell_or_by_the_error():
    assert remap.denied_id_of("Mobile Phones > Toys > Children's Scooters & Ride-On Toys", "") == "38213"
    assert remap.denied_id_of("  mobile phones > toys > children's scooters & ride-on toys ", "Synced") == "38213"
    assert remap.denied_id_of("Anything", "Failed: An error occurred: Category '38188' is not a lowest level category") == "38188"
    assert remap.denied_id_of("Toys & Games > Toys > Children's Scooters & Ride-On Toys > Children's Scooters", "Synced") is None
    assert remap.denied_id_of("Anything", "Failed: An error occurred: Category '12345' is not a lowest level category") is None


def test_the_replacement_leaves_are_listable_and_the_denied_paths_are_not():
    ids = _csv_ids()
    by_path = {p.lower(): i for i, p in ids.items()}
    for cid, (path, rules) in remap.RULES.items():
        assert path.lower() not in by_path
        for _, (tid, tpath) in rules:
            assert by_path.get(tpath.lower()) == tid
