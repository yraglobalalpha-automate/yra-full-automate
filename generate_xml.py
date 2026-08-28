import base64
import csv
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import json
import requests
from oauth2client.service_account import ServiceAccountCredentials

import notify
import pricing
import storage
import supabase_db
from onbuy_client import OnBuyClient
from retry_utils import AuthError, PermanentError, RateLimitError, TransientError, raise_for_status, with_retry
from sanitize import sanitize_description, validate_images, strip_emojis

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("onbuy_sync")

# ================= CONFIG =================
EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")

# ================= SETTINGS =================

# TRUE = FETCH ALL PRODUCTS
# FALSE = SMART BATCHING
FULL_REFRESH = False

# CATEGORY REMAP
RUN_CATEGORY_MAPPING = True

# ================= SCALING: DYNAMIC BATCH SIZE =================
# Batch size is computed per run from the actual row count and this daily
# eBay API budget, instead of a fixed number - see main() below. Stay
# comfortably under eBay's rate limit (commonly ~5,000/day on the default
# Browse API tier - check your exact allowance in the eBay Developer Portal
# and adjust this if yours differs).
EBAY_DAILY_CALL_BUDGET = int(os.getenv("EBAY_DAILY_CALL_BUDGET") or "4000")

# How many times this workflow runs per day - keep in sync with the cron
# schedule in .github/workflows/run.yml (currently every 3 hours = 8/day).
RUNS_PER_DAY = int(os.getenv("RUNS_PER_DAY") or "8")

# Optional hard override: set this env var to force a fixed batch size
# instead of the budget-derived one.
_MAX_PRODUCTS_PER_RUN_OVERRIDE = os.getenv("MAX_PRODUCTS_PER_RUN")

# ================= PRICE CHECK FLAG THRESHOLDS =================
# Total margin % over cost (the default formula gives ~40% = 20% fee + 20%
# profit). Normal = at/near default, Medium = moderately above, High = well
# above - adjust these two numbers if "a little more"/"much more" should mean
# different percentages than this.
PRICE_CHECK_NORMAL_MAX_PCT = 45
PRICE_CHECK_MEDIUM_MAX_PCT = 70

# ================= ONBUY API PUSH (safety-gated) =================
# Off by default: this pipeline previously only ever produced feed.xml for
# OnBuy's own feed importer to consume. Turning this on makes it call OnBuy's
# write APIs directly for real SKUs. Since this hasn't been exercised against
# the live account yet, roll it out gradually:
#   1) leave ONBUY_API_PUSH_ENABLED unset/false -> behaves exactly as before
#   2) set ONBUY_API_PUSH_ENABLED=true and ONBUY_API_TEST_SKUS=sku1,sku2
#      -> only those SKUs go through the API, everything else still only
#         goes through the Sheet + feed.xml as before
#   3) once verified, clear ONBUY_API_TEST_SKUS to push every processed SKU
ONBUY_API_PUSH_ENABLED = os.getenv("ONBUY_API_PUSH_ENABLED", "false").strip().lower() == "true"
ONBUY_API_TEST_SKUS = {s.strip() for s in os.getenv("ONBUY_API_TEST_SKUS", "").split(",") if s.strip()}

# Confirmed from the real account's API usage page: OnBuy allows 240 PUT and
# 240 POST calls per hour. The eBay-derived batch size above can now be much
# larger than 12 (up to hundreds of rows/run), which didn't exist as a risk
# when this was hardcoded at 12 - cap OnBuy pushes per run well under the
# hourly limit so one large run can't burn through it on its own. Rows beyond
# this cap still get their Sheet/Supabase update this run; they just wait
# for their next turn to reach OnBuy.
ONBUY_MAX_PUSHES_PER_RUN = int(os.getenv("ONBUY_MAX_PUSHES_PER_RUN") or "200")

# Incident guards (2026-08-21, GTV content shift). ONBUY_CREATE_ENABLED=false
# pauses product CREATES only - price/stock updates keep flowing - while
# OnBuy's matcher is cross-linking consecutive API creates (listing N ends
# up on neighbour N+1's product page). protected_skus.txt (repo root, one
# SKU per line, # comments) lists listings whose OnBuy content is wrong and
# awaiting repair: their price/stock pushes are skipped in the main loop and
# the activation pass, so a zero-stocked wrong page cannot be re-armed before
# the content fix lands. The OOS pass still zeroes them (always safe).
ONBUY_CREATE_ENABLED = (os.getenv("ONBUY_CREATE_ENABLED") or "true").strip().lower() != "false"


def _load_protected_skus():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "protected_skus.txt")
    skus = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    skus.add(line)
    return skus


PROTECTED_SKUS = _load_protected_skus()

# How many eBay fetch failures (after retries) in one run before we email an alert.
FETCH_FAILURE_ALERT_THRESHOLD = 3

PK_TZ = ZoneInfo("Asia/Karachi")


class _SkipPushProtected(Exception):
    """Row's SKU is in protected_skus.txt (OnBuy shows wrong content, repair
    pending): no price/stock push, no status change (2026-08-21)."""


class _SkipCreatePaused(Exception):
    """Creates are paused (ONBUY_CREATE_ENABLED=false) while OnBuy's matcher
    cross-links consecutive creates - the row waits untouched (2026-08-21)."""


class _SkipPushNoPrice(Exception):
    """Control-flow sentinel: an already-created row with no usable selling
    price must never push - OnBuy accepts a price-0 update and then
    auto-suspends the listing (their support logs showed us doing exactly
    this on dead-source rows, 2026-08-18). The OOS pass zeroes such rows
    via the listing price fallback instead."""


class _SkipPushDead(Exception):
    """Control-flow sentinel: a row whose eBay listing is gone and that was
    never created on OnBuy has nothing to create - see the raise site."""



# One-off maintenance switch (workflow input "recategorize"): normally an
# existing VALID category is never overwritten - that protects the manual
# choices employees make. With this on, a confident eBay-Type match may
# replace a valid-but-wrong category (the smart watch filed under Car GPS
# Trackers, 2026-08-03). Only Type may overwrite; the title scorer never
# does, and rows whose Type is unclear keep exactly what they have.
RECATEGORIZE_FROM_TYPE = (os.getenv("RECATEGORIZE_FROM_TYPE") or "").strip().lower() in ("1", "yes", "true")

def should_push_to_onbuy(sku):
    if not ONBUY_API_PUSH_ENABLED:
        return False
    if ONBUY_API_TEST_SKUS:
        return sku in ONBUY_API_TEST_SKUS
    return True


# ================= HELPERS =================
def col_letter(n):
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def parse_time(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime(2000, 1, 1)




# Hand-curated title PHRASES that name the product outright and must beat
# the word scorer (2026-08-10). Two Toshiba "... Smart TV Bluetooth WiFi"
# rows landed in Network Bluetooth Adapters: "bluetooth" is a triple-
# weighted title word, while no TV leaf is reachable by scoring at all -
# "TVs" tokenizes to a 2-letter word the tokenizer drops. A contiguous
# phrase in the title is checked BEFORE the scorer; being hand-curated,
# first match wins and there is no ambiguity to arbitrate. Keep entries
# stemmed (category_match_tokens conventions) and add sparingly - one
# phrase covers a whole device family, unlike per-SKU curation.
TITLE_PHRASE_CATEGORIES = (
    (("smart", "tv"), "Electronics & Technology > TV & Audio > TVs & Accessories > TVs"),
    (("led", "tv"), "Electronics & Technology > TV & Audio > TVs & Accessories > TVs"),
    (("oled", "tv"), "Electronics & Technology > TV & Audio > TVs & Accessories > TVs"),
    (("qled", "tv"), "Electronics & Technology > TV & Audio > TVs & Accessories > TVs"),
    # 2026-08-20: hundreds of manual categorisations on a sibling store were
    # phones/laptops/taps the scorer refused - one phrase each covers them.
    (("smartphone",), "Electronics & Technology > Mobile & Smart Tech > Mobile Phones & Accessories > Mobile Phones"),
    (("smart", "phone"), "Electronics & Technology > Mobile & Smart Tech > Mobile Phones & Accessories > Mobile Phones"),
    (("mobile", "phone"), "Electronics & Technology > Mobile & Smart Tech > Mobile Phones & Accessories > Mobile Phones"),
    (("laptop",), "Electronics & Technology > Computing & Gaming > Laptops, MacBooks & Accessories > Laptops"),
    (("tablet",), "Electronics & Technology > Computing & Gaming > iPads, Tablets & eBook Readers > Tablets"),
    (("kitchen", "tap"), "Tools & DIY > Kitchen & Bathroom Fixtures > Kitchen Fixtures > Kitchen Taps"),
    (("kitchen", "mixer", "tap"), "Tools & DIY > Kitchen & Bathroom Fixtures > Kitchen Fixtures > Kitchen Taps"),
    (("mixer", "tap"), "Tools & DIY > Kitchen & Bathroom Fixtures > Kitchen Fixtures > Kitchen Taps"),
    (("basin", "tap"), "Tools & DIY > Kitchen & Bathroom Fixtures > Bathroom Fixtures > Bathroom Sink Taps"),
    (("bath", "tap"), "Tools & DIY > Kitchen & Bathroom Fixtures > Bathroom Fixtures > Bath & Shower Taps"),
    (("shower", "tap"), "Tools & DIY > Kitchen & Bathroom Fixtures > Bathroom Fixtures > Bath & Shower Taps"),
    (("garden", "tap"), "Home & Garden > Garden & Outdoor Living > Garden Watering & Irrigation Supplies > Garden Taps"),
)


# Titles that CONTAIN a device phrase but are actually accessories or a
# different device ("SMART TV REMOTE CONTROL", "Smart TV Box", a projector
# "for Smart TV") - any of these words in the title vetoes the phrase
# override and lets the normal stages decide (2026-08-10, found by the
# category correctness scan on its first pass).
TITLE_PHRASE_VETO = {
    "remote", "control", "bracket", "mount", "stand", "strip", "light",
    "backlight", "sticker", "cover", "case", "protector", "cable", "box",
    "projector", "soundbar", "aerial", "antenna",
    # phones/laptops/tablets/taps accessory words (2026-08-20)
    "bag", "sleeve", "charger", "adapter", "holder", "battery", "keyboard",
    "stylus", "pen", "washer", "cartridge", "aerator", "hose", "connector",
}


def title_phrase_category(title):
    """The category a hand-curated title phrase dictates, or None."""
    seq = [_stem(w) for w in re.findall(r"\w+", str(title).lower())]
    if TITLE_PHRASE_VETO & set(seq):
        return None
    for phrase, path in TITLE_PHRASE_CATEGORIES:
        n = len(phrase)
        if n <= len(seq) and any(tuple(seq[i:i + n]) == phrase for i in range(len(seq) - n + 1)):
            return path
    return None


def carry_forward(fresh, stored, default=None):
    """First non-None value wins - this run's value, else what Supabase
    already had, else `default`.

    `or` cannot be used for the OnBuy-tracking columns. Under the canonical
    typed schema (2026-08-06: boolean / boolean / nullable timestamp, as on
    the GTV store) PostgREST returns a stored boolean False as Python False
    - falsy but perfectly valid - so `or` falls straight past it to the
    default and sends "" into a boolean column, which 400s the ENTIRE
    batch (22P02) and loses the database mirror for every row in it
    (GTV, ~25% of runs, fixed 2026-08-04). Safe against the pre-migration
    text schema too: "FALSE" is a normal string there and NULL is accepted
    (canary-verified per store before this port).
    """
    if fresh is not None:
        return fresh
    if stored is not None:
        return stored
    return default


def is_valid_gtin(code):
    """True if `code` is a real barcode by the GS1 check-digit standard used
    for UPC-A/EAN-8/EAN-13/GTIN-14 (all the same algorithm, just different
    lengths). Being all-digits and the right length isn't enough - OnBuy
    validates the actual check digit and rejects create_product outright
    with "not a valid product code" otherwise, which happened for two SKUs
    that were numeric and 12 digits long but not real barcodes."""
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    # GS1 reserves the "5" number system of 12-digit UPCs for discount
    # coupons - such codes pass the check digit but can never be product
    # codes, and OnBuy's GS1 registry check rejects them ("not a valid
    # product code": 23 YRA + 74 Arden feed rows, 2026-07-28). A 13-digit
    # code starting "05" is the same coupon number zero-padded; 13-digit
    # codes starting "50" (GS1 UK) remain perfectly valid.
    if (len(code) == 12 and code[0] == "5") or (len(code) == 13 and code.startswith("05")):
        return False
    body, check_digit = code[:-1], code[-1]
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str((10 - total % 10) % 10) == check_digit


def sku_numeric_part(sku):
    """The digits of the SKU ARE the product's barcode (user policy
    2026-07-13, both stores): a SKU may carry non-digit decoration around
    the barcode ("GTV-5012345678900") and everything validated or sent as
    an EAN/UPC uses only the digits. The decoration must not itself contain
    digits - a "-1" style suffix corrupts the barcode and will (correctly)
    fail the check-digit test."""
    return re.sub(r"\D", "", str(sku or ""))


# Values eBay sellers sometimes put in the "Brand" aspect that are not
# actually a brand name - typically someone answering a yes/no-style prompt
# literally ("Branded") rather than naming the brand. User's explicit policy
# (2026-07-04): normalize all of these to "Unbranded" rather than pass a
# placeholder through as if it were a real brand.
_NON_BRAND_VALUES = {"branded", "unbranded", "no brand", "none", "n/a", "na", "generic", ""}


def normalize_brand(brand):
    if str(brand).strip().lower() in _NON_BRAND_VALUES:
        return "Unbranded"
    # OnBuy 400s "brand name must be greater than or equal to 2 characters
    # after processing" (a lone "." from eBay, 2026-08-21): a value with
    # fewer than 2 letters/digits is not a brand.
    if len(re.sub(r"[^A-Za-z0-9]", "", str(brand))) < 2:
        return "Unbranded"
    return brand


def dedupe_rows_by_sku(rows, what):
    """Postgres/PostgREST rejects a whole bulk upsert if two rows in the same
    call share the same SKU (the conflict target) - "ON CONFLICT DO UPDATE
    command cannot affect row a second time". That only happens from a real
    duplicate SKU somewhere in the Sheet (e.g. a copy-pasted row, or the same
    value with stray whitespace), so keep the last occurrence and log which
    SKU(s) need fixing in the Sheet, rather than losing the whole batch."""
    deduped = {}
    duplicates = set()
    for row in rows:
        sku = row.get("SKU")
        if sku in deduped:
            duplicates.add(sku)
        deduped[sku] = row
    if duplicates:
        logger.warning(
            "%s: %d row(s) dropped due to duplicate SKU(s) in the Sheet - please fix these SKUs: %s",
            what, len(duplicates), ", ".join(sorted(duplicates)),
        )
    return list(deduped.values())


_RED = {"red": 0.96, "green": 0.8, "blue": 0.8}
_WHITE = {"red": 1, "green": 1, "blue": 1}


def row_highlight_request(sheet_id, row_index, num_cols, active):
    """Sheets API repeatCell request: red background for an inactive
    (stock=0) row, cleared back to white when it's active again."""
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": row_index - 1,
                "endRowIndex": row_index,
                "startColumnIndex": 0,
                "endColumnIndex": num_cols,
            },
            "cell": {"userEnteredFormat": {"backgroundColor": _WHITE if active else _RED}},
            "fields": "userEnteredFormat.backgroundColor",
        }
    }


