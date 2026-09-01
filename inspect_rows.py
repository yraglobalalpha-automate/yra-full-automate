"""One-off, READ-ONLY (2026-08-21): user reports GTV listings whose title
and picture match the supplier link but whose specs/description belong to
the NEXT sheet row's product (rows 4442-4444, 4514-4531, 4534-4538, ...).
Three views, all read-only:
  ROWS        - dump the named rows (SKU, link, title, description head,
                image, status/OPC/sync) and score each row's description
                against its own title vs the previous/next rows' titles.
  SCAN_ALL    - sweep every row with the same scoring and list rows whose
                description matches a neighbour's title better than its own.
  FETCH_ONBUY - for the ROWS' SKUs, find the live listing (name +
                product_url) via the listings API, then fetch the public
                product page and print the description OnBuy shows.
The API exposes no product description, so the page is the only source."""
import json
import os
import re
import time

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry

ROWS = os.getenv("ROWS") or ""
SCAN_ALL = (os.getenv("SCAN_ALL") or "").strip().lower() in ("1", "true", "yes")
FETCH_ONBUY = (os.getenv("FETCH_ONBUY") or "").strip().lower() in ("1", "true", "yes")
SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"

STOP = set("the a an and or of for with to in on at by from is are this that it its as be "
           "new uk free delivery fast high quality set pack pcs pc x cm mm kg g ml l".split())


def parse_rows(spec):
    out = []
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def norm_tokens(s):
    s = re.sub(r"<[^>]+>", " ", str(s or ""))
    toks = re.sub(r"[^a-z0-9]+", " ", s.lower()).split()
    return {t for t in toks if len(t) > 2 and t not in STOP}


def sim(desc_tokens, title):
    tt = norm_tokens(title)
    if not tt or not desc_tokens:
        return 0.0
    return round(len(desc_tokens & tt) / len(tt), 2)


