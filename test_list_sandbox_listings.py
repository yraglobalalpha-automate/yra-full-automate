"""Manual check: list everything actually in the OnBuy sandbox catalog.

OnBuy's seller dashboard only shows the production catalog - there is no
visible UI for sandbox data, even when the dashboard session and the sandbox
API credentials belong to the same account. This is the only direct way to
see what our sandbox test pushes have actually done.
"""
import logging
import os

from onbuy_client import OnBuyClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

TEST_SKUS = {s.strip() for s in os.getenv("ONBUY_API_TEST_SKUS", "").split(",") if s.strip()}

client = OnBuyClient(use_sandbox=True)

if not client.authenticate():
    print("FAILED to authenticate - check ONBUY_TEST_CONSUMER_KEY/ONBUY_TEST_SECRET_KEY secrets")
    raise SystemExit(1)

try:
    result = client.list_listings()
except Exception as exc:
    print("FAILED:", exc)
    raise SystemExit(1)

listings = result.get("listings", []) if isinstance(result, dict) else []
print(f"\nTotal listings in sandbox catalog: {len(listings)}")

found_skus = {str(item.get("sku", "")).strip() for item in listings}

if TEST_SKUS:
    print("\nChecking your ONBUY_API_TEST_SKUS against the sandbox catalog:")
    for sku in TEST_SKUS:
        if sku in found_skus:
            match = next(item for item in listings if str(item.get("sku", "")).strip() == sku)
            print(f"  FOUND    {sku}: price={match.get('price')} stock={match.get('stock')}")
        else:
            print(f"  MISSING  {sku}: not in sandbox catalog yet (may still be pending OnBuy's review queue)")
