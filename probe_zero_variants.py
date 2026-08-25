"""One-off (2026-08-25): the 08-25 import surfaced pairs where a
pre-existing row's SKU and an imported row's SKU differ only by leading
zeros (Sheets numeric coercion stripped them from pasted SKUs). Determine,
for every such pair, which variant actually exists as a live OnBuy listing,
and what Sync Status the pre row carries - decides whether the pre rows
have been pushing to a SKU OnBuy doesn't know. Read-only."""
import json
import os
import time
from collections import Counter, defaultdict

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
BLOCK_START = int(os.environ["BLOCK_START"])


def core(sku):
    digits = "".join(ch for ch in str(sku) if ch.isdigit())
    return digits.lstrip("0") or digits


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    rows = with_retry(lambda: sheet.get_all_records(), what="sheet read", max_attempts=3)
    headers = with_retry(lambda: sheet.row_values(1), what="headers", max_attempts=3)
    sku_col = {h.strip(): i for i, h in enumerate(headers)}["SKU"] + 1
    # Displayed strings - get_all_records numericises "0102..." and 102...
    # to the same int, hiding exactly the difference this probe exists to
    # find. The displayed text keeps the imported rows' leading zeros; a
    # number-formatted pre cell displays zero-less, which is also what
    # get_all_records feeds the pipeline's by-SKU pushes.
    disp = with_retry(lambda: sheet.col_values(sku_col), what="sku display col", max_attempts=3)

    def display(rownum):
        return str(disp[rownum - 1]).strip() if rownum - 1 < len(disp) else ""

    pre, imported = {}, {}
    for i, r in enumerate(rows):
        rownum = i + 2
        sku = display(rownum)
        if not sku or not core(sku):
            continue
        if rownum < BLOCK_START:
            pre.setdefault(core(sku), []).append((rownum, sku, str(r.get("Sync Status") or "").strip()))
        elif not str(r.get("Supplier URL") or "").strip():
            imported.setdefault(core(sku), []).append((rownum, sku))
    pairs = []
    for c, imps in imported.items():
        for prn, psku, pstat in pre.get(c, []):
            for irn, isku in imps:
                if psku != isku:
                    pairs.append((prn, psku, pstat, irn, isku))
    print(f"leading-zero variant pairs (pre vs imported): {len(pairs)}")

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    live = set()
    offset, limit = 0, 100
    while True:
        def _page(off=offset):
            r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                            params={"site_id": onbuy.site_id, "limit": limit, "offset": off}, timeout=60)
            r.raise_for_status()
            return r
        body = with_retry(_page, what=f"listings page {offset}", max_attempts=4).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            break
        for it in items:
            s = str((it or {}).get("sku") or "").strip()
            if s:
                live.add(s)
        if len(items) < limit:
            break
        offset += limit
        time.sleep(0.3)
    print(f"live SKUs: {len(live)}")

    kinds = Counter()
    status_hist = Counter()
    shown = 0
    for prn, psku, pstat, irn, isku in pairs:
        p_live, i_live = psku in live, isku in live
        kind = ("BOTH-LIVE" if p_live and i_live else
                "ONLY-IMPORTED-LIVE" if i_live else
                "ONLY-PRE-LIVE" if p_live else "NEITHER")
        kinds[kind] += 1
        status_hist[(kind, pstat or "(blank)")] += 1
        if shown < 12:
            print(f"  {kind}: pre row {prn} {psku!r} [{pstat}] <> imported row {irn} {isku!r}")
            shown += 1
    print("verdict: " + " | ".join(f"{k}: {v}" for k, v in kinds.most_common()))
    print("pre-row Sync Status by verdict:")
    for (kind, st), n in status_hist.most_common():
        print(f"  {kind} | {st}: {n}")


if __name__ == "__main__":
    main()
