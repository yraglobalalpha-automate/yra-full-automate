"""One-off, READ-ONLY: scan every Supabase row holding a VALID category
for signs the category is wrong (2026-08-10, after two Toshiba Smart TVs
turned up in Network Bluetooth Adapters).

Tier 1 - DISAGREES WITH A CURATED TITLE PHRASE: the row's title contains a
hand-curated device phrase ("smart tv", "led tv", ...) but its stored
category differs from that phrase's category. Unambiguous; safe to repair.

Tier 2 - NO VOCABULARY OVERLAP: the stored category's own leaf words never
appear in the row's title. Suspicious but sometimes legitimate (synonyms:
earbuds -> Headphones), so SKUs in the curated autofill map are excluded
and the rest are listed for review, not repaired.

Prints only - changes nothing."""
import csv
import os

import requests

from autofill_categories import CURATED
from generate_xml import category_match_tokens, title_phrase_category
from supabase_db import TABLE_NAME

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
KEY = os.getenv("SUPABASE_SERVICE_KEY") or ""


def main():
    valid = {}
    with open("onbuy_categories_only.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = (row.get("OnBuy Category Path") or "").strip()
            if p:
                valid[p.lower()] = p

    endpoint = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}"
    headers = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
    rows, offset, page = [], 0, 1000
    while True:
        r = requests.get(endpoint, headers=headers, params={
            "select": 'SKU,Title,Category', "offset": str(offset), "limit": str(page)}, timeout=30)
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    print(f"rows scanned: {len(rows)}")

    tier1, tier2 = [], []
    for row in rows:
        sku = str(row.get("SKU") or "").strip()
        title = str(row.get("Title") or "").strip()
        cat = str(row.get("Category") or "").strip()
        if not sku or not title or cat.lower() not in valid:
            continue
        by_phrase = title_phrase_category(title)
        if by_phrase and by_phrase.strip().lower() != cat.lower():
            tier1.append((sku, title, cat, by_phrase))
            continue
        if sku in CURATED:
            continue
        leaf_tokens = category_match_tokens(cat.split(">")[-1])
        title_tokens = category_match_tokens(title)
        if leaf_tokens and not (leaf_tokens & title_tokens):
            tier2.append((sku, title, cat))

    print(f"\nTIER 1 - phrase disagreement (repairable): {len(tier1)}")
    for sku, title, cat, want in tier1[:40]:
        print(f"  {sku} | {title[:55]}")
        print(f"      stored: {cat}")
        print(f"      phrase says: {want}")
    print(f"\nTIER 2 - no title/leaf vocabulary overlap (review): {len(tier2)}")
    for sku, title, cat in tier2[:40]:
        print(f"  {sku} | {title[:55]} | {cat}")
    print("\nDONE - read-only, nothing modified.")


if __name__ == "__main__":
    main()
