"""READ-ONLY (2026-10-03): the picture data for a list of listings whose product pages never finished loading.

OnBuy's usual answer to a "stuck loading" / 404 product page is that the product IMAGES never made it onto their
system, so they ask for the image links per OPC. For every SKU in SKU_FILE this finds its row (first product tab and
the Amazon tab; leading zeros ignored - spreadsheet pastes drop them) and writes, into OUT_DIR:

  ticket_image_links.csv   OPC, SKU, Main image link, Additional image links
  reference.csv            SKU, Tab, Row, OPC, Sync Status, OnBuy Product Created, Product URL, Title
  no_opc.csv               SKUs on the sheet without a real OPC yet (nothing to hand OnBuy)
  all_found_images.csv     every found row with its images (also those without an OPC - to pair with an OnBuy lookup)
  not_on_sheet.txt         SKUs that are not on the sheet any more / at all
  duplicates.csv           SKUs found on more than one row (both rows listed in reference.csv)

Prints counts only. Writes nothing to the sheet, Supabase or OnBuy.
"""
import csv
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import sheet_tabs
from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
SKU_FILE = os.getenv("SKU_FILE") or "cleanup/image_ticket_skus_2026-10-03.txt"
OUT_DIR = os.getenv("OUT_DIR") or "out"


def norm(sku):
    """Zero-tolerant SKU key: digits only matter, leading zeros do not."""
    s = str(sku or "").replace(",", "").strip()
    return s.lstrip("0") or s


def main():
    wanted = []
    with open(SKU_FILE, encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if s and norm(s) not in {norm(w) for w in wanted}:
                wanted.append(s)
    want = {norm(s): s for s in wanted}
    print(f"SKUs asked for: {len(wanted)} (duplicates in the list removed)")

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        amz = book.worksheet(sheet_tabs.AMAZON_TAB)
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass

    found = {}                                   # norm sku -> [row dicts]
    for ws in tabs:
        values = with_retry(lambda ws=ws: ws.get_all_values(), what=f"read {ws.title}", max_attempts=3)
        header = [str(h).strip() for h in values[0]]
        ix = {h: i for i, h in enumerate(header) if h}

        def cell(r, col):
            i = ix.get(col)
            return str(r[i]).strip() if i is not None and i < len(r) else ""
        for n, r in enumerate(values[1:], start=2):
            key = norm(cell(r, "SKU"))
            if key in want:
                found.setdefault(key, []).append({
                    "sku": cell(r, "SKU"), "tab": ws.title, "row": n, "opc": cell(r, "OPC"),
                    "status": cell(r, "Sync Status")[:160], "created": cell(r, "OnBuy Product Created"),
                    "url": cell(r, "Product URL"), "title": cell(r, "Title")[:120],
                    "main": cell(r, "Image URL"), "extra": cell(r, "Additional Images")})
        print(f"tab {ws.title!r}: {len(values) - 1} rows read")

    os.makedirs(OUT_DIR, exist_ok=True)
    ticket, reference, no_opc, dups = [], [], [], []
    for key, sku in want.items():
        rows = found.get(key, [])
        if len(rows) > 1:
            dups.append([sku] + [f"{r['tab']} row {r['row']}" for r in rows])
        for r in rows:
            reference.append([r["sku"], r["tab"], r["row"], r["opc"], r["status"], r["created"], r["url"], r["title"]])
            if r["opc"].upper() in ("", "PENDING"):
                no_opc.append([r["sku"], r["tab"], r["row"], r["opc"], r["status"], r["title"]])
            else:
                ticket.append([r["opc"], r["sku"], r["main"], r["extra"]])
    missing = [sku for key, sku in want.items() if key not in found]

    def write(name, header, rows):
        with open(os.path.join(OUT_DIR, name), "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
    write("ticket_image_links.csv", ["OPC", "SKU", "Main image link", "Additional image links"], ticket)
    write("reference.csv", ["SKU", "Tab", "Row", "OPC", "Sync Status", "OnBuy Product Created", "Product URL", "Title"], reference)
    write("no_opc.csv", ["SKU", "Tab", "Row", "OPC", "Sync Status", "Title"], no_opc)
    write("duplicates.csv", ["SKU", "Found at"], dups)
    write("all_found_images.csv", ["SKU", "Tab", "Row", "OPC", "Sync Status", "Main image link", "Additional image links"],
          [[r["sku"], r["tab"], r["row"], r["opc"], r["status"], r["main"], r["extra"]]
           for key in want for r in found.get(key, [])])
    with open(os.path.join(OUT_DIR, "not_on_sheet.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(missing) + ("\n" if missing else ""))

    by_tab = {}
    for r in reference:
        by_tab[r[1]] = by_tab.get(r[1], 0) + 1
    no_main = sum(1 for t in ticket if not t[2])
    n_links = [len([u for u in t[3].split(",") if u.strip()]) for t in ticket]
    print(f"found on the sheet: {len(found)} of {len(wanted)} SKUs ({len(reference)} rows: {by_tab})")
    print(f"with an OPC (-> ticket file): {len(ticket)} | without a real OPC yet: {len(no_opc)} | "
          f"not on the sheet: {len(missing)} | on more than one row: {len(dups)}")
    print(f"ticket rows without a main image link: {no_main} | additional links per row: "
          f"{(sum(n_links) / len(n_links)) if n_links else 0:.1f} on average")
    hosts = {}
    for t in ticket:
        for u in [t[2]] + [x for x in t[3].split(",") if x.strip()]:
            host = u.split("/")[2] if "//" in u else "?"
            hosts[host] = hosts.get(host, 0) + 1
    print(f"image hosts: {dict(sorted(hosts.items(), key=lambda kv: -kv[1])[:5])}")
    statuses = {}
    for r in reference:
        k = r[4].split(":")[0][:40] or "(blank)"
        statuses[k] = statuses.get(k, 0) + 1
    print(f"Sync Status of those rows: {dict(sorted(statuses.items(), key=lambda kv: -kv[1])[:8])}")


if __name__ == "__main__":
    main()