def tokenize(text):
    return set(re.findall(r"\w+", str(text).lower()))


# Words too generic to say anything about a product's category - they appear
# in almost every eBay title/description ("premium quality", "free shipping",
# "brand new", "UK stock"...) and were a major source of wrong matches.
_CATEGORY_STOPWORDS = {
    "and", "the", "for", "with", "from", "this", "that", "your", "our", "you",
    "are", "not", "new", "brand", "pack", "pcs", "set", "free", "shipping",
    "delivery", "returns", "quality", "premium", "high", "best", "top", "hot",
    "sale", "gift", "uni", "unisex", "universal", "portable", "durable",
    "stock", "fast", "included", "includes", "colour", "color", "size", "uk",
    "usa", "use", "product", "products", "item", "items", "piece",
    "pieces", "note", "please", "buy", "seller", "customer", "support",
    "service", "day", "days", "one", "two", "three", "all", "small", "large",
    "mini", "big", "medium", "extra", "travel",
}

# Category subtrees that share everyday vocabulary with ordinary physical
# products and kept stealing them in testing (on the Arden store, where this
# matcher was rewritten first): a microwave FOOD COVER matched "Cooking
# Books" and "Kitchen Role Play Toys", a SLEEP MASK matched "BDSM Masks &
# Blindfolds". Each subtree is only allowed when the product explicitly uses
# one of its own words (stemmed forms, matching category_match_tokens'
# output - hence "dres" for dress).
_GUARDED_SUBTREES = (
    ("books, movies & music",
     {"book", "dvd", "blu", "vinyl", "movie", "film", "music", "album", "magazine", "novel"}),
    ("health & beauty > sex & adult",
     {"adult", "bdsm", "erotic", "bondage", "sex"}),
    ("toys & games > pretend play & fancy dress",
     {"toy", "pretend", "costume", "fancy", "dres", "kid", "child", "children"}),
)


def _stem(word):
    # Bridge singular/plural ("adapter" <-> "Adapters", "watch" <-> "Watches")
    # without a real stemmer. The -es forms matter: the naive "drop one s"
    # rule turned "Watches" into "watche", so a Type of "Smart Watch" could
    # never match the "Smart Watches" leaf and landed in "Smart Watch Cases"
    # instead (2026-08-03). Order matters - most specific ending first.
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"            # batteries -> battery
    if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]                  # watches -> watch, boxes -> box
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]                  # adapters -> adapter
    return word


def category_match_tokens(text):
    """Meaningful whole-word tokens for category matching: 3+ characters,
    not a stopword, not a bare number, plural-normalized."""
    return {_stem(w) for w in tokenize(text)
            if len(w) >= 3 and w not in _CATEGORY_STOPWORDS and not w.isdigit()}


def clean_category(cat):
    if not cat:
        return ""
    cat = str(cat).replace("\n", " ").strip()
    cat = re.sub(r"\s+", " ", cat).strip()
    return cat


def to_jpg(url):
    if not url:
        return ""
    url = re.sub(r"\.webp.*$", ".jpg", url)
    url = re.sub(r"\.(png|jpeg).*?$", ".jpg", url)
    return url


def empty_ebay_response():
    return {
        "stock": 0,
        "price": 0,
        "description": "",
        "main_image": "",
        "additional_images": [],
        "title": "",
        "brand": "",
        "product_code": "",
        "condition": "",
    }


_BARCODE_ASPECT_NAMES = ("EAN", "GTIN", "UPC", "ISBN")


def extract_product_code(data):
    """Look for a real barcode (EAN/GTIN/UPC/ISBN) in eBay's item aspects -
    same array already parsed for Brand, no extra API call. Returns "" when
    the listing has no barcode specified. This is purely informational (the
    Sheet/Supabase "EAN" column) - it is NOT what gets sent to OnBuy as the
    product code. OnBuy uses the seller's own SKU for that instead (see the
    main loop), since the eBay item ID looked plausible as a fallback here
    but isn't a real barcode and got create_product rejected outright with
    "not a valid product code" when tried.
    """
    for aspect in data.get("localizedAspects", []):
        name = aspect.get("name", "").strip().upper()
        if name in _BARCODE_ASPECT_NAMES:
            values = aspect.get("value", "")
            raw = values[0] if isinstance(values, list) else values
            digits = re.sub(r"\D", "", str(raw))
            if digits:
                return digits
    return ""


# ================= EBAY TOKEN =================
def get_ebay_token():
    def _do_token():
        encoded = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
        resp = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {encoded}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
            timeout=30,
        )
        raise_for_status(resp, what="ebay token")
        token = resp.json().get("access_token")
        if not token:
            raise AuthError("ebay token response missing access_token")
        return token

    try:
        return with_retry(_do_token, what="ebay token", max_attempts=3)
    except (AuthError, PermanentError) as exc:
        logger.error("eBay authentication failed: %s", exc)
        return None
    except Exception as exc:
        logger.error("eBay authentication failed after retries: %s", exc)
        return None


ITEM_GROUP_ERROR_ID = 11006  # "The legacy Id is invalid... use get_items_by_item_group"


def _is_item_group_error(resp):
    try:
        errors = resp.json().get("errors", [])
    except ValueError:
        return False
    return any(e.get("errorId") == ITEM_GROUP_ERROR_ID for e in errors)


def _fetch_item_group_as_item(item_group_id, token):
    """Some eBay listings are multi-variation ("item group") listings - e.g.
    a listing with size/color options - which get_item_by_legacy_id rejects
    with errorId 11006, pointing at this endpoint instead.

    item_group_id here is the specific legacy item ID the Sheet row's URL
    actually linked to (that's what triggered the 11006 error in the first
    place), so match it back against the group's returned items and use that
    *exact* variation's own title/description/images/price. Only falls back
    to the first item in the group if no exact match is found - previously
    this always used the first item regardless of which variation was
    linked, which is the likely cause of unrelated rows appearing to share
    one variation's description (they'd all resolve to whichever variation
    the API happened to list first for that group).
    """
    resp = requests.get(
        "https://api.ebay.com/buy/browse/v1/item/get_items_by_item_group",
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"},
        params={"item_group_id": item_group_id},
        timeout=20,
    )
    raise_for_status(resp, what=f"ebay item group {item_group_id}")
    group_data = resp.json()

    items = group_data.get("items", [])
    if not items:
        return None

    chosen = next(
        (item for item in items if str(item.get("legacyItemId") or "") == str(item_group_id)),
        None,
    )
    if chosen is not None:
        logger.info("Item %s is a multi-variation listing - using its own linked variation", item_group_id)
    else:
        chosen = items[0]
        logger.warning(
            "Item %s is a multi-variation listing but its own variation wasn't found among "
            "the %d returned - falling back to variation %s, title/description/price may not "
            "match what this row's link actually points to",
            item_group_id, len(items), chosen.get("legacyItemId") or chosen.get("itemId"),
        )

    description = chosen.get("description")
    if not description:
        for common in group_data.get("commonDescriptions", []):
            if chosen.get("itemId") in common.get("itemIds", []):
                description = common.get("description", "")
                break
    chosen["description"] = description or ""

    return chosen


