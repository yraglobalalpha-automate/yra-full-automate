"""Shared harness: extracts the category matcher's module-level helpers
straight from generate_xml.py's source (no gspread import needed) and
replicates the two nested decision stages - the title scorer and the
leaf-named-in-title phrase fallback - exactly as generate_xml.main()
builds them. If the nested logic in generate_xml.py changes, change the
replica here in the same commit; test_matcher.py exists precisely to make
that drift loud instead of silent."""
import ast
import csv
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "generate_xml.py"


def _extract_helpers():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    wanted = {"tokenize", "_stem", "category_match_tokens", "title_phrase_category"}
    nodes = [n for n in tree.body
             if (isinstance(n, ast.FunctionDef) and n.name in wanted)
             or (isinstance(n, ast.Assign) and any(
                 getattr(t, "id", None) in ("_CATEGORY_STOPWORDS", "_GUARDED_SUBTREES",
                                            "TITLE_PHRASE_CATEGORIES")
                 for t in n.targets))]
    ns = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SRC), "exec"), ns)
    return ns


_ns = _extract_helpers()
tokenize = _ns["tokenize"]
_stem = _ns["_stem"]
category_match_tokens = _ns["category_match_tokens"]
_GUARDED_SUBTREES = _ns["_GUARDED_SUBTREES"]
title_phrase_category = _ns["title_phrase_category"]

onbuy_categories = []
with open(REPO / "onbuy_categories_only.csv", newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if row.get("OnBuy Category Path"):
            onbuy_categories.append(row["OnBuy Category Path"])

category_tokens = {p: category_match_tokens(p) for p in onbuy_categories}
category_leaf_tokens = {p: category_match_tokens(p.split(">")[-1]) for p in onbuy_categories}
category_leaf_phrase = {
    p: tuple(_stem(w) for w in re.findall(r"\w+", p.split(">")[-1].lower()))
    for p in onbuy_categories
}
category_guard = {
    p: next((req for prefix, req in _GUARDED_SUBTREES if p.strip().lower().startswith(prefix)), None)
    for p in onbuy_categories
}


def map_onbuy_category(title, current_category, description=""):
    """Replica of generate_xml.main()'s scorer + phrase fallback (the Type
    stage is exercised separately in production; every regression case here
    had an empty eBay Type). Returns (result, stage)."""
    by_phrase = title_phrase_category(title)
    if by_phrase:
        return by_phrase, "title-phrase"

    title_words = category_match_tokens(f"{title}\n{current_category}")
    desc_words = category_match_tokens(description) - title_words
    all_words = title_words | desc_words
    if not all_words:
        return current_category, "no-tokens"

    def weight(word):
        return len(word) * (3 if word in title_words else 1)

    best_match, best_score, best_has_title_hit = None, 0, False
    for category_path in onbuy_categories:
        required = category_guard[category_path]
        if required and not (all_words & required):
            continue
        hits = all_words & category_tokens[category_path]
        if not hits:
            continue
        leaf_hits = hits & category_leaf_tokens[category_path]
        score = sum(weight(w) for w in hits) + 2 * sum(weight(w) for w in leaf_hits)
        if score > best_score:
            best_score, best_match = score, category_path
            best_has_title_hit = bool(hits & title_words)

    if best_match and best_score >= 9 and best_has_title_hit:
        return best_match, "scorer"

    title_seq = [_stem(w) for w in re.findall(r"\w+", str(title).lower())]
    covered = []
    for category_path in onbuy_categories:
        required = category_guard[category_path]
        if required and not (all_words & required):
            continue
        phrase = category_leaf_phrase[category_path]
        n = len(phrase)
        if not n or n > len(title_seq):
            continue
        if any(tuple(title_seq[i:i + n]) == phrase
               for i in range(len(title_seq) - n + 1)):
            covered.append((n, -len(category_path), category_path))
    if covered:
        covered.sort(reverse=True)
        if len(covered) == 1 or covered[0][0] > covered[1][0]:
            return covered[0][2], "leaf-in-title"

    return current_category, "refused"
