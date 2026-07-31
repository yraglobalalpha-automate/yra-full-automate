"""Manual smoke test: create a product via OnBuy's API.

Runs against the sandbox (ONBUY_TEST_CONSUMER_KEY/ONBUY_TEST_SECRET_KEY), not
the live seller account - safe to trigger anytime. Previously this hardcoded
site_id=2000/seller_id=48948 (the real production account) directly in the
payload regardless of which credentials were used - now it goes through
OnBuyClient like everything else, using whichever seller_id/site_id belongs
to the sandbox credentials.
"""
import logging

from onbuy_client import OnBuyClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DESCRIPTION = """
<p><strong>4 Glue Stick Washable Adhesives Home School Office Craft</strong></p>

<p>Brand new 4 pack of glue sticks suitable for home, school, office and craft projects.</p>

<p>These glue sticks provide a clean and easy way to bond paper, cardboard, photographs and other lightweight materials. The formula is solvent free, PVC free, non-toxic and washable from clothing at 30&deg;C.</p>

<p><strong>Features:</strong></p>

<ul>
<li>Pack of 4 glue sticks</li>
<li>Strong adhesive formula</li>
<li>Acid free</li>
<li>Solvent free</li>
<li>PVC free</li>
<li>Non-toxic</li>
<li>Easy to apply</li>
<li>Suitable for paper, card and craft projects</li>
<li>Suitable for home, school and office use</li>
<li>Washable from clothing at 30&deg;C</li>
</ul>

<p><strong>Package Includes:</strong></p>

<ul>
<li>4 x Glue Sticks</li>
</ul>

<p>Please note: Product packaging may vary.</p>
"""

client = OnBuyClient(use_sandbox=True)

if not client.authenticate():
    print("FAILED to authenticate - check ONBUY_TEST_CONSUMER_KEY/ONBUY_TEST_SECRET_KEY secrets")
    raise SystemExit(1)

print("Token generated")

try:
    result = client.create_product(
        sku="194343045790-test",
        ean="5056345008268",
        title="4 GLUE STICK Washable Adhesives Home School Office Decorative Craft",
        description=DESCRIPTION,
        brand="Krd Ltd",
        category_id=9474,
        price=7.50,
        main_image="https://i.ebayimg.com/images/g/8L0AAOSwvIhjaOh7/s-l1600.jpg",
        additional_images=["https://i.ebayimg.com/images/g/UJIAAOSwKCZjaOiL/s-l140.jpg"],
    )
except Exception as exc:
    print("FAILED:", exc)
    raise SystemExit(1)

print("SUCCESS:", result)
if "queue_id" in result:
    print("QUEUE ID:", result["queue_id"])
if "uid" in result:
    print("UID:", result["uid"])
