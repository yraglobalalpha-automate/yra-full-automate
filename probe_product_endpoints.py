"""READ-ONLY (2026-10-09): which product endpoint tells us whether a catalogue product has its pictures?

The user sees "picture loading" / 404 on the OnBuy dashboard for products we created days ago. GET /v2/listings `image_url` is the
placeholder for EVERY listing (not a signal), the public product pages are behind Cloudflare, and the queue entry only says
"success". This tries the plausible product reads for each SKU's OPC and prints what they answer - status code, the keys, and every
key that looks like an image - so a stuck product can be told from a good one. Writes nothing anywhere.

Env: SKUS (comma-separated; each is looked up with a filtered listing read to get its OPC), OPCS (comma-separated, used as given).
"""
import json
import os

from onbuy_client import BASE_URL, OnBuyClient

SKUS = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
OPCS = [s.strip() for s in (os.getenv("OPCS") or "").split(",") if s.strip()]


def show(label, resp):
    body = resp.text or ""
    print(f"  {label}: HTTP {resp.status_code} {len(body)} bytes" + (f" | {body[:170]}" if resp.status_code != 200 else ""))
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        print("    (not JSON) " + body[:200].replace("\n", " "))
        return None
    node = data
    if isinstance(data, dict) and isinstance(data.get("results"), list) and data["results"]:
        node = data["results"][0]
        print(f"    results: {len(data['results'])} item(s); first item keys: {sorted(node.keys()) if isinstance(node, dict) else type(node)}")
    elif isinstance(data, dict):
        print(f"    top-level keys: {sorted(data.keys())}")
    if isinstance(node, dict):
        for k, v in node.items():
            if "image" in k.lower() or "picture" in k.lower() or "photo" in k.lower() or k.lower() in ("thumb", "thumbnail", "published", "status", "visible"):
                print(f"    {k}: {json.dumps(v, ensure_ascii=False, default=str)[:400]}")
    else:
        print("    " + json.dumps(data, ensure_ascii=False, default=str)[:300])
    return data


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    todo = [(None, o) for o in OPCS]
    for sku in SKUS:
        r = onbuy._send("GET", f"{BASE_URL}/listings", what="listing by sku",
                        params={"site_id": onbuy.site_id, "limit": 5, "offset": 0, "filter[sku]": sku}, timeout=60)
        items = (r.json().get("results") if r.status_code == 200 else None) or []
        hit = [i for i in items if str(i.get("sku") or "").strip() == sku]
        if not hit:
            print(f"SKU {sku}: no live listing answered ({r.status_code})")
            continue
        h = hit[0]
        print(f"SKU {sku}: OPC {h.get('opc')} | created {h.get('created_at')} | product_url {h.get('product_url')} | image_url {h.get('image_url')}")
        todo.append((sku, h.get("opc") or h.get("product_encoded_id")))
    for sku, opc in todo:
        print(f"OPC {opc} (SKU {sku or '-'})")
        tries = [
            ("GET /products/{opc}", f"{BASE_URL}/products/{opc}", {"site_id": onbuy.site_id}),
            ("GET /products?search=", f"{BASE_URL}/products", {"site_id": onbuy.site_id, "search": opc}),
            ("GET /products?filter[search]=", f"{BASE_URL}/products", {"site_id": onbuy.site_id, "filter[search]": opc}),
            ("GET /products?query=", f"{BASE_URL}/products", {"site_id": onbuy.site_id, "query": opc}),
            ("GET /products?opc=", f"{BASE_URL}/products", {"site_id": onbuy.site_id, "opc": opc}),
            ("GET /products/{opc}/images", f"{BASE_URL}/products/{opc}/images", {"site_id": onbuy.site_id}),
        ]
        for label, url, params in tries:
            try:
                resp = onbuy._send("GET", url, what=label, params=params, timeout=60)
                show(label, resp)
            except Exception as exc:  # noqa: BLE001 - read-only diagnostic
                print(f"  {label}: error {str(exc)[:150]}")


if __name__ == "__main__":
    main()
