"""One-off, READ-ONLY diagnostic: for each SKU given, show exactly what the
category matcher saw and why it refused (or what it would pick). Reuses the
real helpers from generate_xml (tokenize/category_match_tokens/guards) and
replicates the two decision stages verbatim - eBay Type first, then the
title scorer - but instrumented, printing every candidate considered and
every rule that eliminated it.

Modifies nothing: no Sheet writes, no Supabase writes, no OnBuy calls.
The only network calls are Supabase reads and one eBay Browse fetch per SKU
(to get the live Type item-specific, which isn't stored anywhere).

Usage: DIAG_SKUS=sku1,sku2 python diagnose_categories.py
"""
import csv
import os
import re

import supabase_db
from generate_xml import (
    _GUARDED_SUBTREES,
    _stem,
    category_match_tokens,
    empty_ebay_response,
    get_ebay_data,
    get_ebay_token,
    tokenize,
)

raw = os.getenv("DIAG_SKUS") or ""
target_skus = [s.strip() for s in raw.split(",") if s.strip()]
if not target_skus:
    print("DIAG_SKUS is empty - nothing to check.")
    raise SystemExit(0)

# ---- category file, same structures as generate_xml.main() ----
onbuy_categories = []
with open("onbuy_categories_only.csv", newline="", encoding="utf-8") as csvfile:
    for row in csv.DictReader(csvfile):
        if row.get("OnBuy Category Path"):
            onbuy_categories.append(row["OnBuy Category Path"])

category_tokens = {}
category_leaf_tokens = {}
category_leaf_phrase = {}
category_guard = {}
for _path in onbuy_categories:
    category_tokens[_path] = category_match_tokens(_path)
    category_leaf_tokens[_path] = category_match_tokens(_path.split(">")[-1])
    category_leaf_phrase[_path] = tuple(
        _stem(w) for w in re.findall(r"\w+", _path.split(">")[-1].lower()))
    _low = _path.strip().lower()
    category_guard[_path] = next(
        (req for prefix, req in _GUARDED_SUBTREES if _low.startswith(prefix)), None)

print(f"Loaded {len(onbuy_categories)} OnBuy categories")


def extract_type(data):
    """Same extraction the main loop does (generate_xml.py ~542)."""
    for aspect in data.get("localizedAspects", []):
        if aspect.get("name", "").strip().lower() in ("type", "product type", "item type"):
            values = aspect.get("value")
            return str(values[0] if isinstance(values, list) else values).strip()
    return ""


def diagnose_type_stage(product_type, title, description):
    """type_category(), instrumented. Returns the winner or None."""
    type_tokens = category_match_tokens(product_type)
    print(f"  Type tokens: {sorted(type_tokens) or '(none)'}")
    if not type_tokens:
        print("  -> Type stage SKIPPED (no usable Type tokens)")
        return None
    corroborating = category_match_tokens(f"{title}\n{description}")
    candidates = []
    killed_by_corroboration = []
    for category_path in onbuy_categories:
        leaf = category_leaf_tokens[category_path]
        if not leaf:
            continue
        leaf_word_count = len(tokenize(category_path.split(">")[-1]))
        covers_whole_leaf = leaf <= type_tokens and leaf_word_count == len(leaf)
        strong_submatch = len(type_tokens) >= 2 and (leaf <= type_tokens or type_tokens <= leaf)
        if covers_whole_leaf or strong_submatch:
            extras = leaf - type_tokens
            if extras and not extras <= corroborating:
                killed_by_corroboration.append((category_path, sorted(extras - corroborating)))
                continue
            overlap = len(leaf & type_tokens)
            if overlap:
                candidates.append((overlap, -len(leaf - type_tokens),
                                   -len(category_path), category_path))
    for path, missing in killed_by_corroboration[:5]:
        print(f"    eliminated (leaf words not in listing text: {missing}): {path}")
    if not candidates:
        print("  -> Type stage: NO candidates")
        return None
    candidates.sort(reverse=True)
    for c in candidates[:5]:
        print(f"    candidate overlap={c[0]} extra_leaf_words={-c[1]}: {c[3]}")
    if len(candidates) == 1 or candidates[0][:2] > candidates[1][:2]:
        print(f"  -> Type stage WINNER: {candidates[0][3]}")
        return candidates[0][3]
    print("  -> Type stage: TIE between top candidates - refused (falls to title scorer)")
    return None


