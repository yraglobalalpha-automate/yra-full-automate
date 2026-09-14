"""Amazon price, stock and content for the Amazon tab, through the Keepa API.

Amazon offers no supplier API (SP-API is bound to a seller account, the
Product Advertising API is affiliate-only and was retired in May 2026), so
the Amazon tab is fed by Keepa - a paid data service tracking amazon.co.uk.
One product request answers up to 100 ASINs for one token each with the
current Amazon price, the lowest third-party New price, Amazon's own
availability, barcodes, images, bullet points and the category tree, and
refreshes its copy first when it is older than about an hour. Every
response carries the token bucket (tokensLeft / refillIn / refillRate);
this client paces itself on that instead of failing on 429.
Docs: https://keepa.com/api-docs/product.html

get_amazon_data() returns the same (available, data) shape as
generate_xml.get_ebay_data() (see empty_ebay_response there) plus a few
Amazon-only keys, so the row loop downstream stays supplier-agnostic.
"""
import logging
import math
import os
import re
import time
from datetime import datetime, timezone

import requests

from retry_utils import PermanentError, TransientError, with_retry
from sanitize import sanitize_description, strip_emojis, validate_images

logger = logging.getLogger("onbuy_sync")

BASE_URL = "https://api.keepa.com"
DOMAIN_UK = 2                       # amazon.co.uk
KEEPA_EPOCH_OFFSET_MIN = 21564000   # Keepa time (minutes) -> Unix minutes
IMAGE_BASE = "https://m.media-amazon.com/images/I/"
BATCH = 100                         # ASINs per product request
DEFAULT_STOCK = int(os.getenv("AMAZON_DEFAULT_STOCK") or "5")
# +2 tokens per product: the Buy Box price including shipping and who holds
# it. Off by default - Amazon's own price plus the lowest New offer cover
# most rows for the base 1 token.
USE_BUYBOX = (os.getenv("KEEPA_BUYBOX") or "").strip().lower() in ("1", "yes", "true")
MAX_WAIT_FOR_TOKENS = int(os.getenv("KEEPA_MAX_WAIT_SECONDS") or "600")
# Keepa's "update" parameter: refresh the product from Amazon if its copy is
# older than this many hours. The documented default (1) still handed the
# first probe a 22-hour-old product (2026-09-09), so the default here is 0 =
# always fetch live - which costs 1 extra token only when Keepa's copy is
# under an hour old, rare on a 2-hourly cadence. "off" leaves the parameter
# out (Keepa's own 1-hour rule); a blank repo variable means the default.
UPDATE_HOURS = (os.getenv("KEEPA_UPDATE_HOURS") or "0").strip().lower()
if UPDATE_HOURS in ("off", "none"):
    UPDATE_HOURS = ""

# stats.current / csv price-type indexes (Keepa "Price Type indexing")
IDX_AMAZON, IDX_NEW, IDX_COUNT_NEW, IDX_BUY_BOX_SHIPPING = 0, 1, 11, 18

# availabilityAmazon codes (the "sold by Amazon" offer only)
AVAILABILITY_TEXT = {
    -1: "No Amazon offer",
    0: "In stock (Amazon)",
    1: "Pre-order",
    2: "Unknown",
    3: "Back-order",
    4: "Delayed",
}

