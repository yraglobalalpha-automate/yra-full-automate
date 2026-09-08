"""READ-ONLY: did the clean-up cut too much?

The sentence rules assume a sentence. An eBay description written as one
long unpunctuated run of specs is a single "sentence" to them, so one
policy word could delete the lot - it did for GTV rows 5691/5692, caught
before those rows were written because they fell below the empty
threshold. Rows that ended up merely SHORT were written, and this checks
whether any of them lost real product text.

For every row it compares the stored description against what the CURRENT
sanitizer would produce from the row's own eBay source text is not
available here, so it uses a different signal: a stored description that
is very short, or that lost most of its length relative to its title and
peers, is flagged for a look. It also re-runs the current sanitizer over
the stored text - if that now removes nothing, the row is stable; if it
still shrinks a lot, the row is reported.

Writes overstrip_audit.csv. Changes nothing.
"""
import csv
import json
import os
import re

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from sanitize import sanitize_description

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
SHORT = int(os.getenv("SHORT_CHARS") or "120")   # a real description is longer than this
OUT = "overstrip_audit.csv"


def plain(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(html or ""))).strip()


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    sheet = gspread.authorize(creds).open(SHEET_NAME).sheet1
    values = sheet.get_all_values()
    headers = [h.strip() for h in values[0]]
    idx = {h.lower(): i for i, h in enumerate(headers)}
    i_desc, i_sku, i_title = idx.get("description"), idx.get("sku"), idx.get("title")
    if i_desc is None:
        raise SystemExit("no Description column")

    def cell(row, i):
        return (row[i] if i is not None and i < len(row) else "").strip()

    short, unstable, ok, blank = [], [], 0, 0
    for r in range(2, len(values) + 1):
        row = values[r - 1]
        desc = cell(row, i_desc)
        if not desc:
            blank += 1
            continue
        body = plain(desc)
        again = plain(sanitize_description(desc))
        rec = {"row": r, "sku": cell(row, i_sku), "title": cell(row, i_title)[:60],
               "stored_chars": len(body), "after_current_rules": len(again),
               "stored_text": body[:300]}
        if len(body) < SHORT:
            short.append(rec)
        elif again and len(again) < len(body) * 0.5:
            # the current rules would still halve it - worth a human look
            unstable.append(rec)
        else:
            ok += 1

    with open(OUT, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["flag", "row", "sku", "title", "stored_chars",
                                           "after_current_rules", "stored_text"])
        w.writeheader()
        for rec in short:
            w.writerow({"flag": "very short", **rec})
        for rec in unstable:
            w.writerow({"flag": "current rules would still halve it", **rec})

    print(f"rows with a description: {ok + len(short) + len(unstable)} | blank: {blank}")
    print(f"looks fine: {ok}")
    print(f"VERY SHORT (< {SHORT} chars): {len(short)}")
    print(f"current rules would still halve: {len(unstable)}")
    for rec in (short[:25] + unstable[:15]):
        print(f"   row {rec['row']:>5} {rec['sku']:<15} {rec['stored_chars']:>5} chars | {rec['title'][:45]}")
        print(f"        {rec['stored_text'][:150]}")


if __name__ == "__main__":
    main()
