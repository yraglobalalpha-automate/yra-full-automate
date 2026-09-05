"""The description policy: product content and nothing else. No prices,
store navigation, shipping/returns terms, links or seller branding.

The three fixtures are real GTV descriptions (rows 5807, 5808, 5812 on
2026-09-05) from one seller template that walked straight past the old
phrase blocklist; a sweep found 1,772 of 5,465 descriptions in the same
state. The shape rules exist because of them, so they are pinned here
alongside the specs that must survive - a sanitizer that empties the
description is as wrong as one that leaves the junk in."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sanitize import sanitize_description  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _clean(name):
    return _text(sanitize_description((FIX / name).read_text(encoding="utf-8")))


def _absent(text, *phrases):
    low = text.lower()
    left = [p for p in phrases if p.lower() in low]
    assert not left, f"seller junk survived: {left}"


def _present(text, *phrases):
    low = text.lower()
    missing = [p for p in phrases if p.lower() not in low]
    assert not missing, f"product content lost: {missing}"


def test_5807_cross_sell_menu_prices_and_policy_go():
    t = _clean("seller_junk_5807.html")
    _absent(t, "GBP 20.99", "Other Hot Item", "Store Home", "Shop Category", "UK STOCK",
            "PaymentShippingReturnsContact", "We ship to UK only", "pleasant shopping experience",
            "opportunity to resolve", "All rights reserved", "do more business")
    _present(t, "Bone Conduction", "IPX8", "32GB", "Music Time", "Charging Time",
             "Transmission Distance", "1 x Charging Cable")


def test_5808_returns_policy_and_copyright_footer_go():
    t = _clean("seller_junk_5808.html")
    _absent(t, "Changed your mind", "10-30% deduction", "Faulty item", "incorrect item",
            "receieved", "Volo Origin", "All Rights Reserved", "Powered by", "Total Digital Stores",
            "must be returned", "manufacturer fault")


def test_5812_brand_story_goes_specs_stay():
    t = _clean("seller_junk_5812.html")
    _absent(t, "affordable prices", "we have established", "competitive prices",
            "Total Digital Stores", "satisfied clients", "our range", "They offer the latest")
    _present(t, "GVPS110", "Box Contains", "Earphones", "Instruction manual")


def test_specs_that_look_like_junk_survive():
    keep = [
        "<p>USB 3.0 transfer speeds up to 5.0GBPS.</p>",          # not pounds sterling
        "<p>Delivers 500W of continuous power.</p>",              # the verb, not the noun
        "<p>Haptic feedback on every key press.</p>",
        "<p>Comes with a 1 Year Manufacturer Warranty.</p>",
        "<p>Enjoy up to 10 hours of playback on a single charge.</p>",
        "<p>Supplied with a US plug adapter.</p>",                 # uppercase US is a spec
        "<p>Waterproof: IPX8. Built-in Memory: 32GB.</p>",
    ]
    for html in keep:
        out = _text(sanitize_description(html))
        assert out, f"spec wrongly removed: {html}"
        assert _text(html)[:12].lower() in out.lower(), f"spec mangled: {html} -> {out}"


def test_seller_voice_and_policy_shapes_go():
    gone = [
        "<p>We ship to UK only.</p>",
        "<p>Please contact us if you have any questions about your order.</p>",
        "<p>Our aim is to give you the best shopping experience.</p>",
        "<p>Price: £19.99 including postage.</p>",
        "<p>RRP $49 - save 30% off today.</p>",
        "<p>© 2022 Volo Origin - All Rights Reserved. Powered by Frooition</p>",
        "<p>Changed your mind? Unwanted items must be returned unopened within 14 days.</p>",
        "<p>Other Hot Item Foo Gadget GBP 20.99 Bar Widget GBP 9.99</p>",
        "<p>PaymentShippingReturnsContact Us</p>",
        "<p>UK STOCK</p>",
        "<p>We have established this brand as the go-to supplier since 2009.</p>",
    ]
    for html in gone:
        out = _text(sanitize_description(html))
        assert out == "", f"seller junk survived: {html!r} -> {out!r}"


def test_cross_sell_tail_is_cut_but_early_label_is_only_dropped():
    body = "<p>Spec one. Spec two. Spec three. Spec four. Spec five. Spec six.</p>"
    tail = "<p>You may also like Thing A GBP 5.00 Thing B GBP 6.00</p>"
    out = _text(sanitize_description(body + tail))
    assert "Spec six" in out and "Thing A" not in out and "GBP" not in out
    early = _text(sanitize_description("<p>Similar items</p><p>Real spec about the product.</p>"))
    assert "Real spec about the product" in early and "Similar items" not in early


def test_empty_and_nbsp_only_list_items_collapse():
    html = ("<ul><li>&nbsp;&nbsp; &nbsp;</li><li>&nbsp;</li></ul><ul><li>Real bullet 12V</li></ul>"
            "<p>&nbsp;</p><p>______________________</p><br><br><br><br>")
    out = sanitize_description(html)
    assert "<li>Real bullet 12V</li>" in out
    assert "&nbsp;&nbsp;" not in out and "______" not in out
    assert out.count("<br>") <= 1