# ================= EBAY FETCH =================
def get_ebay_data(url, token):
    """Returns (available, data). available=False with empty_ebay_response()
    means eBay gave us a definitive "not available" answer (404 / no price /
    out of stock) - a real signal, not a failure.

    Raises TransientError/PermanentError if the fetch itself failed after
    retries. Callers MUST NOT treat that the same as "removed" - the previous
    version's bare `except Exception` did exactly that and zeroed live
    listings on ordinary network blips.
    """
    match = re.search(r"/itm/(\d+)", url)
    if not match:
        return False, empty_ebay_response()
    item_id = match.group(1)

    def _do_fetch():
        resp = requests.get(
            "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id",
            headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"},
            params={"legacy_item_id": item_id},
            timeout=20,
        )
        if resp.status_code == 404:
            return None  # confirmed removed - a real signal, not an error
        if resp.status_code == 400 and _is_item_group_error(resp):
            return _fetch_item_group_as_item(item_id, token)
        raise_for_status(resp, what=f"ebay item {item_id}")
        return resp.json()

    data = with_retry(_do_fetch, what=f"ebay item {item_id}", max_attempts=3)

    if data is None:
        logger.info("REMOVED LISTING: %s", item_id)
        return False, empty_ebay_response()

    price_data = data.get("price", {}) or {}
    price = float(price_data.get("value", 0) or 0)
    if price <= 0:
        logger.info("NO PRICE: %s", item_id)
        return False, empty_ebay_response()

    estimated = data.get("estimatedAvailabilities", [])
    stock = 5
    if estimated:
        est = estimated[0]
        status = est.get("estimatedAvailabilityStatus", "")
        if status in ("OUT_OF_STOCK", "UNAVAILABLE"):
            logger.info("OUT OF STOCK: %s", item_id)
            return False, empty_ebay_response()
        stock = est.get("estimatedAvailableQuantity")
        if not stock:
            # eBay hides the exact count above a threshold - the listing
            # shows "More than 10 available" and the API sends only
            # availabilityThresholdType=MORE_THAN + the threshold, no
            # quantity. Use that known floor ("at least 10") rather than
            # a made-up number; never inflate past what eBay confirms -
            # overselling is the costlier mistake.
            if str(est.get("availabilityThresholdType") or "") == "MORE_THAN":
                stock = est.get("estimatedAvailabilityThreshold") or 0
    if not stock or stock <= 0:
        stock = 5

    html_description = sanitize_description(data.get("description", ""))

    main_image = ""
    if data.get("image"):
        main_image = to_jpg(data["image"].get("imageUrl", ""))

    additional_images = []
    for img in data.get("additionalImages", []):
        img_url = to_jpg(img.get("imageUrl", ""))
        if img_url:
            additional_images.append(img_url)

    all_images = validate_images([main_image] + additional_images, max_images=11)
    main_image = all_images[0] if all_images else ""
    additional_images = all_images[1:11]

    title = strip_emojis(data.get("title", ""))

    brand = ""
    for aspect in data.get("localizedAspects", []):
        if aspect.get("name", "").lower() == "brand":
            values = aspect.get("value", "")
            brand = values[0] if isinstance(values, list) else values
    brand = normalize_brand(brand)

    product_code = extract_product_code(data)
    condition = data.get("condition") or "New"

    # eBay's structured "Type" item specific names the product class
    # outright ("Air Fryer", "Foot File") even when a marketing title
    # doesn't (user insight 2026-08-01: novelty-gift titles hide the
    # product, but Type identifies it). Fallback: sellers often render
    # the specifics into the description text as "Type: ...".
    product_type = ""
    for aspect in data.get("localizedAspects", []):
        if aspect.get("name", "").strip().lower() in ("type", "product type", "item type"):
            values = aspect.get("value", "")
            product_type = str(values[0] if isinstance(values, list) else values).strip()
            if product_type:
                break
    if not product_type:
        m = re.search(r"(?i)\btype\s*:\s*(?:</?[a-z][^>]*>\s*)*([A-Za-z][A-Za-z &/-]{2,40})",
                      str(data.get("description") or ""))
        if m:
            product_type = m.group(1).strip()

    return True, {
        "stock": stock,
        "price": price,
        "description": html_description,
        "main_image": main_image,
        "additional_images": additional_images,
        "title": title,
        "brand": brand,
        "product_code": product_code,
        "condition": condition,
        "product_type": product_type,
    }


