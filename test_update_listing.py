"""Manual smoke test: update a listing's price/stock via OnBuy's API.

Runs against the sandbox (ONBUY_TEST_CONSUMER_KEY/ONBUY_TEST_SECRET_KEY), not
the live seller account - safe to trigger anytime.
"""
import logging

from onbuy_client import OnBuyClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

client = OnBuyClient(use_sandbox=True)

if not client.authenticate():
    print("FAILED to authenticate - check ONBUY_TEST_CONSUMER_KEY/ONBUY_TEST_SECRET_KEY secrets")
    raise SystemExit(1)

try:
    result = client.update_listing(sku="194343045790-test", price=7.5, stock=10)
except Exception as exc:
    print("FAILED:", exc)
    raise SystemExit(1)

print("SUCCESS:", result)
