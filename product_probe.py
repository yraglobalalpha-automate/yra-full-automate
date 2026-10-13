"""READ-ONLY probe (2026-10-13): does OnBuy's PRODUCT API expose a listing's image URLs (and would a HEAD on them reveal the broken "404" images)?
For the SKUs given: the listing (filtered GET) -> its OPC -> GET /v2/products/{opc} and GET /v2/products?filter[opc]=..., print the answer's keys and every
image-looking field, then the HTTP status of each image URL. Writes nothing. Env: SKUS."""
import json
import os
import re
import time

import requests

from onbuy_client import BASE_URL, OnBuyClient

SKUS = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"}


def walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, node


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    for sku in SKUS:
        lst = onbuy.get_listing(sku) or {}
        opc = str(lst.get("opc") or lst.get("product_encoded_id") or "")
        print(f"\n== {sku}: opc {opc} | listing image_url {lst.get('image_url')} | product_url {str(lst.get('product_url') or '')[:100]}")
        for label, url, params in ((f"GET /products/{opc}", f"{BASE_URL}/products/{opc}", {"site_id": onbuy.site_id}),
                                   ("GET /products?filter[opc]", f"{BASE_URL}/products", {"site_id": onbuy.site_id, "filter[opc]": opc, "limit": 5}),
                                   ("GET /products?filter[sku]", f"{BASE_URL}/products", {"site_id": onbuy.site_id, "filter[sku]": sku, "limit": 5})):
            r = onbuy._send("GET", url, what=label, params=params, timeout=60)
            print(f"  {label}: HTTP {r.status_code} {r.text[:160]!r}" if r.status_code != 200 else f"  {label}: HTTP 200, {len(r.text)} chars")
            if r.status_code != 200:
                continue
            try:
                body = r.json()
            except ValueError:
                continue
            flat = list(walk(body))
            keys = sorted({re.sub(r"\[\d+\]", "[]", p) for p, _ in flat})
            print(f"     keys: {keys[:60]}")
            imgs = [(p, v) for p, v in flat if isinstance(v, str) and re.search(r"\.(jpe?g|png|webp|gif)(\?|$)", v, re.I)]
            for p, v in imgs[:12]:
                try:
                    h = requests.head(v, headers=UA, timeout=20, allow_redirects=True)
                    st = h.status_code
                    if st in (403, 405):
                        st = requests.get(v, headers=UA, timeout=20, stream=True).status_code
                except requests.RequestException as exc:
                    st = f"ERR {str(exc)[:40]}"
                print(f"     image {p}: HTTP {st} {v[:120]}")
                time.sleep(0.3)
            if not imgs:
                print("     (no image-looking field)")
        time.sleep(1)


if __name__ == "__main__":
    main()
