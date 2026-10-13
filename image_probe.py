"""READ-ONLY probe (2026-10-13): can OnBuy's PUBLIC product page tell us which listings have broken images (the "image loading / 404" the user sees on many
listings)? The seller API cannot (GET /v2/listings image_url is always the placeholder), so the public page is the only unauthenticated signal.

For N in-stock sheet rows (or SKUS): the listing's product_url from a filtered GET, then the public page fetched like a browser; prints the HTTP status,
whether it is a bot challenge, the image URLs the page carries (og:image + onbuy CDN images), and the HTTP status of each image URL - plus the sheet's own
Image URL status for comparison. Writes nothing anywhere. Env: SKUS (comma-separated), N (8), SHEET_NAME."""
import json
import os
import re
import time

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

import sheet_tabs
from onbuy_client import OnBuyClient

SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Full_Feed_Master"
N = int(os.getenv("N") or "8")
WANT = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8", "Accept-Language": "en-GB,en;q=0.9"}
IMG_RE = re.compile(r"https?://[^\s\"'<>]+?\.(?:jpe?g|png|webp)(?:\?[^\s\"'<>]*)?", re.I)


def sheet_rows(n):
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(SHEET_NAME)
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        amz = book.worksheet(sheet_tabs.AMAZON_TAB)
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    out = []
    for ws in tabs:
        values = ws.get_all_values()
        h = [str(x).strip() for x in values[0]]
        ix = {k: h.index(k) for k in ("SKU", "Stock", "Image URL", "Title") if k in h}
        for r in values[1:]:
            cell = lambda k: (str(r[ix[k]]).strip() if k in ix and ix[k] < len(r) else "")   # noqa: E731
            sku = cell("SKU").replace(",", "")
            if not sku:
                continue
            if WANT:
                if sku not in WANT:
                    continue
            elif cell("Stock") in ("", "0"):
                continue
            out.append((sku, cell("Image URL"), f"{ws.title}: {cell('Title')[:45]}"))
            if len(out) >= n:
                return out
    return out


def status_of(url):
    try:
        r = requests.head(url, headers=UA, timeout=20, allow_redirects=True)
        if r.status_code in (403, 405):
            r = requests.get(url, headers=UA, timeout=20, allow_redirects=True, stream=True)
        return r.status_code, r.headers.get("content-type", ""), r.headers.get("content-length", "")
    except requests.RequestException as exc:
        return "ERR", str(exc)[:60], ""


def main():
    rows = sheet_rows(len(WANT) if WANT else N)
    print(f"rows: {len(rows)}")
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    for sku, sheet_img, title in rows:
        lst = onbuy.get_listing(sku) or {}
        url = str(lst.get("product_url") or "")
        print(f"\n== {sku} {title!r} | listing image_url {str(lst.get('image_url') or '')[:70]} | product_url {url}")
        if not url:
            continue
        try:
            r = requests.get(url, headers=UA, timeout=30, allow_redirects=True)
        except requests.RequestException as exc:
            print(f"   page: ERR {str(exc)[:100]}")
            continue
        text = r.text
        challenge = any(m in text for m in ("cf-chl", "Just a moment", "challenge-platform", "captcha", "Access denied"))
        og = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', text)
        imgs = [u for u in dict.fromkeys(IMG_RE.findall(text)) if "onbuy" in u.lower()]
        print(f"   page: HTTP {r.status_code}, {len(text)} chars, final {r.url[:90]} | challenge {challenge} | title {re.search(r'<title>(.*?)</title>', text, re.S).group(1).strip()[:70] if re.search(r'<title>(.*?)</title>', text, re.S) else '?'!r}")
        print(f"   og:image: {og.group(1)[:120] if og else None}")
        print(f"   onbuy image urls on the page: {len(imgs)}")
        for u in ([og.group(1)] if og else []) + imgs[:6]:
            st, ct, cl = status_of(u)
            print(f"     {st} {ct[:20]} {cl:>8} {u[:110]}")
            time.sleep(0.3)
        st, ct, cl = status_of(sheet_img) if sheet_img else ("-", "", "")
        print(f"   sheet Image URL: {st} {ct[:20]} {sheet_img[:100]}")
        time.sleep(1.0)


if __name__ == "__main__":
    main()
