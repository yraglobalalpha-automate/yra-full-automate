"""One-off (2026-08-27, user-approved SKU repair, GTV's 127-pair repair
ported to YRA's situation): ~344 supplier rows hold zero-less SKUs while
their real live listings carry leading zeros - the rows sit "Awaiting OnBuy
go-live" forever, pushing a SKU OnBuy doesn't know. YRA's imported twin
rows were deleted from the sheet, so the true SKU comes from the LIVE
catalog instead: for each stuck row, find the single live SKU that differs
from the row's SKU only by leading zeros, and re-key the row to it
(RAW write so the zeros survive as text; the displayed-SKU overlay shipped
alongside makes the pipeline read them). Sets Sync Status "Synced" + blank
Last OnBuy Sync so the next sync's activation pass adopts in one batch;
purges the stale zero-less mirror key. Guards: only rows whose status
starts "Awaiting OnBuy go-live"; row SKU must NOT itself be live; exactly
one live variant; target not on another row; protected skipped. Live
catalog is paged TWICE and unioned (transient short-page truncation seen
2026-08-25). DRY_RUN default on."""
import json
import os
import time

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import supabase_db
from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
SHEET_NAME = "YRA_Full_Feed_Master"


def col_letter(idx0):
    s = ""
    idx0 += 1
    while idx0:
        idx0, rem = divmod(idx0 - 1, 26)
        s = chr(65 + rem) + s
    return s


def page_live(onbuy):
    out = set()
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
                out.add(s)
        if len(items) < limit:
            break
        offset += limit
        time.sleep(0.3)
    return out


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    headers = [str(h).strip() for h in with_retry(lambda: sheet.row_values(1), what="headers", max_attempts=3)]
    col = {h: i for i, h in enumerate(headers)}
    rows = with_retry(lambda: sheet.get_all_records(), what="sheet read", max_attempts=3)
    disp = with_retry(lambda: sheet.col_values(col["SKU"] + 1), what="sku display col", max_attempts=3)

    protected = set()
    pp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "protected_skus.txt")
    if os.path.exists(pp):
        with open(pp, encoding="utf-8") as fh:
            protected = {ln.split("#", 1)[0].strip() for ln in fh if ln.split("#", 1)[0].strip()}

    def display(rownum):
        return str(disp[rownum - 1]).strip() if rownum - 1 < len(disp) else ""

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    live = page_live(onbuy)
    time.sleep(2.0)
    live |= page_live(onbuy)
    print(f"live SKUs (two-pass union): {len(live)}")
    variants = {}
    for L in live:
        variants.setdefault(L.lstrip("0"), []).append(L)

    display_use = {}
    for i in range(len(rows)):
        d = display(i + 2)
        if d:
            display_use.setdefault(d, []).append(i + 2)

    updates, mirror_purge = [], []
    repaired = no_variant = already_live = ambiguous = 0
    for i, r in enumerate(rows):
        rownum = i + 2
        status = str(r.get("Sync Status") or "").strip()
        if not status.startswith("Awaiting OnBuy go-live"):
            continue
        s = display(rownum)
        if not s or s in protected:
            continue
        if s in live:
            already_live += 1
            continue
        cands = [L for L in variants.get(s.lstrip("0"), []) if L != s]
        if not cands:
            no_variant += 1
            continue
        if len(cands) > 1:
            print(f"SKIP row {rownum} {s!r}: {len(cands)} live variants {cands}")
            ambiguous += 1
            continue
        target = cands[0]
        if target in protected or display_use.get(target):
            print(f"SKIP row {rownum} {s!r}: target {target!r} protected or already on rows {display_use.get(target)}")
            ambiguous += 1
            continue
        if repaired < 10:
            print(f"REPAIR row {rownum}: {s!r} -> {target!r}, Synced, sync cleared")
        updates.append((f"{col_letter(col['SKU'])}{rownum}", [[target]]))
        updates.append((f"{col_letter(col['Sync Status'])}{rownum}", [["Synced"]]))
        updates.append((f"{col_letter(col['Last OnBuy Sync'])}{rownum}", [[""]]))
        mirror_purge.append(s)
        repaired += 1
    print(f"rows to repair: {repaired} | still-awaiting with no live variant: {no_variant} | "
          f"awaiting but SKU already live: {already_live} | ambiguous/skipped: {ambiguous}")
    if DRY_RUN:
        print("DRY RUN - nothing written")
        return
    if not updates:
        print("nothing to repair")
        return
    for c in range(0, len(updates), 400):
        chunk = updates[c:c + 400]
        with_retry(lambda ch=chunk: sheet.batch_update(
            [{"range": rg, "values": v} for rg, v in ch], value_input_option="RAW"),
            what=f"repair writes {c}", max_attempts=3)
        print(f"written {min(c + 400, len(updates))}/{len(updates)}")
    for c in range(0, len(mirror_purge), 100):
        supabase_db.delete_products(mirror_purge[c:c + 100])
    print(f"repaired {repaired} row(s); purged {len(mirror_purge)} stale mirror key(s)")


if __name__ == "__main__":
    main()
