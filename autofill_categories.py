"""One-off: auto-fill the Category column for rows the strict matcher
refused ("Failed: no matching OnBuy category"). Scores each row's title
(+description backup) against the official OnBuy category file and writes
the best path; rows with no defensible match are listed for a human.
User decision 2026-08-01: auto-assignment beats employee retyping time -
this relaxed scorer exists ONLY in this tool, the pipeline's strict
matcher is unchanged.

DRY_RUN=1 (default): print every choice, change nothing.
"""
import csv
import json
import logging
import os
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("onbuy_sync")

SHEET_NAME = "YRA_Full_Feed_Master"
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")

_STOP = {"the", "a", "an", "and", "or", "for", "of", "with", "in", "on", "to", "by",
         "new", "set", "pack", "pcs", "uk", "x", "s", "m", "l", "xl", "mm", "cm", "b"}


def _stem(w):
    # light plural fold so "TV" meets "TVs" and "Fryer" meets "Fryers"
    if len(w) > 3 and w.endswith("es"):
        return w[:-2]
    if len(w) > 2 and w.endswith("s"):
        return w[:-1]
    return w


def toks(text):
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if len(w) > 1 and w not in _STOP and not w.isdigit()}


def main():
    with open("onbuy_categories_only.csv", newline="", encoding="utf-8-sig") as f:
        paths = [r["OnBuy Category Path"].strip() for r in csv.DictReader(f)
                 if (r.get("OnBuy Category Path") or "").strip()]
    cats = []
    for path in paths:
        segs = [s.strip() for s in path.split(">")]
        leaf = toks(segs[-1])
        rest = toks(" ".join(segs[:-1]))
        cats.append((path, leaf, rest))
    logger.info("Official categories loaded: %d", len(cats))

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]), scope)
    sheet = gspread.authorize(creds).open(SHEET_NAME).sheet1
    data = sheet.get_all_records()
    headers = [str(h).strip() for h in sheet.row_values(1)]
    col_map = {col: idx + 1 for idx, col in enumerate(headers) if col}

    def col_letter(n):
        out = ""
        while n:
            n, rem = divmod(n - 1, 26)
            out = chr(65 + rem) + out
        return out

    updates, unmatched, chosen = [], [], []
    for idx, row in enumerate(data, start=2):
        status = str(row.get("Sync Status") or "")
        if "no matching OnBuy category" not in status:
            continue
        title_t = toks(row.get("Title"))
        desc_t = toks(str(row.get("Description") or "")[:400])
        best, best_score = None, 0.0
        for path, leaf, rest in cats:
            score = 3.0 * len(title_t & leaf) + 1.0 * len(title_t & rest)                     + 0.5 * len(desc_t & leaf)
            # tie-break toward deeper, more specific leaves
            if score > best_score + 1e-9 or (abs(score - best_score) < 1e-9 and best
                                             and len(path) > len(best)):
                best, best_score = path, score
        sku = str(row.get("SKU") or "").strip()
        if not best or best_score < 3.0:  # not even one solid leaf word from the title
            unmatched.append((idx, sku, str(row.get("Title") or "")[:60]))
            continue
        chosen.append((idx, sku, str(row.get("Title") or "")[:55], best, best_score))
        updates.append({"range": f"{col_letter(col_map['Category'])}{idx}", "values": [[best]]})
        if "Sync Status" in col_map:
            updates.append({"range": f"{col_letter(col_map['Sync Status'])}{idx}", "values": [[""]]})

    for idx, sku, title, path, score in chosen:
        logger.info("row %d %s | %s  ->  %s  (score %.1f)", idx, sku, title, path, score)
    for idx, sku, title in unmatched:
        logger.info("UNMATCHED row %d %s | %s (needs a human)", idx, sku, title)
    logger.info("choices: %d, unmatched: %d", len(chosen), len(unmatched))
    if DRY_RUN:
        logger.info("DRY RUN - nothing written")
        return
    if updates:
        sheet.batch_update(updates)
        logger.info("Categories written for %d row(s) - they retry on the next run", len(chosen))


if __name__ == "__main__":
    main()
