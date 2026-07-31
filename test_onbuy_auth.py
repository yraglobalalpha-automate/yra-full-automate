"""Manual smoke test: confirm OnBuy sandbox credentials authenticate.

Runs against the sandbox (ONBUY_TEST_CONSUMER_KEY/ONBUY_TEST_SECRET_KEY), not
the live seller account - safe to trigger anytime.
"""
from onbuy_client import OnBuyClient

client = OnBuyClient(use_sandbox=True)
ok = client.authenticate()

if ok:
    print("SUCCESS")
    print("Token received:", client._token[:40] + "...")
else:
    print("FAILED - check ONBUY_TEST_CONSUMER_KEY/ONBUY_TEST_SECRET_KEY secrets")