def main():
    run_had_errors = False
    fetch_failures = 0

    # ================= GOOGLE SHEET =================
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    # Google's API throws intermittent 503s (5 crashed runs in the week of
    # 2026-08-18) - one retry cycle rides them out.
    sheet = with_retry(lambda: client.open("YRA_Full_Feed_Master").sheet1,
                       what="sheet open", max_attempts=3)

    # Header hygiene BEFORE reading the data: one stray space typed into a
    # header cell ("SKU ") makes that whole column unreadable - row.get("SKU")
    # returns None on every row - and a blanked A1 did exactly that on the
    # YRA Full sheet on 2026-08-06. Strip every header name, and refuse to
    # run at all on missing/duplicated critical headers: one clear email
    # beats a run that half-works and flags every row. (Ported from the
    # semi tier, where a deleted header row taught this on 2026-07-29.)
    headers = [str(h).strip() for h in sheet.row_values(1)]
    col_map = {col: idx + 1 for idx, col in enumerate(headers) if col}

    required = ["SKU", "Supplier URL", "Title", "Status", "Last Checked Time"]
    missing = [h for h in required if h not in col_map]
    duplicates = sorted({h for h in headers if h and headers.count(h) > 1})
    if missing or duplicates:
        problems = []
        if missing:
            problems.append("missing header(s): " + ", ".join(missing))
        if duplicates:
            problems.append("duplicated header(s): " + ", ".join(duplicates))
        message = ("The Sheet's header row (row 1) is broken - " + "; ".join(problems)
                   + f". Row 1 currently reads: {[h for h in headers if h]}. "
                   "Fix row 1 to match sheet_headers.csv (one name per cell, spelled exactly) "
                   "and run the sync again. No rows were touched this run.")
        logger.error(message)
        notify.send_alert_email("Sheet header row needs fixing", message)
        sys.exit(1)

    data = with_retry(lambda: sheet.get_all_records(),
                      what="sheet read", max_attempts=3)
    # Same hygiene on the row dicts (their keys come from the header row).
    data = [{str(k).strip(): v for k, v in row.items()} for row in data]

    # SKUs are taken from the column's DISPLAYED text, not get_all_records:
    # numericise turns a pure-digit SKU stored with a leading zero into an
    # int with the zero stripped, so every by-SKU push targets a SKU OnBuy
    # doesn't know and defers "Awaiting OnBuy go-live" forever (GTV's 127
    # census pairs, repaired 2026-08-27, all carried leading-zero SKUs).
    # Formatting characters are stripped; leading zeros are kept.
    sku_display = with_retry(lambda: sheet.col_values(col_map["SKU"]),
                             what="sku display column", max_attempts=3)
    for _i, _row in enumerate(data):
        if _i + 1 < len(sku_display):
            _row["SKU"] = re.sub(r"[,\s]", "", str(sku_display[_i + 1]))

    # Manual targeted runs: the dispatch form's `rows` input (env ROWS_RANGE,
    # e.g. "2200-2320", "116", "10-50,200-210") limits THIS run - batch
    # selection, OOS pass and activation pass - to those sheet row numbers
    # (as seen in Google Sheets, header = row 1). Scheduled runs never set
    # it. A typed range also overrides the manual-run "unfilled rows only"
    # default: naming rows means refresh exactly these, filled or not.
    rows_range_spec = (os.getenv("ROWS_RANGE") or "").strip()
    rows_ranges = []
    if rows_range_spec:
        for _part in rows_range_spec.split(","):
            _part = _part.strip()
            if not _part:
                continue
            _a, _, _b = _part.partition("-")
            try:
                _lo, _hi = int(_a), int(_b or _a)
            except ValueError:
                logger.error("rows input %r is not a row number or a start-end range", _part)
                sys.exit(1)
            rows_ranges.append((min(_lo, _hi), max(_lo, _hi)))
        logger.info("Targeted run: limited to sheet row(s) %s", rows_range_spec)

    def row_in_ranges(rownum):
        return (not rows_ranges) or any(lo <= rownum <= hi for lo, hi in rows_ranges)

    logger.info("TOTAL ROWS IN SHEET: %d", len(data))

    # ================= DYNAMIC BATCH SIZE =================
    # Sized from the actual row count and the eBay daily call budget, so the
    # same code scales from a 150-row catalog to a 5,000-row one without
    # needing a manual reconfiguration each time it grows - see the comment
    # on EBAY_DAILY_CALL_BUDGET/RUNS_PER_DAY above.
    if _MAX_PRODUCTS_PER_RUN_OVERRIDE:
        MAX_PRODUCTS_PER_RUN = max(1, int(_MAX_PRODUCTS_PER_RUN_OVERRIDE))
    else:
        MAX_PRODUCTS_PER_RUN = max(1, EBAY_DAILY_CALL_BUDGET // RUNS_PER_DAY)

    cycle_runs = -(-len(data) // MAX_PRODUCTS_PER_RUN) if data else 0  # ceil division
    cycle_days = cycle_runs / RUNS_PER_DAY if RUNS_PER_DAY else 0
    logger.info(
        "Batch size: %d products/run (budget %d eBay calls/day over %d runs/day) "
        "- a full refresh cycle over %d rows takes ~%.1f day(s)",
        MAX_PRODUCTS_PER_RUN, EBAY_DAILY_CALL_BUDGET, RUNS_PER_DAY, len(data), cycle_days,
    )

    # ================= CATEGORY FILE =================
    onbuy_categories = []
    category_id_by_path = {}

    with open("onbuy_categories_only.csv", newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            category = row.get("OnBuy Category Path")
            if category:
                onbuy_categories.append(category)
                try:
                    category_id_by_path[category.strip().lower()] = int(row.get("Category ID"))
                except (TypeError, ValueError):
                    category_id_by_path[category.strip().lower()] = None

    logger.info("Loaded %d OnBuy categories", len(onbuy_categories))

    valid_onbuy_categories = set(cat.strip().lower() for cat in onbuy_categories)

    def is_valid_onbuy_category(category):
        return str(category).strip().lower() in valid_onbuy_categories

    # A NON-EMPTY Category cell is a human decision (2026-08-22: 44 Arden rows
    # the user had categorised by hand were re-matched after OnBuy renamed
    # 107 paths and our category file was refreshed). Never replace typed
    # text with a guess: resolve it - exact path; the row's Category ID
    # (renames keep the id); a unique leaf name - or keep it and flag it.
    _display_path = {cat.strip().lower(): cat for cat in onbuy_categories}
    _path_by_id = {}
    for _p, _id in category_id_by_path.items():
        if _id is not None and _id not in _path_by_id:
            _path_by_id[_id] = _p
    _leaf_index = {}
    for _cat in onbuy_categories:
        _leaf_index.setdefault(_cat.rsplit(" > ", 1)[-1].strip().lower(), []).append(_cat)

    def resolve_manual_category(current_category, current_id=None):
        """(path, resolved). resolved=False keeps the typed text untouched."""
        text = str(current_category or "").strip()
        if not text:
            return "", False
        if is_valid_onbuy_category(text):
            return _display_path[text.lower()], True
        try:
            cid = int(float(str(current_id).strip())) if str(current_id or "").strip() else None
        except (TypeError, ValueError):
            cid = None
        if cid is not None and cid in _path_by_id:
            return _display_path[_path_by_id[cid]], True
        leaf = text.rsplit(" > ", 1)[-1].strip().lower()
        if len(_leaf_index.get(leaf, [])) == 1:
            return _leaf_index[leaf][0], True
        return text, False

    # Precomputed token sets per category path (and per leaf segment) - both
    # for speed and correctness. The old scorer gave +2 whenever a product
    # word appeared as a SUBSTRING anywhere in the path text, so a
    # DisplayPort adapter (description mentioning "home", "supports" - and
    # "port" is literally a substring of "Supports") landed in "Braces,
    # Splints & Slings > Arm, Hand & Finger Supports" (found on the Arden
    # store; same matcher here). This version matches whole words only,
    # weights title words over description words (titles identify the
    # product; descriptions are marketing text), weights the leaf segment
    # over ancestors, and refuses to guess without at least one strong
    # title-level match. Rows already holding a valid category path are
    # never touched, so existing catalog categories are unaffected.
    category_tokens = {}
    category_leaf_tokens = {}
    category_leaf_phrase = {}  # path -> ordered stemmed words of the leaf name
    category_guard = {}  # path -> required word set (None = unguarded)
    for _path in onbuy_categories:
        category_tokens[_path] = category_match_tokens(_path)
        category_leaf_tokens[_path] = category_match_tokens(_path.split(">")[-1])
        category_leaf_phrase[_path] = tuple(
            _stem(w) for w in re.findall(r"\w+", _path.split(">")[-1].lower()))
        _low = _path.strip().lower()
        category_guard[_path] = next(
            (req for prefix, req in _GUARDED_SUBTREES if _low.startswith(prefix)), None)

    def type_category(product_type, title="", description=""):
        """The category eBay's Type field alone justifies, or None.
        Extracted so maintenance runs can ask it directly."""
        # eBay's structured Type is AUTHORITATIVE and runs FIRST (user
        # 2026-08-03: a smart watch landed in Car GPS Trackers because its
        # title carries "GPS" and "tracker" as feature words - scoring a
        # marketing title can never beat a field that names the product).
        # Still strict: leaf names only, unique best required; anything
        # ambiguous falls through to the title scorer exactly as before.
        # Leaf-name-only matching, subset in either direction (tokens are
        # plural-normalized), and only a UNIQUE best is accepted: precision
        # over recall, the DisplayPort rule. Ties or no candidates fall
        # through to the human worklist exactly as before.
        type_tokens = category_match_tokens(product_type)
        if type_tokens:
            # Words a leaf adds beyond the Type must be corroborated by the
            # product's own words: a Type of "GPS Tracker" must not become
            # "Car GPS Trackers" unless the listing actually says "car"
            # (2026-08-03). "Phone Case" -> "Mobile Phone Cases" still
            # works because such listings do say "mobile".
            corroborating = category_match_tokens(f"{title}\n{description}")
            candidates = []
            for category_path in onbuy_categories:
                leaf = category_leaf_tokens[category_path]
                if not leaf:
                    continue
                # A 1-word Type may only take a leaf whose visible name it
                # covers completely - "Light" must NOT take "DJ Lights"
                # (the 2-letter DJ vanishes in tokenization). Multi-word
                # Types may also sub-match inside a bigger leaf.
                leaf_word_count = len(tokenize(category_path.split(">")[-1]))
                covers_whole_leaf = leaf <= type_tokens and leaf_word_count == len(leaf)
                strong_submatch = len(type_tokens) >= 2 and (leaf <= type_tokens or type_tokens <= leaf)
                if covers_whole_leaf or strong_submatch:
                    extras = leaf - type_tokens
                    if extras and not extras <= corroborating:
                        continue
                    overlap = len(leaf & type_tokens)
                    if overlap:
                        candidates.append((overlap, -len(leaf - type_tokens),
                                           -len(category_path), category_path))
            if candidates:
                candidates.sort(reverse=True)
                if len(candidates) == 1 or candidates[0][:2] > candidates[1][:2]:
                    logger.info("Category matched via eBay Type %r -> %s",
                                product_type, candidates[0][3])
                    return candidates[0][3]
        return None

    def map_onbuy_category(title, current_category, description="", product_type=""):
        by_type = type_category(product_type, title, description)
        if by_type:
            return by_type
        by_phrase = title_phrase_category(title)
        if by_phrase:
            logger.info("Category matched via curated title phrase -> %s", by_phrase)
            return by_phrase
        title_words = category_match_tokens(f"{title}\n{current_category}")
        desc_words = category_match_tokens(description) - title_words
        all_words = title_words | desc_words
        if not all_words:
            return current_category

        def weight(word):
            # Longer words are more specific; title words count triple.
            return len(word) * (3 if word in title_words else 1)

        best_match = None
        best_score = 0
        best_has_title_hit = False
        for category_path in onbuy_categories:
            required = category_guard[category_path]
            if required and not (all_words & required):
                continue
            hits = all_words & category_tokens[category_path]
            if not hits:
                continue
            leaf_hits = hits & category_leaf_tokens[category_path]
            score = sum(weight(w) for w in hits) + 2 * sum(weight(w) for w in leaf_hits)
            if score > best_score:
                best_score = score
                best_match = category_path
                best_has_title_hit = bool(hits & title_words)

        # Refuse to guess unless at least one TITLE word matched (titles
        # identify the product; a description-only match is marketing noise).
        # An unmatched row keeps its current/blank category - the push block
        # below turns that into a clear "fill in the Category column" status
        # instead of submitting a wrong or null category to OnBuy.
        if best_match and best_score >= 9 and best_has_title_hit:
            return best_match

        # ---- Leaf-named-in-title fallback (2026-08-05, ported from the
        # GTV store with its phrase-containment tightening) ----
        # Only reached when the scorer above REFUSED, so behaviour changes
        # solely for rows that were failing anyway. Diagnosed there:
        # listings with no eBay Type whose descriptions drowned the scorer
        # in generic boilerplate - "Model Train Replacement Parts" (hits:
        # part, replacement, model, accessory) outscored every real audio
        # leaf for a pair of earbuds, and that junk winner had no title
        # hit, so the refusal fired even though the title said "Tablet"/
        # "Speaker" outright.
        #
        # The leaf's visible name must appear as a CONTIGUOUS PHRASE in the
        # title (stemmed, in order), not merely as scattered word tokens.
        # The first token-subset version matched an "Opsite Post-Op
        # Dressing ... Box of 20" (a medical wound dressing) to Garden
        # Decor > Post Boxes on its first live firing - "post" from
        # Post-Op plus "box" from Box of 20, words in unrelated roles. A
        # phrase can't be assembled from scattered words, so that class of
        # error is structurally impossible here. Longest phrase wins
        # ("Tablet Cases" beats "Tablets" for a case listing); a tie at the
        # top refuses; guarded subtrees keep their guard.
        title_seq = [_stem(w) for w in re.findall(r"\w+", str(title).lower())]
        covered = []
        for category_path in onbuy_categories:
            required = category_guard[category_path]
            if required and not (all_words & required):
                continue
            phrase = category_leaf_phrase[category_path]
            n = len(phrase)
            if not n or n > len(title_seq):
                continue
            if any(tuple(title_seq[i:i + n]) == phrase
                   for i in range(len(title_seq) - n + 1)):
                covered.append((n, -len(category_path), category_path))
        if covered:
            covered.sort(reverse=True)
            if len(covered) == 1 or covered[0][0] > covered[1][0]:
                logger.info("Category matched via leaf-in-title -> %s", covered[0][2])
                return covered[0][2]

        return current_category

    # ================= ONBUY CLIENT =================
    onbuy = OnBuyClient()
    onbuy_ready = False
    if ONBUY_API_PUSH_ENABLED:
        onbuy_ready = onbuy.authenticate()
        if not onbuy_ready:
            run_had_errors = True
            logger.error("ONBUY_API_PUSH_ENABLED is true but OnBuy authentication failed - skipping all OnBuy API pushes this run")

    # ================= CATEGORY MAPPING =================
    # Cheap full-catalog pass (no eBay calls) for rows that already have a
    # Title/Description from a previous run. A brand-new row (employee only
    # pasted the URL) still has blank Title/Description at this point, so it
    # can't be mapped yet here - the main loop below re-checks category using
    # the freshly-fetched eBay data for exactly that case.
    if RUN_CATEGORY_MAPPING:
        logger.info("Updating categories...")
        category_updates = []
        for idx, row in enumerate(data):
            i = idx + 2
            current_category = str(row.get("Category") or "").strip()
            if is_valid_onbuy_category(current_category):
                continue
            if current_category:
                resolved, ok = resolve_manual_category(current_category, row.get("Category ID"))
                if ok and resolved != current_category:
                    category_updates.append({"range": f"{col_letter(col_map['Category'])}{i}", "values": [[resolved]]})
                    logger.info("Row %d: category text refreshed to OnBuy's current name: %r -> %r", i, current_category, resolved)
                elif not ok:
                    logger.info("Row %d: manual category %r not recognised - kept as typed, not remapped", i, current_category)
                continue
            mapped = map_onbuy_category(row.get("Title"), current_category, row.get("Description"))
            if mapped != current_category:
                category_updates.append({"range": f"{col_letter(col_map['Category'])}{i}", "values": [[mapped]]})
                logger.info("Mapped row %d", i)
        if category_updates:
            sheet.batch_update(category_updates)

    updated_count = 0
    onbuy_created = 0
    onbuy_updated = 0
    onbuy_failed = 0
    onbuy_removed = 0
    onbuy_brand_blocked = 0  # brand owned by another seller - flagged, kept (2026-08-03)
    onbuy_deferred = 0  # created earlier, listing not yet updatable on OnBuy's side
    onbuy_suspended_locked = 0  # listing suspended on OnBuy - edits rejected until reactivation
    onbuy_no_price = 0  # already-created rows with no usable price - push skipped (never send price 0)
    onbuy_protected = 0  # protected_skus.txt rows - push skipped until content repair lands (2026-08-21)
    onbuy_create_paused = 0  # creates held back by ONBUY_CREATE_ENABLED=false (2026-08-21)
    onbuy_postponed = 0  # transient OnBuy/transport trouble - status left untouched, retried next run
    onbuy_skipped_dead = 0  # eBay listing gone + never created on OnBuy - nothing to create (2026-08-06)
    onbuy_needs_category = 0  # refusals awaiting a Category cell - a worklist, not failures (2026-08-06)
    onbuy_halt_reason = None  # set when pushing must stop for the rest of the run (rate limit / dead token)
    onbuy_pushes_this_run = 0
    rows_to_delete = []  # Sheet row numbers to remove entirely - see the
    # "supplied brand is owned by another seller" check below. Applied after
    # every other Sheet write this run, in descending row order, so deleting
    # one doesn't shift the row numbers the other writes/highlights already
    # targeted.

    # ================= OUT-OF-STOCK PASS (2026-08-15) =================
    # A row that goes out of stock gets its sheet refresh immediately, but
    # its OnBuy push competed for the same capped slots as everything else,
    # and a capped-out row only retried on its NEXT rotation visit (~1.5
    # days on a large catalog) - so OnBuy kept selling sold-out products
    # (user-reported oversell risk, 4 known SKUs on this store's sibling).
    # Out of stock is the one state that must never wait: before ANY other
    # push, zero the OnBuy stock of every row whose sheet says 0 but whose
    # last push predates its last data refresh. Runs first = highest
    # priority; steady-state it is a trickle (only newly-OOS rows).
    if ONBUY_API_PUSH_ENABLED and onbuy_ready:
        oos_pending = []
        for idx, row in enumerate(data):
            if not row_in_ranges(idx + 2):
                continue
            status = str(row.get("Sync Status") or "").strip()
            # "Awaiting OnBuy go-live" rows ARE created products - excluding
            # them left sold-out listings live on the front end while the
            # listing stayed unaddressable (SKU 198651491114, 2026-08-24).
            # Zero them too; not-yet-addressable ones bounce and retry.
            if not (status.startswith("Synced") or status.startswith("Pending Approval")
                    or status.startswith("Awaiting OnBuy go-live")):
                continue
            raw_stock = str(row.get("Stock") if row.get("Stock") is not None else "").strip()
            if raw_stock == "":
                continue
            try:
                if int(float(raw_stock)) != 0:
                    continue
            except (TypeError, ValueError):
                continue
            if parse_time(row.get("Last OnBuy Sync", "")) >= parse_time(row.get("Last Checked Time", "")):
                continue  # the last push already carried this state
            sku = str(row.get("SKU") or "").strip()
            if not sku:
                continue
            try:
                price = float(row.get("Selling Price (£)") or 0)
            except (TypeError, ValueError):
                price = 0.0
            oos_pending.append((idx, sku, price if price > 0 else None))
        if oos_pending:
            logger.info("OOS pass: %d out-of-stock row(s) with a stale OnBuy push", len(oos_pending))
        if any(p is None for _, _, p in oos_pending):
            # A row with no usable sheet price must not push a placeholder -
            # 0.01 invites the "Price below minimum" auto-suspension, which
            # would lock the listing against its restock update later. Use
            # the listing's own current price; skip if neither side has one.
            from onbuy_client import BASE_URL as _oos_base
            _lp = {}
            _off = 0
            while True:
                def _oos_page(off=_off):
                    r = onbuy._send("GET", f"{_oos_base}/listings", what="OOS listings page",
                                    params={"site_id": onbuy.site_id, "limit": 100, "offset": off},
                                    timeout=60)
                    r.raise_for_status()
                    return r
                try:
                    _body = with_retry(_oos_page, what=f"OOS listings page {_off}", max_attempts=3).json()
                except Exception as exc:
                    logger.warning("OOS listing-price sweep failed (%s) - price-less rows skip this run", exc)
                    break
                _items = _body.get("results") if isinstance(_body, dict) else _body
                if not isinstance(_items, list) or not _items:
                    break
                for _it in _items:
                    _it = _it or {}
                    _s = str(_it.get("sku") or "").strip()
                    if _s:
                        try:
                            _lp[_s] = float(_it.get("price") or 0)
                        except (TypeError, ValueError):
                            pass
                if len(_items) < 100:
                    break
                _off += 100
            oos_pending = [(i2, s2, (p2 if p2 else _lp.get(s2))) for i2, s2, p2 in oos_pending]
            _skipped = sum(1 for _, _, p in oos_pending if not p)
            if _skipped:
                logger.info("OOS pass: %d row(s) skipped - no usable price on row or listing", _skipped)
            oos_pending = [t for t in oos_pending if t[2]]
        oos_done = oos_bounced = 0
        now_oos = datetime.now(PK_TZ).strftime("%Y-%m-%d %H:%M:%S")
        oos_updates = []
        # Batched like the activation pass (one call per 500 SKUs). Per-item
        # errors (hidden/suspended listings rejecting the update) stamp the
        # row so it does not monopolise this pass; it re-enters if a later
        # refresh changes its state again.
        for c0 in range(0, len(oos_pending), 500):
            if onbuy_halt_reason is not None:
                break
            chunk = oos_pending[c0:c0 + 500]
            onbuy_pushes_this_run += 1
            try:
                results = onbuy.update_listings_by_sku_batch(
                    [(sku, price, 0) for _idx, sku, price in chunk])
            except (TransientError, AuthError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                logger.warning("OOS batch postponed: %s", exc)
                if isinstance(exc, AuthError):
                    run_had_errors = True
                if isinstance(exc, (RateLimitError, AuthError)):
                    onbuy_halt_reason = str(exc)[:200]
                continue
            except Exception as exc:
                logger.warning("OOS batch failed outright: %s", str(exc)[:200])
                continue
            outcome = {}
            for it in results:
                it = it or {}
                s = str(it.get("sku") or "").strip()
                if s:
                    outcome[s] = str(it.get("error") or "").strip()
            for idx, sku, _price in chunk:
                i = idx + 2
                err = outcome.get(sku, "")
                if sku in outcome and not err:
                    oos_done += 1
                    if "Last OnBuy Sync" in col_map:
                        oos_updates.append({"range": f"{col_letter(col_map['Last OnBuy Sync'])}{i}", "values": [[now_oos]]})
                else:
                    oos_bounced += 1
                    if err:
                        logger.info("OOS push bounced for SKU %s (%s) - stamped", sku, err[:120])
                    if "Last OnBuy Sync" in col_map:
                        oos_updates.append({"range": f"{col_letter(col_map['Last OnBuy Sync'])}{i}", "values": [[now_oos]]})
            time.sleep(1.0)
        if oos_updates:
            try:
                with_retry(lambda: sheet.batch_update(
                    [dict(u) for u in oos_updates]), what="OOS sheet update", max_attempts=3)
            except Exception as exc:
                logger.error("OOS sheet update failed: %s", exc)
        if oos_done or oos_bounced:
            logger.info("OOS pass: %d zeroed on OnBuy, %d bounced", oos_done, oos_bounced)

    # ================= ACTIVATION PASS (2026-08-11) =================
    # OnBuy support confirmed: product-create ignores embedded price/stock
    # BY DESIGN - every new listing is born 0/0 and inactive, and only a
    # by-SKU stock/price update activates it. Waiting for the row's next
    # batch visit works on a small catalog (GTV/YRA activated thousands
    # that way) but loses the race against the zero-price auto-suspension
    # on a large one (OpenMaal's 6,000-row load: 3-6 day revisit vs 2-4
    # day suspension - and suspended listings reject all edits). So every
    # run FIRST activates created-but-never-synced rows directly from the
    # price/stock already on the row: no eBay calls, shares the OnBuy push
    # cap, freshest rows first. A row whose update bounces ("SKU does not
    # exist" - not addressable yet, or already suspended) gets its Last
    # OnBuy Sync stamped so it falls back to normal rotation instead of
    # monopolising this pass every run.
    if ONBUY_API_PUSH_ENABLED and onbuy_ready:
        pending_activation = []
        for idx, row in enumerate(data):
            if not row_in_ranges(idx + 2):
                continue
            status = str(row.get("Sync Status") or "").strip()
            # "Pending Approval" = fresh create; "Synced" with a blank Last
            # OnBuy Sync = the hourly backfill confirmed the queue before this
            # pass got to the row - either way we have never pushed its
            # price/stock, so it is still sitting 0/0 inactive on OnBuy.
            if not (status.startswith("Pending Approval") or status.startswith("Synced")):
                continue
            if str(row.get("Last OnBuy Sync") or "").strip():
                continue
            sku = str(row.get("SKU") or "").strip()
            if sku in PROTECTED_SKUS:
                continue
            try:
                a_price = float(row.get("Selling Price (£)") or 0)
                a_stock = int(float(row.get("Stock") or 0))
            except (TypeError, ValueError):
                continue
            if not sku or a_price <= 0 or a_stock <= 0:
                continue
            pending_activation.append((idx, sku, a_price, a_stock,
                                       str(row.get("Last Checked Time") or "")))
        # Freshest creations first - they are the ones still inside the
        # editable window.
        pending_activation.sort(key=lambda t: t[4], reverse=True)
        if pending_activation:
            logger.info("Activation pass: %d created-but-inactive row(s) pending", len(pending_activation))
        activation_updates = []
        activated = act_bounced = act_waiting = 0
        now_act = datetime.now(PK_TZ).strftime("%Y-%m-%d %H:%M:%S")
        # Batched per OnBuy support's own recommendation (2026-08-18): up to
        # 1,000 SKUs per PUT /v2/listings/by-sku request, answered 200 with
        # per-item errors inline. One chunk costs ONE call against the hourly
        # quota, so the whole pool fits in every run and the old half-budget
        # slot cap is obsolete. "SKU does not exist" items stay pending (no
        # stamp) - the queue hasn't made them addressable; genuinely rejected
        # rows drop out when the hourly backfill rewrites their status.
        for c0 in range(0, len(pending_activation), 500):
            if onbuy_halt_reason is not None:
                break
            chunk = pending_activation[c0:c0 + 500]
            onbuy_pushes_this_run += 1
            try:
                results = onbuy.update_listings_by_sku_batch(
                    [(sku, a_price, a_stock) for _idx, sku, a_price, a_stock, _t in chunk])
            except (TransientError, AuthError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                logger.warning("Activation batch postponed: %s", exc)
                if isinstance(exc, AuthError):
                    run_had_errors = True
                if isinstance(exc, (RateLimitError, AuthError)):
                    onbuy_halt_reason = str(exc)[:200]
                continue
            except Exception as exc:
                logger.warning("Activation batch failed outright: %s", str(exc)[:200])
                continue
            outcome = {}
            for it in results:
                it = it or {}
                s = str(it.get("sku") or "").strip()
                if s:
                    outcome[s] = str(it.get("error") or "").strip()
            for idx, sku, _p, _s2, _t in chunk:
                i = idx + 2
                if sku not in outcome:
                    act_waiting += 1
                    continue
                err = outcome[sku]
                if not err:
                    activated += 1
                    if "Sync Status" in col_map:
                        activation_updates.append({"range": f"{col_letter(col_map['Sync Status'])}{i}", "values": [["Synced"]]})
                    if "Last OnBuy Sync" in col_map:
                        activation_updates.append({"range": f"{col_letter(col_map['Last OnBuy Sync'])}{i}", "values": [[now_act]]})
                    if "OnBuy Listing Active" in col_map:
                        activation_updates.append({"range": f"{col_letter(col_map['OnBuy Listing Active'])}{i}", "values": [["TRUE"]]})
                elif "sku does not exist" in err.lower():
                    act_waiting += 1
                else:
                    act_bounced += 1
                    logger.info("Activation bounced for SKU %s (%s) - stamped back to rotation", sku, err[:120])
                    if "Last OnBuy Sync" in col_map:
                        activation_updates.append({"range": f"{col_letter(col_map['Last OnBuy Sync'])}{i}", "values": [[now_act]]})
            time.sleep(1.0)
        if activation_updates:
            try:
                with_retry(lambda: sheet.batch_update(
                    [dict(u) for u in activation_updates]), what="activation sheet update", max_attempts=3)
            except Exception as exc:
                logger.error("Activation sheet update failed: %s", exc)
        if activated or act_bounced or act_waiting:
            logger.info("Activation pass: %d activated, %d awaiting addressability, %d bounced to rotation", activated, act_waiting, act_bounced)

    # ================= PRODUCT ORDER =================
    # Rows with no usable Supplier URL yet (e.g. a SKU pre-filled ahead of the
    # rest of the row) can never actually be processed - the main loop below
    # just silently `continue`s past them without ever setting Last Checked
    # Time. Left in the sort, they never age out of "oldest first," so once
    # there are more of them than MAX_PRODUCTS_PER_RUN they permanently
    # occupy every run's entire batch and starve real, fully-filled-in rows
    # of any processing at all (confirmed: 770 SKU-only rows blocked all 500
    # slots in a run, so none of the 376 real rows were even reached).
    # Filtering them out before the sort/slice means batch capacity is only
    # ever spent on rows that can actually make progress.
    processable = [(idx, row) for idx, row in enumerate(data)
                   if "ebay." in str(row.get("Supplier URL", "")).strip().lower()
                   and row_in_ranges(idx + 2)]
    if rows_ranges:
        logger.info("Targeted run: %d processable row(s) inside the requested range(s)", len(processable))
    skipped_incomplete = len(data) - len(processable)
    if skipped_incomplete:
        logger.info("Skipping %d row(s) with no eBay Supplier URL yet (not counted against this run's batch)", skipped_incomplete)

    # Manual runs pick ONLY unfilled rows (no Title yet) by default - the
    # Run button exists to onboard newly added products fast, not to
    # re-fetch the whole catalogue (user policy 2026-07-22; a routine
    # manual trigger was spending 28 minutes re-fetching 1,300 filled
    # rows). Scheduled runs keep the full oldest-first rotation. Setting
    # refresh_all=yes on the dispatch form restores a deliberate full
    # sweep (repricing days). Local runs behave like scheduled.
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch" \
            and not rows_ranges \
            and str(os.getenv("REFRESH_ALL") or "").strip().lower() not in ("yes", "true", "1"):
        _unfilled = [(idx, row) for idx, row in processable
                     if not str(row.get("Title") or "").strip()]
        logger.info("Manual run: limited to %d unfilled row(s) of %d processable - filled rows "
                    "refresh on scheduled runs; set refresh_all=yes for a full sweep",
                    len(_unfilled), len(processable))
        processable = _unfilled

    if FULL_REFRESH:
        sorted_data = processable
    else:
        sorted_data = sorted(processable, key=lambda x: parse_time(x[1].get("Last Checked Time", "")))

    # While testing the OnBuy API push against a specific SKU allowlist, move
    # those SKUs to the front of the queue - otherwise a manual test run can
    # easily land on a batch that doesn't include any of them (oldest-checked
    # rows win by default), making it look like the push silently did nothing
    # when really it just never got a chance to run.
    if ONBUY_API_PUSH_ENABLED and ONBUY_API_TEST_SKUS:
        sorted_data = sorted(
            sorted_data,
            key=lambda x: str(x[1].get("SKU") or "").strip() not in ONBUY_API_TEST_SKUS,
        )

    logger.info("Processing %d products", min(len(sorted_data), MAX_PRODUCTS_PER_RUN))

    # ================= MAIN UPDATE LOOP =================
    token = get_ebay_token()
    if not token:
        # Abort instead of proceeding to call every row with a bad/missing
        # token - the old code sent "Authorization: Bearer None" per row,
        # which zeroed price/stock for the entire batch on a single auth failure.
        logger.error("Could not obtain an eBay token - aborting run without touching any rows")
        notify.send_alert_email(
            "eBay authentication failed - run aborted",
            "generate_xml.py could not obtain an eBay OAuth token this run. "
            "No sheet rows were touched. Check EBAY_CLIENT_ID/EBAY_CLIENT_SECRET.",
        )
        sys.exit(1)

    removed_skus = []  # matching SKUs, for the Supabase delete + summary log
    supabase_rows = []  # one upsert for the whole run - every row must have
    # identical keys (PostgREST's bulk-upsert requirement) AND every NOT NULL
    # column must be present (Postgres validates that on the candidate insert
    # row before it even checks ON CONFLICT, so a partial-column "tracking
    # only" upsert can never work here - see fetch_existing_fields()).
    highlight_requests = []
    all_sheet_updates = []  # accumulated across every row, written in ONE batch_update
    # after the loop instead of one call per row - a run can now process
    # hundreds of rows (see dynamic batch sizing above), and one Sheets API
    # write call per row at that scale risks Google's own rate limits, which
    # weren't a concern back when this was capped at a hardcoded 12/run.
    num_cols = len(headers)

    batch = sorted_data[:MAX_PRODUCTS_PER_RUN]

    # Within the selected batch, hand the limited OnBuy push slots
    # (ONBUY_MAX_PUSHES_PER_RUN) to rows that have never been pushed first
    # (blank "Last OnBuy Sync" parses to year 2000), then oldest-pushed.
    # Without this, processing order == Last Checked Time order, which is
    # stable across runs - so the same ~200 rows won the push slots every
    # run and rows beyond the cap (including genuinely new, never-listed
    # products) never reached OnBuy at all. Batch *selection* above stays
    # based on Last Checked Time (eBay refresh fairness); this only reorders
    # within the same set, so it changes who gets OnBuy slots, not which
    # rows get their eBay/Sheet refresh. Skipped while a test-SKU allowlist
    # is active so those keep absolute front-of-queue priority.
    if not (ONBUY_API_PUSH_ENABLED and ONBUY_API_TEST_SKUS):
        batch.sort(key=lambda x: parse_time(str(x[1].get("Last OnBuy Sync") or "")))

    # Pre-fetch OPC + OnBuy-tracking fields already on record for this run's
    # batch, so the single Supabase upsert (below) can carry forward real
    # values instead of blanking them out for rows not pushed to OnBuy this
    # run - see fetch_existing_fields() for why this has to be a single
    # always-full-row upsert rather than a separate partial-column one.
    skus_in_batch = [str(row.get("SKU") or "").strip() for _, row in batch]
    skus_in_batch = [s for s in skus_in_batch if s]
    existing_fields = supabase_db.fetch_existing_fields(skus_in_batch)

    for idx, row in batch:
        i = idx + 2
        url = str(row.get("Supplier URL", "")).strip()

        if "ebay." not in url.lower():
            continue

        try:
            available, ebay_data = get_ebay_data(url, token)
        except (TransientError, PermanentError) as exc:
            fetch_failures += 1
            run_had_errors = True
            logger.error("Row %d (%s): fetch failed after retries, leaving existing values untouched - %s", i, url, exc)
            continue

        stock = ebay_data["stock"]
        cost_price = ebay_data["price"]

        # When eBay reports the item unavailable (removed/no price/out of
        # stock), ebay_data's descriptive fields are all blank - previously
        # those blanks got written straight over the Sheet's existing good
        # data, making the row look emptied out. Only stock/price/status
        # should reflect "unavailable"; title/description/images/brand keep
        # whatever was already there.
        if available:
            title = ebay_data["title"]
            description = ebay_data["description"]
            brand = ebay_data["brand"]
            main_image = ebay_data["main_image"]
            additional_images = ebay_data["additional_images"]
        else:
            title = str(row.get("Title") or "")
            description = str(row.get("Description") or "")
            brand = normalize_brand(str(row.get("Brand") or ""))
            main_image = str(row.get("Image URL") or "")
            additional_images = [img.strip() for img in str(row.get("Additional Images") or "").split(",") if img.strip()]

        # ================= SKU (must be entered manually - OnBuy requires unique
        # SKUs, and two different sourcing links can share the same barcode/item
        # ID, so auto-deriving one risks a collision between two real products) ==
        sku = str(row.get("SKU") or "").strip()
        if not sku:
            logger.warning("Row %d: no SKU provided (OnBuy requires a unique SKU per product) - skipping until one is added", i)
            continue

        # ================= CATEGORY (re-checked here with fresh title/description so a
        # brand-new row gets categorized on this same pass, not just the upfront
        # full-catalog remap above, which ran before this row's eBay data existed) ====
        current_category = str(row.get("Category") or "").strip()
        manual_unresolved = False
        if is_valid_onbuy_category(current_category):
            category = current_category
            category_needs_write = False
            if RECATEGORIZE_FROM_TYPE:
                _by_type = type_category(
                    (ebay_data.get("product_type") or "") if isinstance(ebay_data, dict) else "",
                    title, description)
                if _by_type and _by_type != current_category:
                    logger.info("Recategorized by eBay Type: %r -> %r", current_category, _by_type)
                    category = _by_type
                    category_needs_write = True
        elif current_category:
            # typed by a human - resolve (renames/ids/unique leaf) or keep as typed
            category, _ok = resolve_manual_category(current_category, row.get("Category ID"))
            category_needs_write = _ok and category != current_category
            manual_unresolved = not _ok
        else:
            category = map_onbuy_category(title, current_category, description,
                                          (ebay_data.get("product_type") or "") if isinstance(ebay_data, dict) else "")
            category_needs_write = category != current_category
        category_id = category_id_by_path.get(category.strip().lower())

        # ================= PRICING =================
        # Default margin is a floor, not a fixed price: if a product's price
        # already implies more than the default 40% total margin (20% fee +
        # 20% profit), leave it alone - only bump prices UP that currently
        # imply less than the default, never silently lower a price someone
        # deliberately set higher.
        shipping_cost = float(row.get("Shipping Cost (£)") or 0)
        formula_price = pricing.calculate_selling_price(cost_price, shipping_cost)
        existing_price = float(row.get("Selling Price (£)") or 0)
        # Out-of-stock must never destroy the price: writing 0 into the
        # Selling Price cell erased manually-raised prices (max() only
        # protects what's still in the cell), and on restock the formula
        # price silently replaced the manual one (Arden, user report
        # 2026-08-28). Keep the price; stock 0 rides on its own column and
        # push (stock-0 updates with a real price are valid - the OOS pass
        # already pushes exactly that).
        selling_price = max(existing_price, formula_price)

        # ================= PRICE CHECK FLAG =================
        # Normal = at/near the default margin, Medium = moderately above it,
        # High = well above it. Thresholds are a judgment call on "a little
        # more" / "much more" - adjust PRICE_CHECK_MEDIUM_MAX_PCT /
        # PRICE_CHECK_HIGH_MIN_PCT below if these don't match what you meant.
        if stock == 0 or cost_price <= 0:
            price_check_flag = ""
        else:
            margin_pct = (selling_price - cost_price) / cost_price * 100
            if margin_pct <= PRICE_CHECK_NORMAL_MAX_PCT:
                price_check_flag = "Normal"
            elif margin_pct <= PRICE_CHECK_MEDIUM_MAX_PCT:
                price_check_flag = "Medium"
            else:
                price_check_flag = "High"

        additional_images_str = ",".join(additional_images)
        now_str = datetime.now(PK_TZ).strftime("%Y-%m-%d %H:%M:%S")
        is_active = stock > 0

        # ================= ONBUY API PUSH (gated, see ONBUY_API_PUSH_ENABLED) =================
        # Runs before the sheet write below so the outcome (Sync Status, OPC
        # placeholder, etc.) can go into the SAME batch_update call instead of
        # a second Sheets API round-trip per row.
        # EAN column (Sheet/Supabase): the SKU's numeric part IS the EAN
        # (user policy 2026-07-13 - every product is a new listing under the
        # seller's own barcode). eBay's own barcode is only a fallback for
        # rows whose SKU somehow has no digits.
        ean = sku_numeric_part(sku) or ebay_data.get("product_code") or ""
        sync_status = None
        onbuy_product_created = None
        onbuy_listing_active = None
        onbuy_product_id = None
        last_onbuy_sync = None

        if (sku and onbuy_ready and onbuy_halt_reason is None
                and should_push_to_onbuy(sku) and onbuy_pushes_this_run < ONBUY_MAX_PUSHES_PER_RUN):
            existing = existing_fields.get(sku, {})
            # Supabase first, Sheet as fallback - the Sheet carries the same
            # tracking columns (backfill writes both), so the guard below
            # still works on a run where the Supabase pre-fetch failed.
            last_sync_status = str(existing.get("Sync Status") or row.get("Sync Status") or "")

            # A brand that's a registered trademark another seller already
            # owns on OnBuy isn't a bug to route around - it's a real product
            # this business isn't allowed to list under that brand at all.
            # User's explicit policy (2026-07-06, superseding the earlier
            # "mark it Unbranded and relist" policy): remove the row entirely
            # instead of relisting it as Unbranded.
            if "supplied brand is owned by another seller" in last_sync_status:
                # Policy changed 2026-08-03 (user): FLAG, never delete. The
                # original delete-the-row rule was written for GTV's rare
                # one-off rejections; at YRA's volume it silently destroyed
                # 124+ rows in a day along with every record of WHICH brands
                # bounced. The row now stays, turns amber, and stops being
                # pushed - the team decides to re-source or re-brand.
                onbuy_brand_blocked += 1
                brand_alert = (f"BRAND BLOCKED - OnBuy says the brand '{brand}' is owned by "
                               "another seller, so this product cannot be listed under it. "
                               "Replace the link with a different product, or correct the Brand cell "
                               "- the row retries automatically on the next run.")
                # Full-auto sheets have no Change Alert column - Sync Status
                # is where a human looks, so the instruction goes there.
                all_sheet_updates.append(
                    {"range": f"{col_letter(col_map['Sync Status'])}{i}", "values": [[brand_alert]]})
                if "Change Alert" in col_map:
                    all_sheet_updates.append(
                        {"range": f"{col_letter(col_map['Change Alert'])}{i}", "values": [[brand_alert]]})
                if "Last Checked Time" in col_map:
                    all_sheet_updates.append(
                        {"range": f"{col_letter(col_map['Last Checked Time'])}{i}", "values": [[now_str]]})
                highlight_requests.append(
                    row_highlight_request(sheet.id, i, num_cols, False))
                logger.info(
                    "Row %d (SKU %s): BRAND BLOCKED (%s) - flagged, not deleted, not pushed",
                    i, sku, brand,
                )
                continue

            onbuy_pushes_this_run += 1
            # OnBuy's product code = the numeric part of the seller's own SKU
            # (the pre-validated UPC; non-digit decoration around it is
            # allowed and stripped). Being numeric and the right length isn't
            # enough on its own - confirmed two real SKUs got rejected ("not
            # a valid product code") despite both being 12-digit numbers,
            # because their check digit isn't a real GS1/UPC checksum. Only
            # forward it if it actually passes that check; otherwise send
            # blank rather than repeat the rejection.
            sku_digits = sku_numeric_part(sku)
            upc_for_onbuy = sku_digits if is_valid_gtin(sku_digits) else ""

            # OnBuy's own brand-matching backend can also crash outright on a
            # brand it doesn't recognize ("MatchedBrandData...Argument #1
            # ($id) must be of type int, null given") - that's a bug on their
            # end, unrelated to trademark ownership, so it still gets retried
            # as Unbranded rather than repeating the same crash.
            brand_for_onbuy = brand
            if "MatchedBrandData::__construct" in last_sync_status:
                brand_for_onbuy = "Unbranded"

            # "SKU does not exist" from update_listing does NOT always mean
            # the product was never created. OnBuy's queue confirms a creation
            # as success (OPC issued, findable in the Add Listing search) days
            # before the listing becomes addressable via PUT /listings/by-sku
            # - and falling back to create_product in that window re-submits
            # the same product, which OnBuy answers with a NEW OPC instead of
            # matching the existing record (confirmed 2026-07-06: most of the
            # 07-04 rollout's products got duplicated this way on the next
            # full run). So the create fallback is only allowed when our own
            # records say this SKU was never successfully submitted: no real
            # OPC on record and no submitted/synced status. Rows whose last
            # submission outright Failed keep the fallback - re-creating is
            # exactly how those recover.
            opc_on_record = str(existing.get("OPC") or row.get("OPC") or "").strip()
            already_created = (
                opc_on_record.upper() not in ("", "PENDING")
                or last_sync_status.startswith(("Synced", "Pending Approval", "Awaiting OnBuy go-live"))
            )

            try:
                if not already_created and not available:
                    # Dead sourcing link on a never-created row: eBay says
                    # the listing is gone (404/no price), so there is no
                    # title, price, or stock to build a product from - a
                    # create can only ever fail. 31 such rows across the
                    # Full-tier stores were red-flagging every run with "no
                    # matching OnBuy category" (2026-08-06), retrying
                    # forever. Skip the push: not an error, and deliberately
                    # NOT "Failed" (that keyword reopens the create
                    # fallback). The row keeps its Sheet/Supabase refresh
                    # and pushes normally if the link starts resolving again
                    # or is replaced. (Costs one push slot of the per-run
                    # cap - not worth restructuring the loop over.)
                    raise _SkipPushDead()
                if not already_created and not str(main_image or "").strip():
                    # OnBuy 400s a create whose default_image is empty (all
                    # source images dead/rejected - 3 cases this week). A
                    # data problem for a human, not a system fault: flag the
                    # row and keep the run green; it retries the moment the
                    # source gets working images.
                    raise PermanentError("no usable product image - fix the source images or replace the link")
                if not already_created and category_id is None:
                    # A create can't succeed without a category (OnBuy 400s
                    # on a null category_id), and the matcher now refuses to
                    # guess rather than pick something wrong - flag the row
                    # for a human instead of burning the API call on a known
                    # rejection. Price/stock updates don't need a category,
                    # so already-created rows are unaffected.
                    raise PermanentError(
                        "Category cell not recognised by OnBuy - check the spelling against the OnBuy category list (kept as typed)"
                        if manual_unresolved else
                        "no matching OnBuy category - fill in the Category column and it will retry")
                if sku in PROTECTED_SKUS:
                    raise _SkipPushProtected()
                if not already_created and not ONBUY_CREATE_ENABLED:
                    raise _SkipCreatePaused()
                if already_created:
                    if selling_price <= 0:
                        raise _SkipPushNoPrice()
                    result = onbuy.update_listing(sku=sku, price=selling_price, stock=stock)
                    action = "updated"
                else:
                    # A row can reach the create path with its SKU ALREADY a
                    # live listing - the team entered an old product without
                    # "Synced" (Makstore 2026-08-28: 86 rows create-pathed
                    # into phantom queue entries). One check-winning call
                    # answers only for existing listings, so a hit means
                    # adopt via the plain update; any check failure falls
                    # through to the normal create.
                    _live_hit = False
                    try:
                        _res = onbuy.check_winning([sku]) or []
                        _live_hit = any(str((_e or {}).get("sku") or "").strip() == sku for _e in _res)
                    except Exception as _exc:
                        logger.info("live-SKU pre-create check failed for %s (%s) - proceeding with create", sku, str(_exc)[:80])
                    if _live_hit:
                        logger.info("SKU %s is already a live listing - adopting via update instead of creating a duplicate", sku)
                        result = onbuy.update_listing(sku=sku, price=selling_price, stock=stock)
                        action = "updated"
                    else:
                        action, result = onbuy.sync_product(
                            sku=sku,
                            ean=upc_for_onbuy,
                            title=title or str(row.get("Title") or ""),
                            description=description,
                            brand=brand_for_onbuy,
                            category_id=category_id,
                            price=selling_price,
                            stock=stock,
                            main_image=main_image,
                            additional_images=additional_images,
                        )
                logger.info("OnBuy %s: %s", action, sku)
                # Stamp ONLY updates. OnBuy confirmed (support, 2026-08-11)
                # that product-create IGNORES the embedded price/stock by
                # design: the listing is born 0/0 and inactive, and only a
                # follow-up by-SKU update activates it (~30 min processing).
                # Stamping on create rotated the fresh row to the BACK of a
                # multi-day queue, so the activating update arrived days
                # late - after OnBuy's "Price below minimum" auto-suspension
                # had already fired, and suspended listings reject all
                # edits. An unstamped created row keeps front-of-queue
                # priority: the very next run sends the activating update
                # while the listing is still editable.
                if action == "updated":
                    last_onbuy_sync = now_str
                if action == "created":
                    onbuy_created += 1
                    # Accepted into OnBuy's async approval queue - not confirmed live yet.
                    # The real OPC/approval status only appears later via
                    # OnBuyClient.check_queue(); this pipeline doesn't poll for it, so
                    # these reflect "submitted", not "confirmed active".
                    sync_status = "Pending Approval"
                    onbuy_product_created = "TRUE"
                    onbuy_listing_active = "FALSE"
                    onbuy_product_id = str(result.get("queue_id", "")) if isinstance(result, dict) else ""
                else:
                    onbuy_updated += 1
                    sync_status = "Synced"
                    onbuy_product_created = "TRUE"
                    onbuy_listing_active = "TRUE"
            except _SkipPushNoPrice:
                onbuy_no_price += 1
                logger.info(
                    "Row %d (SKU %s): no usable selling price - push skipped, not failed "
                    "(the OOS pass zeroes stock with the listing price)", i, sku)
            except _SkipPushProtected:
                onbuy_protected += 1
                logger.info("Row %d (SKU %s): PROTECTED - OnBuy content repair pending, push skipped", i, sku)
            except _SkipCreatePaused:
                onbuy_create_paused += 1
                logger.info("Row %d (SKU %s): create PAUSED (ONBUY_CREATE_ENABLED=false) - waiting", i, sku)
            except _SkipPushDead:
                onbuy_skipped_dead += 1
                sync_status = "Skipped: eBay listing unavailable - replace or remove the link"
                logger.info(
                    "Row %d (SKU %s): eBay listing unavailable and never created on OnBuy "
                    "- nothing to create, skipping the push", i, sku)
            except (TransientError, AuthError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                # OnBuy-side or transport trouble (rate limit, 5xx after
                # retries, expired token even after the client's one re-auth,
                # network blip) - the product itself was NOT rejected, so
                # leave Sync Status exactly as it was: writing "Failed" here
                # would reopen the create fallback for a recently created row
                # whose real OPC hasn't been backfilled yet, and re-creating
                # is exactly how the 07-06 duplicates were minted. No Last
                # OnBuy Sync stamp either, so these rows keep their place at
                # the front of the push order next run.
                # (AuthError subclasses PermanentError, so it must be listed
                # here, before the PermanentError handler below.)
                onbuy_postponed += 1
                # Postponed pushes are SELF-HEALING by design (status
                # untouched, front-of-queue retry next run), so a scattered
                # network blip or a rate-limit halt doesn't red the run -
                # hitting the 240/hr cap is the cap doing its job, seen
                # whenever a manual run shares an hour with a scheduled one
                # (2026-08-06). A token that can't refresh IS a real fault:
                # nothing will push until someone fixes credentials.
                if isinstance(exc, AuthError):
                    run_had_errors = True
                logger.warning("OnBuy push postponed for SKU %s: %s", sku, exc)
                if isinstance(exc, (RateLimitError, AuthError)):
                    # The hourly quota won't come back mid-run, and a token
                    # that couldn't be refreshed won't start working again -
                    # pushing on would just burn time failing row after row.
                    onbuy_halt_reason = str(exc)[:200]
                    logger.warning(
                        "Halting OnBuy pushes for the rest of this run (%s) - remaining rows "
                        "still get their Sheet/Supabase refresh and will push next run",
                        onbuy_halt_reason,
                    )
            except PermanentError as exc:
                if already_created and "SKU does not exist" in str(exc):
                    # Created earlier, OnBuy just hasn't made the listing
                    # addressable yet - not a failure, and NOT a reason to
                    # re-create. Stamp Last OnBuy Sync so this row rotates to
                    # the back of the push-priority order (batch sort above)
                    # instead of holding a front slot every run; the update
                    # will simply succeed on a later attempt once OnBuy makes
                    # the listing live.
                    onbuy_deferred += 1
                    sync_status = "Awaiting OnBuy go-live (created earlier - listing not yet updatable)"
                    last_onbuy_sync = now_str
                    logger.info(
                        "Row %d (SKU %s): created earlier (OPC %s) but OnBuy's listing isn't "
                        "updatable yet - deferring, not re-creating",
                        i, sku, opc_on_record or "pending",
                    )
                elif already_created and "suspended listings cannot be edited" in str(exc).lower():
                    # The listing exists but OnBuy has it suspended (e.g. the
                    # zero-price auto-suspension) and rejects every edit until
                    # the suspension lifts. NOT "Failed" - on a row with no
                    # OPC on record that reopens the create fallback and mints
                    # a duplicate product. Keep the created-guard satisfied,
                    # stamp the sync so the row rotates instead of holding a
                    # front slot, and retry on every future visit; the moment
                    # OnBuy reactivates the listing the update just succeeds.
                    onbuy_suspended_locked += 1
                    sync_status = "Synced (suspended on OnBuy - awaiting reactivation)"
                    last_onbuy_sync = now_str
                    logger.info(
                        "Row %d (SKU %s): listing suspended on OnBuy - edits rejected until "
                        "reactivation, will keep retrying on rotation", i, sku)
                elif "no usable product image" in str(exc):
                    # Same worklist treatment as the category case: a human
                    # fixes the source images; the run stays green.
                    onbuy_needs_category += 1
                    sync_status = f"Failed: {str(exc)[:300]}"
                    logger.warning("SKU %s needs working images before it can list: %s", sku, exc)
                elif "no matching OnBuy category" in str(exc):
                    # A product waiting for its Category cell is a WORKLIST
                    # item, not a system failure (2026-08-06): with a growing
                    # catalog every run carries a few brand-new no-Type
                    # products, so exiting 1 for them kept the workflow
                    # permanently red - which buried the failures that
                    # matter. The status keeps the exact "Failed: no
                    # matching OnBuy category" wording (the create-fallback
                    # and autofill's gate both key on it) and the row
                    # retries the moment a category lands; the run itself
                    # stays green.
                    onbuy_needs_category += 1
                    sync_status = f"Failed: {str(exc)[:300]}"
                    logger.warning("SKU %s needs a category before it can list: %s", sku, exc)
                else:
                    onbuy_failed += 1
                    run_had_errors = True
                    sync_status = f"Failed: {str(exc)[:300]}"
                    logger.error("OnBuy push failed for SKU %s: %s", sku, exc)
            except Exception as exc:
                onbuy_failed += 1
                run_had_errors = True
                # Previously just "Failed" with no reason - the actual cause
                # only ever reached the run's log, not anywhere the user could
                # see it without downloading that specific Actions run's log.
                sync_status = f"Failed: {str(exc)[:300]}"
                logger.error("OnBuy push failed for SKU %s: %s", sku, exc)
            # Confirmed from the account's own API usage page: 240 PUT/POST per
            # hour. Paired with ONBUY_MAX_PUSHES_PER_RUN above, this keeps a
            # single large run from bursting through the hourly limit on its own.
            time.sleep(0.5)

        row_updates = [
            {"range": f"{col_letter(col_map['Cost Price (£)'])}{i}", "values": [[cost_price]]},
            {"range": f"{col_letter(col_map['Stock'])}{i}", "values": [[stock]]},
            {"range": f"{col_letter(col_map['Selling Price (£)'])}{i}", "values": [[selling_price]]},
            {"range": f"{col_letter(col_map['Status'])}{i}", "values": [["ACTIVE" if is_active else "INACTIVE"]]},
            {"range": f"{col_letter(col_map['Description'])}{i}", "values": [[description]]},
            {"range": f"{col_letter(col_map['Image URL'])}{i}", "values": [[main_image]]},
            {"range": f"{col_letter(col_map['Additional Images'])}{i}", "values": [[additional_images_str]]},
            {"range": f"{col_letter(col_map['Brand'])}{i}", "values": [[brand]]},
            {"range": f"{col_letter(col_map['Title'])}{i}", "values": [[title]]},
            {"range": f"{col_letter(col_map['Last Updated'])}{i}", "values": [[now_str]]},
            {"range": f"{col_letter(col_map['Last Checked Time'])}{i}", "values": [[now_str]]},
        ]
        if category_needs_write:
            row_updates.append({"range": f"{col_letter(col_map['Category'])}{i}", "values": [[category]]})
        if "Price Check Flag" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['Price Check Flag'])}{i}", "values": [[price_check_flag]]})
        if "Condition" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['Condition'])}{i}",
                                "values": [[ebay_data.get("condition") or "New"]]})
        if "EAN" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['EAN'])}{i}", "values": [[ean]]})
        # OnBuy-provided tracking fields, written to the Sheet only if those
        # columns exist there and only when a push actually happened this run
        # - otherwise leaving them out preserves whatever was already there.
        if sync_status and "Sync Status" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['Sync Status'])}{i}", "values": [[sync_status]]})
        if onbuy_product_created and "OnBuy Product Created" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['OnBuy Product Created'])}{i}", "values": [[onbuy_product_created]]})
        if onbuy_listing_active and "OnBuy Listing Active" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['OnBuy Listing Active'])}{i}", "values": [[onbuy_listing_active]]})
        if onbuy_product_id and "OnBuy Product ID" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['OnBuy Product ID'])}{i}", "values": [[onbuy_product_id]]})
        if last_onbuy_sync and "Last OnBuy Sync" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['Last OnBuy Sync'])}{i}", "values": [[last_onbuy_sync]]})

        all_sheet_updates.extend(row_updates)
        updated_count += 1
        logger.info("Processed row %d", i)
        highlight_requests.append(row_highlight_request(sheet.id, i, num_cols, is_active))
        time.sleep(0.2)  # light pacing on eBay fetches; OnBuy pushes are paced separately below

        # ================= SUPABASE EXPORT ROW (upserted once after the loop) =================
        # Every row - including OnBuy-tracking fields - goes in this one list,
        # and every row must have identical keys AND real values for every NOT
        # NULL column (see fetch_existing_fields() for why a separate
        # partial-column upsert doesn't work here).
        existing = existing_fields.get(sku, {})
        supabase_row = {
            "SKU": sku,
            "Title": title or str(row.get("Title") or ""),
            "Description": description,
            "Brand": brand,
            "Category": category,
            "Category ID": str(category_id) if category_id is not None else None,
            "Supplier URL": url,
            "Supplier": "eBay",
            "Cost Price (£)": cost_price,
            "Shipping Cost (£)": str(shipping_cost) if shipping_cost else None,
            "Profit %": str(pricing.MIN_PROFIT_PERCENT),
            "Fee %": str(pricing.PLATFORM_FEE_PERCENT),
            "Stock": stock,
            "Selling Price (£)": selling_price,
            "Status": "ACTIVE" if stock > 0 else "INACTIVE",
            "Last Updated": datetime.now(PK_TZ).isoformat(),
            "Image URL": main_image,
            "Additional Images": additional_images_str,
            "Condition": ebay_data.get("condition") or "New",
            "Last Checked Time": datetime.now(PK_TZ).isoformat(),
            "EAN": ean,
            "Listing ID": str(row.get("Listing ID") or "").strip() or None,
            # OPC (OnBuy's permanent product code) is only known once the async
            # queue clears - see OnBuyClient.check_queue(). This column is NOT
            # NULL, so a genuinely new row needs a placeholder - but reuse the
            # real value from Supabase if backfill_onbuy_status.py already
            # found one, instead of stomping it back to "PENDING" every run.
            "OPC": existing.get("OPC") or "PENDING",
            # OnBuy-tracking fields: use this run's fresh value if a push was
            # attempted, otherwise carry forward whatever was already there
            # (never blank it out) - see fetch_existing_fields() for why
            # these have to live on the same row as the fields above rather
            # than a separate partial-column upsert.
            "Sync Status": sync_status or existing.get("Sync Status") or "",
            # These three are boolean/boolean/timestamp under the canonical
            # schema, so "" is not a legal value for any of them - see
            # carry_forward(). "FALSE" is the truthful default for a row
            # never pushed (not created, not live); NULL for a sync that
            # never happened.
            "OnBuy Product Created": carry_forward(onbuy_product_created, existing.get("OnBuy Product Created"), "FALSE"),
            "OnBuy Listing Active": carry_forward(onbuy_listing_active, existing.get("OnBuy Listing Active"), "FALSE"),
            "OnBuy Product ID": onbuy_product_id or existing.get("OnBuy Product ID") or "",
            "Last OnBuy Sync": carry_forward(last_onbuy_sync, existing.get("Last OnBuy Sync")),
        }
        supabase_rows.append(supabase_row)

    # ================= APPLY ALL SHEET VALUE UPDATES (one call for the whole run) =================
    if all_sheet_updates:
        # gspread's batch_update() mutates each dict's "range" in place
        # (unconditionally re-qualifying it with the sheet name, even if
        # already qualified - confirmed from its source). Passing the same
        # list to a retried call would double-qualify the range on the 2nd
        # attempt ('Sheet1'!'Sheet1'!I35), which is invalid and fails outright.
        # Keep the original (range, values) pairs immutable and rebuild fresh
        # dicts on every attempt so a retry never sees an already-mutated one.
        original_pairs = [(u["range"], u["values"]) for u in all_sheet_updates]

        def _do_sheet_update():
            fresh_updates = [{"range": r, "values": v} for r, v in original_pairs]
            return sheet.batch_update(fresh_updates)

        try:
            with_retry(_do_sheet_update, what="sheet batch update", max_attempts=3)
        except Exception as exc:
            run_had_errors = True
            # This is an all-or-nothing commit for the whole run's Sheet writes -
            # a real trade-off against doing one API call per row (which risked
            # Google's own rate limits once batch sizes grew past a hardcoded
            # 12/run). OnBuy/Supabase may already reflect this run's changes
            # even if this call fails - retried 3x before giving up, so a
            # transient blip is unlikely to lose everything.
            logger.error("Sheet batch update failed after retries - this run's Sheet changes may not be saved: %s", exc)

    supabase_rows = dedupe_rows_by_sku(supabase_rows, "Supabase export")
    supabase_ok = supabase_db.upsert_products(supabase_rows)

    if highlight_requests:
        try:
            with_retry(
                sheet.spreadsheet.batch_update,
                {"requests": highlight_requests},
                what="row highlight formatting",
                max_attempts=3,
            )
        except Exception as exc:
            logger.error("Row highlighting failed (values were still updated correctly): %s", exc)

    # ================= REMOVE BRAND-REJECTED ROWS ENTIRELY =================
    # Runs after every other Sheet write/highlight above so deleting these
    # rows can't shift row numbers out from under one of those, which only
    # ever target rows that are staying (a row queued for deletion `continue`d
    # past building any Sheet update for itself). Supabase is deleted first,
    # not the Sheet row - if the Sheet delete then fails, the row survives to
    # be retried (and re-rejected, re-detected, re-deleted) next run; the
    # reverse order would risk a permanently orphaned Supabase row with no
    # Sheet row left to ever trigger cleaning it up.
    if rows_to_delete:
        supabase_db.delete_products(removed_skus)
        delete_requests = [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet.id,
                        "dimension": "ROWS",
                        "startIndex": row_num - 1,
                        "endIndex": row_num,
                    }
                }
            }
            for row_num in sorted(set(rows_to_delete), reverse=True)
        ]
        try:
            with_retry(
                sheet.spreadsheet.batch_update,
                {"requests": delete_requests},
                what="delete brand-rejected rows",
                max_attempts=3,
            )
            logger.info(
                "Removed %d row(s) entirely - brand owned by another seller (SKUs: %s)",
                len(rows_to_delete), ", ".join(removed_skus),
            )
        except Exception as exc:
            run_had_errors = True
            logger.error(
                "Failed to delete brand-rejected row(s) from the Sheet (Supabase row(s) "
                "already removed) - SKUs %s: %s", ", ".join(removed_skus), exc,
            )

    # ================= GENERATE XML (kept as fallback) =================
    root = ET.Element("products")
    feed_count = 0
    skipped_feed = 0

    for row in sheet.get_all_records():
        try:
            sku = str(row.get("SKU") or "").strip()
            title = str(row.get("Title") or "").strip()
            desc = str(row.get("Description") or "").strip()
            brand = str(row.get("Brand") or "").strip()
            category = clean_category(row.get("Category"))
            image = to_jpg(row.get("Image URL"))
            additional_images = [img.strip() for img in str(row.get("Additional Images") or "").split(",") if img.strip()][:10]
            price = float(row.get("Selling Price (£)") or 0)
            stock = int(row.get("Stock") or 0)

            if not all([sku, title, category]):
                skipped_feed += 1
                continue

            product = ET.SubElement(root, "product")
            ET.SubElement(product, "sku").text = sku
            ET.SubElement(product, "product_name").text = title[:150]
            ET.SubElement(product, "description").text = desc
            ET.SubElement(product, "image_url").text = image

            for img_idx, img in enumerate(additional_images):
                ET.SubElement(product, f"additional_image_url_{img_idx + 1}").text = img

            ET.SubElement(product, "brand").text = brand
            ET.SubElement(product, "category").text = category
            ET.SubElement(product, "condition").text = "New"
            ET.SubElement(product, "ean").text = sku
            ET.SubElement(product, "price").text = str(price)
            ET.SubElement(product, "quantity").text = str(stock)

            feed_count += 1
        except Exception:
            skipped_feed += 1

    # Supabase Storage rejects objects over its size cap (OpenMaal's feed of
    # ~5,700 products with full HTML descriptions got 413 "Payload too large"
    # on every run, 2026-08-22). The hosted feed is a catalogue mirror, not
    # what OnBuy reads (the API pushes are), so when the document is too big
    # shorten the descriptions rather than fail the upload.
    FEED_MAX_BYTES = 40 * 1024 * 1024
    feed_bytes = ET.tostring(root, encoding="utf-8")
    if len(feed_bytes) > FEED_MAX_BYTES:
        for _d in root.iter("description"):
            if _d.text and len(_d.text) > 400:
                _d.text = _d.text[:400]
        feed_bytes = ET.tostring(root, encoding="utf-8")
        logger.info("Feed over %d MB - descriptions shortened to 400 chars for the hosted copy (now %.1f MB)",
                    FEED_MAX_BYTES // (1024 * 1024), len(feed_bytes) / (1024 * 1024))
    ET.ElementTree(root).write("feed.xml", encoding="utf-8", xml_declaration=True)
    feed_url = storage.upload_feed()

    # ================= FINAL LOGS + ALERTS =================
    logger.info("DONE")
    logger.info("Updated rows: %d", updated_count)
    logger.info("OnBuy: %d created, %d updated, %d deferred (awaiting go-live), %d postponed (transient), "
                 "%d failed, %d removed (brand rejected), %d brand-blocked (flagged), %d skipped (dead eBay link), "
                 "%d awaiting category (worklist), %d suspended-locked, %d no-price skipped, "
                 "%d protected (repair pending), %d creates paused",
                 onbuy_created, onbuy_updated, onbuy_deferred, onbuy_postponed, onbuy_failed, onbuy_removed,
                 onbuy_brand_blocked, onbuy_skipped_dead, onbuy_needs_category, onbuy_suspended_locked, onbuy_no_price,
                 onbuy_protected, onbuy_create_paused)
    if onbuy_halt_reason:
        logger.warning("OnBuy pushes were halted early this run: %s", onbuy_halt_reason)
    logger.info("Feed products: %d, skipped: %d", feed_count, skipped_feed)
    logger.info("Feed URL: %s", feed_url or "(not uploaded - see SUPABASE_URL/SUPABASE_SERVICE_KEY)")
    logger.info("Supabase database export: %s (%d rows)", "OK" if supabase_ok else "skipped/failed", len(supabase_rows))

    if fetch_failures >= FETCH_FAILURE_ALERT_THRESHOLD or onbuy_failed > 0 or onbuy_removed > 0 or onbuy_postponed > 0:
        notify.send_alert_email(
            "Sync run finished with errors" if (fetch_failures or onbuy_failed or onbuy_postponed) else "Sync run removed brand-rejected product(s)",
            f"eBay fetch failures: {fetch_failures}\n"
            f"OnBuy push failures: {onbuy_failed} (created {onbuy_created}, updated {onbuy_updated}, "
            f"deferred awaiting go-live {onbuy_deferred})\n"
            f"Products awaiting a category (fill the Category column; not counted as failures): {onbuy_needs_category}\n"
            f"OnBuy pushes postponed (rate limit/token/network - auto-retried next run): {onbuy_postponed}"
            + (f" - pushing halted early: {onbuy_halt_reason}" if onbuy_halt_reason else "") + "\n"
            f"Rows removed (brand owned by another seller): {onbuy_removed}"
            + (f" - SKUs: {', '.join(removed_skus)}" if removed_skus else "") + "\n"
            f"Updated rows: {updated_count}\n"
            f"Feed products: {feed_count}, skipped: {skipped_feed}\n"
            "Check the GitHub Actions run log for details.",
        )

    if run_had_errors:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Run crashed")
        notify.send_alert_email("Run crashed", "generate_xml.py raised an unhandled exception - see the GitHub Actions log.")
        raise
