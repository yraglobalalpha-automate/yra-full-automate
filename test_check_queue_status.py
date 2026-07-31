"""Manual check: query OnBuy's /v2/queues endpoint for the real status of
past create_product submissions.

Per OnBuy support (2026-07-02): a queue_id returned from POST /v2/products
only means "accepted for async processing" - not created, not approved. This
is how we find out whether a submission actually succeeded, is still
pending, or failed (with the real validation/processing error OnBuy doesn't
surface anywhere else).

Defaults to the 5 queue_ids from the most recent sandbox test run. Override
with ONBUY_QUEUE_IDS_TO_CHECK as comma-separated "sku:queue_id" pairs to
check different ones later.
"""
import logging
import os

from onbuy_client import OnBuyClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DEFAULT_QUEUE_IDS = (
    "404834091942:6a466c8f7ea1fe07e2001824,"
    "505873497501:6a466c947af33e9c4e0d94bb,"
    "5058048349204:6a466c98c6cab839880383ca,"
    "4049023423478:6a466c9bc4f00654390b8d5e,"
    "404834092475:6a466c9dfe32b0ff9f015b82"
)

raw = os.getenv("ONBUY_QUEUE_IDS_TO_CHECK") or DEFAULT_QUEUE_IDS

pairs = []
for entry in raw.split(","):
    entry = entry.strip()
    if not entry:
        continue
    sku, _, queue_id = entry.partition(":")
    pairs.append((sku.strip(), queue_id.strip() or sku.strip()))

client = OnBuyClient(use_sandbox=True)

if not client.authenticate():
    print("FAILED to authenticate - check ONBUY_TEST_CONSUMER_KEY/ONBUY_TEST_SECRET_KEY secrets")
    raise SystemExit(1)

for sku, queue_id in pairs:
    label = f"{sku} ({queue_id})" if sku else queue_id
    try:
        result = client.check_queue(queue_id)
        print(f"\n{label}: {result}")
    except Exception as exc:
        print(f"\n{label}: FAILED - {exc}")