def text_head(s, n=220):
    s = re.sub(r"<[^>]+>", " ", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = with_retry(lambda: client.open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    values = with_retry(lambda: sheet.get_all_values(), what="sheet read", max_attempts=3)
    headers = values[0]

    def col(name):
        for i, h in enumerate(headers):
            if h.strip().lower() == name.lower():
                return i
        return None

    c = {k: col(k) for k in ["SKU", "Supplier URL", "Title", "Description", "Image URL",
                             "Sync Status", "OPC", "Last OnBuy Sync", "Selling Price (£)",
                             "Cost Price (£)", "Last Checked Time"]}
    if c["Supplier URL"] is None:
        for i, h in enumerate(headers):
            hl = h.lower()
            if "supplier" in hl or ("ebay" in hl and "url" in hl):
                c["Supplier URL"] = i
                break
    print(f"columns: {c}")
    print(f"sheet rows: {len(values) - 1}")

    def cell(r, key):
        i = c.get(key)
        row = values[r - 1] if 0 < r - 1 < len(values) else []
        return row[i] if i is not None and i < len(row) else ""

    def score(r):
        d = norm_tokens(cell(r, "Description"))
        own = sim(d, cell(r, "Title"))
        prev = sim(d, cell(r - 1, "Title")) if r > 2 else 0.0
        nxt = sim(d, cell(r + 1, "Title")) if r < len(values) else 0.0
        return own, prev, nxt

    rows_wanted = parse_rows(ROWS) if ROWS else []
    skus_env = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
    if skus_env:
        want_s = set(skus_env)
        for rr in range(2, len(values) + 1):
            if cell(rr, "SKU").strip() in want_s:
                rows_wanted.append(rr)
        print(f"SKUS resolved: {len(rows_wanted)} row(s) for {len(skus_env)} SKU(s)")
    ROWS_EFFECTIVE = ",".join(str(x) for x in rows_wanted)
    if rows_wanted:
        for r in rows_wanted:
            own, prev, nxt = score(r)
            verdict = "OK" if own >= max(prev, nxt) else ("NEXT-ROW?" if nxt > prev else "PREV-ROW?")
            print("")
            print(f"ROW {r} | SKU {cell(r, 'SKU')} | {cell(r, 'Sync Status')} | OPC {cell(r, 'OPC')} | sync {cell(r, 'Last OnBuy Sync')} | checked {cell(r, 'Last Checked Time')}")
            print(f"  link : {cell(r, 'Supplier URL')[:110]}")
            print(f"  title: {cell(r, 'Title')[:140]}")
            print(f"  desc : {text_head(cell(r, 'Description'))}")
            print(f"  image: {cell(r, 'Image URL')[:110]}")
            print(f"  price: cost {cell(r, 'Cost Price (£)')} sell {cell(r, 'Selling Price (£)')}")
            print(f"  desc~title own={own} prev={prev} next={nxt} -> {verdict}")

    if SCAN_ALL:
        flagged = []
        for r in range(2, len(values) + 1):
            if not cell(r, "SKU").strip() or not cell(r, "Description").strip():
                continue
            own, prev, nxt = score(r)
            best_nb = max(prev, nxt)
            if best_nb >= 0.5 and best_nb > own + 0.2:
                flagged.append((r, cell(r, "SKU"), own, prev, nxt))
        print("")
        print(f"SCAN_ALL: {len(flagged)} row(s) whose description matches a neighbour title better than its own")
        for r, sku, own, prev, nxt in flagged:
            print(f"  FLAG row {r} sku {sku} own={own} prev={prev} next={nxt} -> {'NEXT' if nxt > prev else 'PREV'}")

    if FETCH_ONBUY and rows_wanted:
        want = {}
        for r in rows_wanted:
            s = cell(r, "SKU").strip()
            if s:
                want[s] = r
        onbuy = OnBuyClient()
        if not onbuy.authenticate():
            raise SystemExit("OnBuy auth failed")
        found = {}
        offset, limit = 0, 100
        while want and len(found) < len(want):
            def _page(off=offset):
                resp = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                                   params={"site_id": onbuy.site_id, "limit": limit, "offset": off},
                                   timeout=60)
                resp.raise_for_status()
                return resp
            body = with_retry(_page, what=f"listings page {offset}", max_attempts=3).json()
            items = body.get("results") if isinstance(body, dict) else body
            if not isinstance(items, list) or not items:
                break
            for it in items:
                s = str(it.get("sku") or "").strip()
                if s in want and s not in found:
                    found[s] = it
            offset += limit
            time.sleep(0.3)
        print("")
        print(f"FETCH_ONBUY: {len(found)}/{len(want)} SKU(s) located on the account")
        for s, r in want.items():
            it = found.get(s)
            if not it:
                print("")
                print(f"ROW {r} SKU {s}: NOT FOUND in live listings")
                continue
            print("")
            print(f"ROW {r} SKU {s} | onbuy name: {str(it.get('name'))[:140]} | price {it.get('price')} stock {it.get('stock')} | opc {it.get('opc')}")
            print(f"  codes: {it.get('product_codes')} | created_at: {it.get('created_at')} | listing_id: {it.get('product_listing_id')}")
            url = it.get("product_url")
            print(f"  url: {url}")
            try:
                pg = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                html = pg.text
                print(f"  page [{pg.status_code}] {len(html)} bytes")
                desc = ""
                for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
                    try:
                        ld = json.loads(m.group(1))
                    except Exception:
                        continue
                    blocks = ld if isinstance(ld, list) else [ld]
                    for b in blocks:
                        if isinstance(b, dict) and b.get("description"):
                            desc = b["description"]
                            break
                    if desc:
                        break
                if not desc:
                    m = re.search(r'(?is)(?:product\s+description|description)</?[^>]*>(.{0,1200})', html)
                    desc = m.group(1) if m else ""
                print(f"  onbuy desc: {text_head(desc, 300)}")
                d = norm_tokens(desc)
                print(f"  onbuy-desc ~ own title={sim(d, cell(r, 'Title'))} prev={sim(d, cell(r - 1, 'Title'))} next={sim(d, cell(r + 1, 'Title'))}")
                print(f"  sheet desc: {text_head(cell(r, 'Description'), 160)}")
            except Exception as exc:
                print(f"  page fetch failed: {str(exc)[:160]}")
            time.sleep(1)


if __name__ == "__main__":
    main()
