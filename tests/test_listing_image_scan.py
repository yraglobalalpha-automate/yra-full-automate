"""listing_image_scan: what counts as "no picture on OnBuy", and the tally the scan prints."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import listing_image_scan as scan  # noqa: E402

PLACEHOLDER = "https://onbuy.com/files/default/product/thumb/default.jpg"
REAL = "https://s3-eu-west-1.amazonaws.com/images.onbuy.com/product/abc/large/pic.jpg"


def test_placeholder_urls_are_recognised():
    assert scan.has_placeholder_image(PLACEHOLDER)
    assert scan.has_placeholder_image("https://www.onbuy.com/files/default/product/large/default.jpg")
    assert scan.has_placeholder_image("")
    assert scan.has_placeholder_image(None)


def test_real_picture_is_not_a_placeholder():
    assert not scan.has_placeholder_image(REAL)
    assert not scan.has_placeholder_image("https://images.onbuy.com/product/xyz/default-colour-chart.png")


def test_tally_splits_by_day_and_counts_real_pictures():
    listings = [
        {"sku": "1", "image_url": PLACEHOLDER, "created_at": "2026-10-02 06:06:39"},
        {"sku": "2", "image_url": PLACEHOLDER, "created_at": "2026-10-02 07:00:00"},
        {"sku": "3", "image_url": PLACEHOLDER, "created_at": "2026-09-29 07:13:34"},
        {"sku": "4", "image_url": REAL, "created_at": "2026-08-01 10:00:00"},
    ]
    none, by_day, real = scan.tally(listings)
    assert [it["sku"] for it in none] == ["1", "2", "3"]
    assert by_day == {"2026-09-29": 1, "2026-10-02": 2}
    assert real == 1


def test_norm_ignores_leading_zeros_and_commas():
    assert scan.norm("0123456789012") == scan.norm("123456789012")
    assert scan.norm(" 1,234 ") == "1234"
