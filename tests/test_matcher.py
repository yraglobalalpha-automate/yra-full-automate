"""Category matcher regression suite - every case is a real incident or a
diagnosed refusal from production (2026-08-03..06). Runs against the real
category file via the harness replica of generate_xml's nested stages."""
from _matcher_harness import map_onbuy_category

# Junk-laden description simulating the diagnosed refusals (the words that
# made "Model Train Replacement Parts" outscore real leaves for earbuds).
JUNK = ("menu items for sale check our feedback contact us model train part "
        "replacement accessory game toys railway charger cable screen protector "
        "computer operating system software technology payment delivery")


def _result(title, description=JUNK):
    return map_onbuy_category(title, "", description)


def test_tablet_title_matches_tablets_leaf():
    result, stage = _result(
        "2025 NEW 12S PRO Wifi Tablet Android 15 PC 8GB+64GB Tablets 10.1 Inch 6000mAh")
    # The 2026-08-20 phrase rule now catches tablets a stage earlier than
    # the leaf-in-title fallback this test originally pinned.
    assert stage == "title-phrase"
    assert result.endswith("> Tablets")


def test_speaker_title_matches_speakers_leaf():
    result, stage = _result(
        "Soundcore Boom 2 Plus Portable Outdoor Speaker 140W 2+2 Channel Fast Charge IPX7")
    assert stage == "leaf-in-title"
    assert result.endswith("> Speakers")


def test_earbuds_refuse_no_earbud_leaf_exists():
    _, stage = _result(
        "soundcore Liberty Buds by Anker, Half-in-Ear, Adaptive Noise Cancelling, ANC")
    assert stage == "refused"


def test_brandname_only_title_refuses():
    _, stage = _result("soundcore Boom Go 3i")
    assert stage == "refused"


def test_phone_without_the_word_phone_refuses():
    _, stage = _result("TCL Plex Dual SIM - Obsidian Black - 128GB 6GB RAM - Unlocked - Brand New")
    assert stage == "refused"


def test_monitor_without_the_word_monitor_refuses():
    _, stage = _result("iiyama GB2771QSU-B1 27 Inches")
    assert stage == "refused"


def test_displayport_adapter_never_hits_arm_supports():
    # The original 2026-07 substring-scorer bug.
    result, _ = _result("DisplayPort to HDMI Adapter Cable 4K for Home Office, supports laptop")
    assert "Arm, Hand & Finger" not in (result or "")


def test_food_cover_never_hits_books_or_toys():
    result, _ = _result("Microwave Food Cover Plate Lid Kitchen")
    assert not any(s in (result or "") for s in ("Books", "Play Toys", "Pretend"))


def test_sleep_mask_never_hits_adult_subtree():
    result, _ = _result("Silk Sleep Mask Eye Cover for Sleeping")
    assert "Sex & Adult" not in (result or "")


def test_smart_watch_lands_in_smart_watch_leaf():
    result, _ = _result("Smart Watch for Men Women Fitness Tracker")
    assert "Smart Watch" in (result or "")


def test_laptop_desk_fallback_never_claims_plain_laptops():
    result, _ = _result("Folding Laptop Desk Table Bed Adjustable Portable Stand Tray Furniture")
    assert (result or "") == "" or result.split(">")[-1].strip() != "Laptops"


def test_post_boxes_incident_wound_dressing_refuses():
    # THE 2026-08-05 live wrong match: scattered "post" (Post-Op) + "box"
    # (Box of 20) must never assemble into Garden Decor > Post Boxes.
    result, stage = _result("Opsite Post-Op Dressing 9.5cm x 8.5cm - Box of 20")
    assert "Post Boxes" not in (result or "")
    assert stage == "refused"


def test_wound_dressing_with_box_in_title_refuses():
    result, _ = _result("Mepore 6x7cm Box of 60 | Sterile Self Adhesive Wound Dressings")
    assert "Post Boxes" not in (result or "")


def test_genuine_post_box_still_matches_via_phrase():
    result, stage = _result("Wall Mounted Post Box Steel Lockable Letterbox Outdoor")
    assert stage == "leaf-in-title"
    assert result.endswith("> Post Boxes")


def test_smart_tv_with_bluetooth_goes_to_tvs_not_adapters():
    # THE 2026-08-10 incident: two Toshiba Smart TVs landed in Network
    # Bluetooth Adapters - "bluetooth" is a triple-weighted title word and
    # no TV leaf is reachable by scoring ("TVs" tokenizes to a dropped
    # 2-letter word). The curated title phrase must win first.
    result, stage = _result(
        "Toshiba Westcoast 50UF2653DB 50 Inch LED 4K Ultra HD Smart TV Bluetooth WiFi")
    assert stage == "title-phrase"
    assert result.endswith("> TVs")
    assert "Bluetooth" not in result


def test_plain_led_tv_title_also_goes_to_tvs():
    result, stage = _result("Veltech VR50UX630 50 Inch LED 4K Ultra HD Smart TV WiFi")
    assert stage == "title-phrase"
    assert result.endswith("> TVs")


def test_smart_watch_title_is_not_captured_by_tv_phrases():
    result, _ = _result("Smart Watch for Men Women Fitness Tracker")
    assert "TVs" not in (result or "")


def test_tv_accessory_titles_are_vetoed_from_the_phrase_override():
    # A remote control, a streaming box, and a projector all SAY "Smart TV"
    # - the veto list must keep the phrase stage away from them.
    for title in (
        "CT-90344 FOR TOSHIBA TV REPLACEMENT REMOTE CONTROL CT90344 SMART TV",
        "Manhattan Aero Smart TV Box 4K HDR Streaming with TiVo",
        "SUREWHEEL SW30 AutoFocus Video Projector 5G WiFi for Smart TV",
        "TV Strip Lights LED Backlight USB Music Sync Smart TV",
    ):
        result, stage = _result(title)
        assert stage != "title-phrase", title
        assert not (result or "").endswith("> TVs"), title


def test_scorer_still_runs_before_fallback():
    # A clear accessory listing must resolve in the scorer; the fallback
    # only ever runs after a refusal.
    _, stage = map_onbuy_category("iPhone 15 Phone Case Shockproof Cover", "", "")
    assert stage == "scorer"