_ASIN_IN_URL_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})(?=[/?#]|$)", re.I)
_BARE_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$", re.I)


def parse_asin(url):
    """The ASIN in an amazon.* link (/dp/ASIN, /gp/product/ASIN) or a bare
    ASIN, upper-cased; "" when there is none."""
    s = str(url or "").strip()
    if _BARE_ASIN_RE.match(s):
        return s.upper()
    m = _ASIN_IN_URL_RE.search(s)
    return m.group(1).upper() if m else ""


def keepa_time_to_iso(minutes):
    """Keepa timestamps are minutes since an offset epoch."""
    try:
        seconds = (int(minutes) + KEEPA_EPOCH_OFFSET_MIN) * 60
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def empty_amazon_response(asin=""):
    """Same keys as generate_xml.empty_ebay_response(), plus the Amazon-only
    ones the tab's extra columns are written from."""
    return {
        "stock": 0,
        "price": 0,
        "description": "",
        "main_image": "",
        "additional_images": [],
        "title": "",
        "brand": "",
        "product_code": "",
        "condition": "New",
        "product_type": "",
        "asin": asin,
        "amazon_seller": "",
        "amazon_availability": "",
        "keepa_updated": "",
        "eans": [],
        "category_path": "",
        "amazon_type": "",
    }


def _price(current, idx):
    """A stats.current entry as a positive integer (pence), else -1."""
    try:
        value = int(current[idx])
    except (IndexError, TypeError, ValueError):
        return -1
    return value if value > 0 else -1


def choose_offer(product, use_buybox=None):
    """(price_pence, seller, availability_text, reason) for the offer we would
    actually buy from; price -1 when nothing is buyable right now.

    Preference: Amazon's own offer when it is in stock; then, with
    KEEPA_BUYBOX, a shippable IN_STOCK Buy Box (price including shipping);
    then the lowest third-party New offer. Pre-order, back-order, delayed
    and "unknown" Amazon offers do not count as in stock - overselling is
    the costlier mistake, the same rule the eBay fetch applies.
    """
    use_buybox = USE_BUYBOX if use_buybox is None else use_buybox
    ptype = product.get("productType")
    if ptype == 4:
        return -1, "", "Invalid or removed ASIN", "dead"
    if ptype in (1, 2, 3):
        return -1, "", "No price data for this product type", "no-price-data"
    stats = product.get("stats") or {}
    current = stats.get("current") or []
    avail = product.get("availabilityAmazon")
    avail = avail if isinstance(avail, int) else -1

    amazon_price = _price(current, IDX_AMAZON)
    if avail == 0 and amazon_price > 0:
        return amazon_price, "Amazon", AVAILABILITY_TEXT[0], "amazon"

    if use_buybox:
        bb_price = stats.get("buyBoxPrice")
        bb_ok = (isinstance(bb_price, int) and bb_price > 0
                 and stats.get("buyBoxIsShippable") is not False
                 and not stats.get("buyBoxIsPreorder")
                 and not stats.get("buyBoxIsBackorder")
                 and not stats.get("buyBoxIsUnqualified")
                 and stats.get("buyBoxAvailabilityMessage") in (None, "IN_STOCK"))
        if bb_ok:
            ship = stats.get("buyBoxShipping")
            price = bb_price + (ship if isinstance(ship, int) and ship > 0 else 0)
            seller = "Amazon" if stats.get("buyBoxIsAmazon") else "3rd party (Buy Box)"
            return price, seller, "In stock (Buy Box)", "buybox"

    new_price = _price(current, IDX_NEW)
    if new_price > 0:
        return new_price, "3rd party (lowest New)", "In stock (3rd party)", "new"
    return -1, "", AVAILABILITY_TEXT.get(avail, "Unknown"), "unavailable"


def _image_urls(product):
    urls = []
    for img in product.get("images") or []:
        if isinstance(img, dict):
            name = img.get("l") or img.get("m")
        else:
            name = img
        if name:
            urls.append(IMAGE_BASE + str(name).strip())
    if not urls and product.get("imagesCSV"):
        urls = [IMAGE_BASE + n.strip() for n in str(product["imagesCSV"]).split(",") if n.strip()]
    return urls


def _description_html(product):
    """Bullet points first (the spec-like "About this item" list), then the
    description; both through the same sanitizer as eBay text."""
    parts = []
    features = [str(f).strip() for f in (product.get("features") or []) if str(f or "").strip()]
    if features:
        parts.append("<ul>" + "".join(f"<li>{f}</li>" for f in features) + "</ul>")
    description = str(product.get("description") or "").strip()
    if description:
        parts.append("<p>" + description.replace("\n", "<br>") + "</p>")
    return sanitize_description("".join(parts))


def barcodes(product):
    """Every EAN/UPC/GTIN Keepa lists for the product, digits only, in order."""
    codes = []
    for key in ("eanList", "upcList", "gtinList"):
        for code in product.get(key) or []:
            digits = re.sub(r"\D", "", str(code))
            if digits and digits not in codes:
                codes.append(digits)
    return codes


def _category_names(product):
    names = [str((c or {}).get("name") or "").strip() for c in (product.get("categoryTree") or [])]
    return [n for n in names if n]


def normalize_product(product, asin="", use_buybox=None):
    """(available, data) in get_ebay_data()'s shape for one Keepa product."""
    asin = asin or str(product.get("asin") or "").upper()
    price, seller, availability, _reason = choose_offer(product, use_buybox)
    data = empty_amazon_response(asin)
    names = _category_names(product)
    data.update({
        "amazon_seller": seller,
        "amazon_availability": availability,
        "keepa_updated": keepa_time_to_iso(product.get("lastUpdate")),
        "eans": barcodes(product),
        "category_path": " > ".join(names),
        "product_type": names[-1] if names else str(product.get("type") or "").strip(),
        # Amazon's own product-type code ("MONITOR", "TELEVISION"): the key
        # of amazon_type_categories.csv, which decides the OnBuy category
        # before any word matching runs.
        "amazon_type": str(product.get("type") or "").strip().upper(),
    })
    if price <= 0:
        return False, data
    images = validate_images(_image_urls(product), max_images=11)
    data.update({
        "stock": DEFAULT_STOCK,
        "price": round(price / 100.0, 2),
        "description": _description_html(product),
        "main_image": images[0] if images else "",
        "additional_images": images[1:11],
        "title": strip_emojis(product.get("title") or ""),
        "brand": str(product.get("brand") or product.get("manufacturer") or "").strip(),
        "product_code": data["eans"][0] if data["eans"] else "",
    })
    return True, data


def get_amazon_data(asin, products):
    """Like get_ebay_data(): (available, data). An ASIN Keepa does not know
    is a definitive "not available" (nothing to build a listing from), the
    same signal as an eBay 404 - not a fetch failure."""
    product = products.get(asin) if asin else None
    if not product:
        logger.info("NOT IN KEEPA: %s", asin or "(no ASIN in the link)")
        data = empty_amazon_response(asin)
        data["amazon_availability"] = "Not found on Amazon/Keepa"
        return False, data
    available, data = normalize_product(product, asin)
    if not available:
        logger.info("UNAVAILABLE: %s (%s)", asin, data["amazon_availability"])
    return available, data


class KeepaClient:
    def __init__(self, api_key, domain=DOMAIN_UK, use_buybox=None, stats_days=90):
        if not api_key:
            raise PermanentError("KEEPA_API_KEY is not set")
        self.api_key = api_key
        self.domain = domain
        self.use_buybox = USE_BUYBOX if use_buybox is None else use_buybox
        self.stats_days = stats_days
        self.tokens_left = None
        self.refill_rate = None
        self.refill_in_ms = None
        self.tokens_consumed = 0

    @classmethod
    def from_env(cls):
        return cls(os.getenv("KEEPA_API_KEY") or "")

    def fetch_products(self, asins):
        """ASIN -> Keepa product object for every ASIN Keepa knows, fetched
        100 per call. Unknown ASINs are simply absent from the result."""
        wanted = list(dict.fromkeys(str(a).strip().upper() for a in asins if str(a or "").strip()))
        out = {}
        for start in range(0, len(wanted), BATCH):
            chunk = wanted[start:start + BATCH]
            # Worst case per product: 1, +2 with Buy Box detail, +1 when a live
            # refresh is forced on a copy under an hour old.
            self._wait_for_tokens(len(chunk) * ((3 if self.use_buybox else 1) + (1 if UPDATE_HOURS == "0" else 0)))
            params = {
                "key": self.api_key,
                "domain": self.domain,
                "asin": ",".join(chunk),
                "stats": self.stats_days,
                "history": 0,
            }
            if self.use_buybox:
                params["buybox"] = 1
            if UPDATE_HOURS:
                params["update"] = int(UPDATE_HOURS)
            body = self._request("product", params)
            for product in body.get("products") or []:
                if isinstance(product, dict) and product.get("asin"):
                    out[str(product["asin"]).upper()] = product
        return out

    def _wait_for_tokens(self, needed):
        """The bucket refills every minute at refillRate; sleep out a
        shortfall rather than burn a 429 on it."""
        if self.tokens_left is None or self.tokens_left >= needed:
            return
        rate = self.refill_rate or 1
        shortfall = needed - self.tokens_left
        wait = min(MAX_WAIT_FOR_TOKENS, math.ceil(shortfall / rate) * 60 + 2)
        logger.info("Keepa: %s token(s) left, %d needed - waiting %ds for the refill (%s/min)",
                    self.tokens_left, needed, wait, rate)
        time.sleep(wait)

    def _note_tokens(self, body):
        if not isinstance(body, dict) or "tokensLeft" not in body:
            return
        self.tokens_left = body.get("tokensLeft")
        self.refill_rate = body.get("refillRate")
        self.refill_in_ms = body.get("refillIn")
        consumed = int(body.get("tokensConsumed") or 0)
        self.tokens_consumed += consumed
        logger.info("Keepa: %d token(s) used, %s left, refill %s/min", consumed, self.tokens_left, self.refill_rate)

    def _request(self, endpoint, params):
        what = f"keepa {endpoint}"

        def _do():
            # Never log params: they carry the API key.
            resp = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=90)
            try:
                body = resp.json()
            except ValueError:
                body = {}
            self._note_tokens(body)
            if resp.status_code == 429:
                wait = min(MAX_WAIT_FOR_TOKENS, int((body.get("refillIn") or 60000) / 1000) + 2)
                logger.warning("%s: out of tokens - waiting %ds for the refill", what, wait)
                time.sleep(wait)
                raise TransientError(f"{what}: out of tokens")
            if resp.status_code >= 500:
                raise TransientError(f"{what}: server error {resp.status_code}")
            if resp.status_code != 200:
                err = body.get("error") if isinstance(body.get("error"), dict) else {}
                raise PermanentError(
                    f"{what}: HTTP {resp.status_code} {err.get('type') or ''} "
                    f"{err.get('message') or resp.text[:200]}".strip())
            return body

        return with_retry(_do, what=what, max_attempts=4)