def diagnose_title_stage(title, current_category, description):
    """map_onbuy_category()'s scorer, instrumented."""
    title_words = category_match_tokens(f"{title}\n{current_category}")
    desc_words = category_match_tokens(description) - title_words
    all_words = title_words | desc_words
    print(f"  Title tokens: {sorted(title_words)}")
    if not all_words:
        print("  -> Title stage: no tokens at all - keeps current category")
        return None

    def weight(word):
        return len(word) * (3 if word in title_words else 1)

    scored = []
    for category_path in onbuy_categories:
        required = category_guard[category_path]
        if required and not (all_words & required):
            continue
        hits = all_words & category_tokens[category_path]
        if not hits:
            continue
        leaf_hits = hits & category_leaf_tokens[category_path]
        score = sum(weight(w) for w in hits) + 2 * sum(weight(w) for w in leaf_hits)
        scored.append((score, bool(hits & title_words), category_path, sorted(hits)))
    scored.sort(reverse=True)
    for score, has_title_hit, path, hits in scored[:5]:
        print(f"    score={score:<4} title_hit={has_title_hit}  {path}  (hits: {hits})")
    if not scored:
        print("  -> Title stage: NO scoring candidates at all")
        return None
    best_score, best_has_title_hit, best_match, _ = scored[0]
    if best_score >= 9 and best_has_title_hit:
        print(f"  -> Title stage WINNER: {best_match}")
        return best_match
    reason = []
    if best_score < 9:
        reason.append(f"best score {best_score} < 9 threshold")
    if not best_has_title_hit:
        reason.append("no TITLE word among the hits (description-only)")
    print(f"  -> Title stage REFUSED: {'; '.join(reason)}")

    # Leaf-named-in-title fallback - mirrors generate_xml.py (2026-08-05,
    # phrase-containment version after the Post Boxes incident).
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
        for c in covered[:5]:
            print(f"    leaf-in-title candidate (coverage={c[0]}): {c[2]}")
        if len(covered) == 1 or covered[0][0] > covered[1][0]:
            print(f"  -> Leaf-in-title WINNER: {covered[0][2]}")
            return covered[0][2]
        print("  -> Leaf-in-title: TIE at top coverage - refused")
    else:
        print("  -> Leaf-in-title: no leaf fully named in the title")
    return None


rows = supabase_db.fetch_full_rows(target_skus)
token = get_ebay_token()

for sku in target_skus:
    print("\n" + "=" * 78)
    row = rows.get(sku)
    if row is None:
        print(f"SKU {sku}: NO Supabase row - cannot diagnose")
        continue
    title = str(row.get("Title") or "")
    description = str(row.get("Description") or "")
    current_category = str(row.get("Category") or "").strip()
    url = str(row.get("Supplier URL") or "")
    print(f"SKU {sku}")
    print(f"  Title: {title[:110]}")
    print(f"  Current Category: {current_category!r}")
    product_type = ""
    try:
        available, data = get_ebay_data(url, token)
        if available:
            product_type = extract_type(data)
            print(f"  eBay Type: {product_type!r}")
        else:
            print("  eBay Type: (listing unavailable on eBay)")
    except Exception as exc:  # diagnostic only - never let one SKU kill the report
        print(f"  eBay Type: fetch failed ({exc})")
    by_type = diagnose_type_stage(product_type, title, description)
    if not by_type:
        diagnose_title_stage(title, current_category, description)

print("\nDONE - read-only, nothing was modified.")
