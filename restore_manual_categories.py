"""One-off (2026-08-22): restore hand-set categories that the pipeline
re-matched after the 2026-08-21 category-file refresh (OnBuy renamed 107
paths and retired 6; rows holding those texts were treated as invalid and
re-mapped by the matcher in the first run after the push - "Mapped row N"
lines in that run's log).

For each candidate row (ROWS env, or every row when ROWS is empty) it
collects three views and proposes a restore:
  sheet   - the Category cell as it is now (the matcher's guess if hit)
  mirror  - the Supabase mirror's Category (only re-upserted when the main
            loop processes the row, so it often still holds the previous
            value)
  by-id   - the path the row's Category ID maps to in the CURRENT file
            (renames keep the id, so this is the previous choice under its
            new name); ids retired by OnBuy cannot be resolved this way
Decision: if mirror and by-id agree (after mapping the mirror's text through
the previous file -> id -> current path) restore that; if only one source is
available restore it; if they disagree list for the user. Anything restored
gets Category (+ Category ID when resolvable) written. DRY_RUN default on."""
import csv
import io
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import supabase_db
from retry_utils import with_retry

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
ROWS = os.getenv("ROWS") or ""
HERE = os.path.dirname(os.path.abspath(__file__))


def parse_rows(spec):
    out = []
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return set(out)


def load_csv(name):
    p = os.path.join(HERE, name)
    if not os.path.exists(p):
        return {}, {}
    by_path, by_id = {}, {}
    with io.open(p, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            cid = str(r.get("Category ID") or "").strip()
            path = str(r.get("OnBuy Category Path") or "").strip()
            if cid and path:
                by_path[path.lower()] = (cid, path)
                by_id.setdefault(cid, path)
    return by_path, by_id


def col_letter(idx):
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def main():
    cur_by_path, cur_by_id = load_csv("onbuy_categories_only.csv")
    prev_by_path, prev_by_id = load_csv("onbuy_categories_previous.csv")
    print(f"current categories: {len(cur_by_id)} | previous file: {len(prev_by_id)}")

    def to_current(text):
        """A category text from any era -> (current path, id) or (None, None)."""
        t = str(text or "").strip()
        if not t:
            return None, None
        if t.lower() in cur_by_path:
            cid, path = cur_by_path[t.lower()]
            return path, cid
        if t.lower() in prev_by_path:
            cid = prev_by_path[t.lower()][0]
            if cid in cur_by_id:
                return cur_by_id[cid], cid
        leaf = t.rsplit(" > ", 1)[-1].strip().lower()
        hits = [p for p in cur_by_id.values() if p.rsplit(" > ", 1)[-1].strip().lower() == leaf]
        if len(hits) == 1:
            return hits[0], cur_by_path[hits[0].lower()][0]
        return None, None

    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    headers = with_retry(lambda: sheet.row_values(1), what="headers", max_attempts=3)
    col = {h.strip(): i for i, h in enumerate(headers)}
    rows = with_retry(lambda: sheet.get_all_records(), what="sheet read", max_attempts=3)
    wanted = parse_rows(ROWS)
    skus = [str(rows[r - 2].get("SKU") or "").strip() for r in sorted(wanted) if 0 <= r - 2 < len(rows)] if wanted \
        else [str(r.get("SKU") or "").strip() for r in rows]
    mirror = {}
    try:
        mirror = supabase_db.fetch_full_rows([s for s in skus if s]) or {}
    except Exception as exc:
        print(f"supabase read failed: {exc}")
    print(f"rows considered: {len(skus)} | mirror rows: {len(mirror)}")

    updates, restored, listed = [], 0, 0
    for i, r in enumerate(rows):
        rownum = i + 2
        if wanted and rownum not in wanted:
            continue
        sku = str(r.get("SKU") or "").strip()
        sheet_cat = str(r.get("Category") or "").strip()
        sheet_id = str(r.get("Category ID") or "").strip().split(".")[0]
        m = mirror.get(sku) or {}
        mirror_cat = str(m.get("Category") or "").strip()
        # a previous value on a category OnBuy has since RETIRED was remapped
        # on purpose (remap_gone_categories) - leave those alone
        _pm = prev_by_path.get(mirror_cat.lower()) if mirror_cat else None
        if _pm and _pm[0] not in cur_by_id and mirror_cat.lower() not in cur_by_path:
            continue
        mirror_path, mirror_id = to_current(mirror_cat) if mirror_cat and mirror_cat != sheet_cat else (None, None)
        byid_path = cur_by_id.get(sheet_id) if sheet_id else None
        if byid_path and byid_path.lower() == sheet_cat.lower():
            byid_path = None   # id agrees with the current text -> nothing to restore from it
        proposal, source = None, ""
        if mirror_path and byid_path:
            if mirror_path.lower() == byid_path.lower():
                proposal, source = mirror_path, "mirror+id agree"
            else:
                listed += 1
                print(f"ROW {rownum} SKU {sku} | DISAGREE | sheet now: {sheet_cat[:60]} | mirror: {mirror_cat[:60]} -> {mirror_path[:60]} | by-id: {byid_path[:60]}")
                continue
        elif mirror_path:
            proposal, source = mirror_path, "mirror"
        elif mirror_cat and mirror_cat != sheet_cat and not mirror_path:
            # the previous value does not resolve to any current category -
            # still the human's text: put it back as typed (the pipeline now
            # keeps unresolved text and flags it instead of remapping)
            proposal, source = mirror_cat, "mirror (unresolved text, kept as typed)"
        elif byid_path:
            proposal, source = byid_path, "by-id"
        if not proposal or proposal.lower() == sheet_cat.lower():
            # nothing to restore from the mirror/id. Classify for the report:
            # SAME-ID = the row's Category ID still maps to the text now in the
            # cell (OnBuy rename only - meaning unchanged); UNKNOWN = no usable
            # previous value left (mirror already re-upserted, id blank/equal)
            # -> needs the sheet's version history.
            kind = "SAME-ID" if (sheet_id and cur_by_id.get(sheet_id, "").lower() == sheet_cat.lower()) else "UNKNOWN"
            print(f"ROW {rownum} SKU {sku} | {kind} | now: {sheet_cat[:80]} | title: {str(r.get('Title') or '')[:60]}")
            continue
        restored += 1
        new_id = cur_by_path.get(proposal.lower(), ("", ""))[0]
        print(f"ROW {rownum} SKU {sku} | RESTORE [{source}] | {sheet_cat[:60]!r} -> {proposal[:70]!r}")
        updates.append({"range": f"{col_letter(col['Category'])}{rownum}", "values": [[proposal]]})
        if new_id and "Category ID" in col:
            updates.append({"range": f"{col_letter(col['Category ID'])}{rownum}", "values": [[new_id]]})
    print(f"restore proposals: {restored} | disagreements listed: {listed}")
    if DRY_RUN:
        print("DRY RUN - nothing written")
        return
    for c0 in range(0, len(updates), 200):
        chunk = updates[c0:c0 + 200]
        with_retry(lambda b=chunk: sheet.batch_update([dict(u) for u in b]), what="restore write", max_attempts=3)
    print(f"written: {len(updates)} cell range(s)")


if __name__ == "__main__":
    main()
