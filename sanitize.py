"""Description HTML cleaning and image URL validation.

The previous pipeline shipped raw scraped eBay HTML straight into the feed
(only whitespace-collapsed and length-truncated) and never checked image URLs
at all, despite both being documented requirements. This module actually
implements them.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import bleach
import requests

logger = logging.getLogger("onbuy_sync")

ALLOWED_TAGS = ["p", "ul", "ol", "li", "b", "strong", "i", "em", "br"]

# Heuristic patterns for eBay/seller boilerplate that survive tag-stripping
# because they're plain text, not markup. Not exhaustive - review real output
# periodically and extend this list as new noise shows up.
_NOISE_PATTERNS = [
    r"check\s*out\s*(my|our)\s*(other\s*)?(items|listings|ebay\s*store)",
    r"visit\s*(my|our)\s*ebay\s*(shop|store)",
    r"add\s*(me|us)\s*to\s*your\s*(favou?rite\s*)?sellers?",
    r"\d{1,3}\s*%\s*positive\s*feedback",
    r"please\s*leave\s*(us\s*|me\s*)?(a\s*)?(positive\s*)?feedback",
    r"we\s*strive\s*for\s*5\s*star",
    r"ebay\.(co\.uk|com)\S*",
    r"paypal\.\S*",
    r"https?://\S+",  # any bare URL left after tag-stripping is an external link
    # Leftover template title from this seller's bulk-listing tool - appears
    # verbatim at the start of many otherwise-unrelated listings' descriptions
    # (confirmed 2026-07-04: same fixed phrase prefixed onto ~9 different,
    # correctly-distinct product descriptions).
    r"3D\s*Optical\s*Illusion\s*Endless\s*Abyss\s*Floor\s*Mat\s*",
]
# A single (?i) at the start applies to every alternative - Python 3.11+
# rejects a repeated inline flag inside each alternative of a joined pattern.
_NOISE_RE = re.compile("(?i)" + "|".join(_NOISE_PATTERNS))

# ---- eBay seller-template furniture (Eselt etc.), 2026-08-01 ----
# These render as page decoration on eBay but arrive as TEXT in the
# listing data, so they leaked into OnBuy descriptions at scale. The
# final catch-all enforces the standing rule: no sentence mentioning
# eBay may survive into a description we publish elsewhere.
_TEMPLATE_JUNK_PATTERNS = [
    r"Created with Eselt[\w ,'&-]*",
    r"Mobile Templates? for eBay Sellers?",
    r"Send us a message",
    r"Our Store\b",
    r"Seller Profile",
    r"Check Our Feedback",
    r"Items for sale\b",
    r"About [Uu]s\b",
    r"Why Shop With Us",
    r"Add (?:us )?to Favou?rites?",
    r"Payment\s+Shipping\b",
    r"FREE Next working day shipping",
    r"Immediate Payment is required[^.!?]*[.!?]?",
    r"Many Payment Methods are accepted[^.!?]*[.!?]?",
    r"We allow 30 calendar days[^.!?]*[.!?]?",
    r"The customer is responsible for returning[^.!?]*[.!?]?",
    r"No P\.?O\.? Box Delivery",
    r"No APO/?FPO Delivery",
    r"Royal Mail Confirmed Addresses Only",
    r"Items? [Ww]ill be [Ss]hipped [Ss]ame or [Nn]ext [Bb]usiness day[^.!?]*[.!?]?",
    r"Shipping via Royal Mail[^.!?]*[.!?]?",
    r"RETURNS are Accepted for this Listing[^.!?]*[.!?]?",
    r"ONLY Returnable if[^.!?]*[.!?]?",
    r"This Item is Brand New",
    r"Actual Images of item are shown above",
    r"COPYRIGHT (?:\u00a9|\(c\))?[^.!?]*RESERVED[.!?]?",
    # catch-all LAST: any remaining sentence-ish chunk mentioning eBay
    r"[^.!?\n<>]*\beBay\b[^.!?\n<>]*[.!?]?",
]
_TEMPLATE_JUNK_RE = re.compile("(?i)" + "|".join(_TEMPLATE_JUNK_PATTERNS))

# ---- big-retailer description templates (Buy It Direct family etc.), ----
# 2026-08-20: whole shop navigation menus, carousel counters and brand-story
# sections arrive as text inside scraped descriptions (user-reported, kitchen
# tap example). Sentence junk here; the nav-menu bullet runs are handled
# structurally in sanitize_description because the item texts themselves
# ("Laptops", "Monitors") are too generic to blocklist safely.
_RETAIL_TEMPLATE_PATTERNS = [
    r"Your browser does not support the video tag\.?",
    r"\b\d+ of(?:\s+\d+){2,}",
    r"\bof(?:\s+\d+){3,}",          # carousel counters: "1 of 1 2 3 ..."
    r"Huge Discounts",
    r"Quality Products\s*Menu",
    r"Ask a question",
    r"Similar items",
    r"View more\b",
    r"View User Manual\s*\S{0,3}",
    r"Shop now\b",
    r"Shop [Aa]ll(?:\s+[A-Z&][\w&' -]{0,30})?",
    r"Browse All\b",
    r"Top Brands\b",
    r"New Arrivals\b",
    r"Ending Soon\b",
    r"\bShop similar items\b",
    r"We stock all the top brands",
    r"Want it sooner\??",
    r"SAFE\s*&(?:amp;)?\s*SECURE\s*SHOPPING",
    r"Recommended Accessories",
    r"RRP\s*£?[\d,.]+(?:\s*£[\d,.]+)?",
    r"About Buy It Direct[\s\S]{0,1200}?(?:something amazing\.?|$)",
    r"We built our business[^.!?]*[.!?]",
    r"[^.!?\n<>]*sister brands?[^.!?\n<>]*[.!?]?",
    r"[^.!?\n<>]*one of the UK'?s largest online retailers[^.!?\n<>]*[.!?]?",
    r"Come on, let'?s find something amazing\.?",
    r"Most products come with a manufacturer'?s warranty[^.!?]*[.!?]",
    r"They will arrange your repair or exchange[^.!?]*[.!?]",
    r"If something goes wrong\b",
    r"But, in the rare event that something gets lost or damaged[^.!?]*[.!?]",
    r"Please share as much information as possible[^.!?]*[.!?]",
    r"This does not include viruses, malware[^.!?]*[.!?]",
]
_RETAIL_TEMPLATE_RE = re.compile("(?i)" + "|".join(_RETAIL_TEMPLATE_PATTERNS))

# A run of MENU_RUN or more consecutive short list items - each at most four
# words, no digits, no colon - is a shop navigation menu, never a spec list
# (real feature bullets are long or carry digits/colons).
_LI_RE = re.compile(r"<li>\s*(.*?)\s*</li>", re.I | re.S)
_MENU_RUN = 5


def _strip_nav_menus(html):
    items = list(_LI_RE.finditer(html))
    if len(items) < _MENU_RUN:
        return html
    def is_navish(text):
        t = re.sub(r"<[^>]+>", " ", text).strip()
        return bool(t) and len(t.split()) <= 4 and not re.search(r"[\d:]", t)
    doomed = set()
    run = []
    for m in items:
        if is_navish(m.group(1)):
            run.append(m)
        else:
            if len(run) >= _MENU_RUN:
                doomed.update(id(x) for x in run)
            run = []
    if len(run) >= _MENU_RUN:
        doomed.update(id(x) for x in run)
    if not doomed:
        return html
    out, last = [], 0
    for m in items:
        if id(m) in doomed:
            out.append(html[last:m.start()])
            last = m.end()
    out.append(html[last:])
    return "".join(out)

# ---- spec-only policy (user rule 2026-08-01): descriptions may contain ----
# product specification and nothing else. Any sentence touching a seller
# topic (refunds/returns, delivery/shipping, payment, contact, feedback,
# store talk, templates, auctions) is deleted whole. Carve-outs keep real
# specs alive: "haptic/force/tactile feedback", "delivers <spec>" (only
# the nouns delivery/deliveries are seller-talk, the verb is not).
_SELLER_TOPICS = (
    r"refunds?|returns?|exchanges? accepted|delivery|deliveries|dispatch\w*|"
    r"shipping|shipped|courier\w*|postage|royal mail|parcel\s*force|evri|hermes|dpd|dhl|fedex|"
    r"tracking number|tracked|next working day|working days?|business days?|"
    r"payments?|paypal|checkout|invoice|"
    r"contact us|please contact|contact our|message us|email us|"
    r"customer (?:service|support|care)|(?<!haptic )(?<!force )(?<!tactile )feedback|"
    r"satisfaction|review us|our (?:store|shop)|visit (?:us|our)|subscribe|newsletter|"
    r"special offers?|great offers?|best price|price match|templates?|"
    r"your order|order (?:will|is|has|before)|auctions?|bidding|sellers?|buyers?|"
    r"vouchers?|coupons?|money.?back guarantee|satisfaction guaranteed?"
)
_SELLER_TOPIC_RE = re.compile(
    r"(?i)[^.!?\n<>]*\b(?:" + _SELLER_TOPICS + r")\b[^.!?\n<>]*[.!?]?")

# ---- shape rules (2026-09-05) ---------------------------------------------
# Everything above is a blocklist of wording already seen, and every new
# seller template walked straight past it: a sweep of GTV found 1,772 of
# 5,465 descriptions still carrying store menus, prices, returns policy and
# copyright footers (rows 5807-5845, one seller's template, were the report).
# These rules target the SHAPE of seller talk rather than its words. Policy
# (user, restated 2026-09-05): a description holds product content and
# nothing else - no prices, no store navigation, no shipping or returns
# terms, no links, no seller branding. A "sentence" here is a run between
# sentence punctuation, newlines or tags, same as the topic rule.
# A "." between two digits is a decimal point, not a full stop: "GBP 20.99"
# must stay one segment, or the money rule leaves a stray "99" behind.
_SEG = r"(?:[^.!?\n<>]|(?<=\d)\.(?=\d))*"

# 1. First-person seller voice. Specs are written in the third person; "we",
#    "our" and "us" are the seller talking about their shop. "us" must stay
#    lowercase-only: "US plug" and "US size" are specs.
_FIRST_PERSON_RE = re.compile(
    r"(?i)" + _SEG + r"\b(?:we|we're|we've|we'll|we'd|our|ours)\b" + _SEG + r"[.!?]?")
_FIRST_PERSON_US_RE = re.compile(_SEG + r"\bus\b" + _SEG + r"[.!?]?")

# 2. Second-person policy talk. "you can enjoy 10 hours of playback" is
#    marketing copy and stays; "your item will be returned to you" is a
#    returns policy and goes. The sentence has to carry BOTH a "you" and a
#    commerce word.
_POLICY_WORDS = (
    r"items?|orders?|goods|parcels?|purchases?|return\w*|refund\w*|receiv\w*|receie\w*|"
    r"contact|message|feedback|dispatch\w*|deliver(?:y|ies|ed)|ship(?:ping|ped|s)?|"
    r"payments?|pay|cancel\w*|claims?|faults?|faulty|packaging|seals?|sealed|unopened|"
    r"unused|deduction|exchanges?|invoice|tracking|courier|postage|"
    r"within \d+ (?:working |calendar |business )?(?:days?|hours?)"
)
_YOU = r"\b(?:you|your|you're|you've|you'll)\b"
_SECOND_PERSON_POLICY_RE = re.compile(
    r"(?i)(?:" + _SEG + _YOU + _SEG + r"\b(?:" + _POLICY_WORDS + r")\b" + _SEG +
    r"|" + _SEG + r"\b(?:" + _POLICY_WORDS + r")\b" + _SEG + _YOU + _SEG + r")[.!?]?")

# 3. Money. A price never belongs in a spec. "5.0GBPS" is a data rate, not
#    pounds, so GBP needs a word boundary on both sides.
_MONEY = (r"(?:(?:[£€$]|&pound;|&euro;|&#163;)\s?\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s?(?:£|€|\bGBP\b|\bUSD\b|\bEUR\b)|"
          r"\bGBP\s?\d|\d+\s?%\s?(?:off|deduction|discount)|save\s+[£€$])")
_MONEY_RE = re.compile(r"(?i)" + _SEG + _MONEY + _SEG + r"[.!?]?")

# 4. Footers and template branding.
_FOOTER_RE = re.compile(
    r"(?i)" + _SEG + r"(?:all rights reserved|©|&copy;|\(c\)\s*(?:19|20)\d\d|copyright|"
    r"powered by|designed by|template by|built with|created with|listing (?:template|designer))"
    + _SEG + r"[.!?]?")

# 5. The seller's story about themselves, in any person.
_STORY_RE = re.compile(
    r"(?i)" + _SEG + r"(?:competitive prices?|affordable prices?|latest in brand[- ]name|"
    r"located in|established (?:in|since)|family[- ]run|years of experience|"
    r"go[- ]to supplier|high street stores?|online retailers?|trusted seller|top[- ]rated|"
    r"leading (?:supplier|retailer|seller|provider)|(?:uk|europe)'?s? largest|"
    r"(?:happy|satisfied) (?:customers|clients)|shopping experience|do more business|"
    r"opportunity to resolve|they offer the latest|portfolio of brands|brands such as|"
    r"peace of mind|direct relationships|(?:our|their) customers)" + _SEG + r"[.!?]?")

# 5b. Returns / delivery policy prose in any voice. These are the words a
#     policy is made of and a spec is not: "unopened in the original retail
#     packaging", "subject to a deduction", "if the item develops a fault".
_POLICY_RE = re.compile(
    r"(?i)" + _SEG + r"(?:this policy|policy does not apply|returned|unopened|unused items?|"
    r"retail packaging|original packaging|manufacturers?'? seal|seal (?:still )?intact|"
    r"seal broken|tampered|subject to a|deduction|refund\w*|faulty|manufacturer fault|"
    r"develops a fault|incorrect item|wrong item|missing item|not received|"
    r"receie?ved (?:the|your|an)|changed your mind|goods (?:are|were|is) (?:found|returned)|"
    r"the goods|hygiene reasons|tried on|not exhaustive|calendar days|working days|"
    r"within \d+ days|damaged (?:in transit|on arrival)|proof of purchase|\bpaid for\b|"
    r"right to withdraw|items? back|accept(?:ed)? items|non-?returnable|completed orders|"
    r"orders? (?:placed|received|dispatched)|all goods|goods are|\(weekdays\)|opening hours|"
    r"responsibilit(?:y|ies)|liabilit(?:y|ies)|accidental(?:ly)? damaged?|customer damaged|"
    r"click\s*(?:&|&amp;|and)\s*collect|\bunfortunately\b|your chosen|hygiene|"
    r"\d{1,2}(?:[.:]\d{2})?\s*(?:am|pm)\b|"
    r"\d{1,2}(?:[.:]\d{2})?\s*(?:am|pm)\b\s*[-–]|"
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\s*[-–]\s*(?:mon|tue|wed|thu|fri|sat|sun))" + _SEG + r"[.!?]?")

# 5c. A short line that is just a name with a registered/trade mark on it is
#     the seller signing off, not a spec ("Total Digital Stores®.").
_TAGLINE_RE = re.compile(
    r"(?i)(?:^|(?<=[>.!?\n]))\s*(?:[A-Za-z&'\-]+\s+){0,6}[A-Za-z&'\-]+\s*(?:<[^>]+>\s*)*(?:®|™|&reg;|&trade;)\s*(?:<[^>]+>\s*)*[.!?]?")

# 6. Store navigation labels: tab strips ("PaymentShippingReturnsContact Us"
#    once the tags are gone), category menus and stock banners.
_NAV_WORDS = (r"Payment|Shipping|Delivery|Returns?|Refunds?|Contact\s*Us|About\s*Us|FAQs?|"
              r"Store\s*Home|Home\s*Page|Shop\s*(?:Categor(?:y|ies)|Now|All)|Store\s*Categor(?:y|ies)|"
              r"Other\s*Hot\s*Items?|Hot\s*Items?|Best\s*Sellers?|New\s*Arrivals|Featured\s*Products?|"
              r"UK\s*STOCK|In\s*Stock|Fast\s*(?:Delivery|Dispatch|Shipping)|"
              r"Free\s*(?:UK\s*)?(?:Delivery|Shipping|Postage|Returns)|Add\s*to\s*Favou?rites?")
_NAV_STRIP_RE = re.compile(r"(?:" + _NAV_WORDS + r"){2,}")        # concatenated tab labels
_NAV_LABEL_RE = re.compile(r"(?i)(?<![A-Za-z])(?:" + _NAV_WORDS + r")(?![A-Za-z])")

# 7. Cross-sell blocks sit at the bottom: from the heading onward it is other
#    products and their prices, never this product's content.
_CROSS_SELL_RE = re.compile(
    r"(?i)\b(?:other hot items?|you may (?:also )?like|similar (?:items|products)|"
    r"related (?:items|products)|customers (?:also|who) (?:bought|viewed|purchased)|"
    r"more (?:items|products) from|recommended (?:items|products|accessories)|"
    r"check out our|see also|frequently bought together)\b")

_RULER_RE = re.compile(r"(?:[_\-—–=~*]|&mdash;|&ndash;){4,}")
_EMPTYISH = r"(?:&nbsp;|&#160;|\s|[.,;:!?\-–—_*])*"


def _cut_cross_sell(text):
    """Truncate at the first cross-sell heading once past a third of the
    text; earlier than that it is more likely a stray label, so only the
    heading itself is dropped."""
    m = _CROSS_SELL_RE.search(text)
    if not m:
        return text
    if m.start() > len(text) * 0.3:
        return text[:m.start()]
    return text[:m.start()] + text[m.end():]



# bleach's strip=True unwraps disallowed tags but keeps their *text* content -
# fine for a stray <div>/<span>, but for <script>/<style> that would leak raw
# JS/CSS straight into the "sanitized" description. Delete these tags and
# everything inside them before bleach ever sees the markup.
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")


# OnBuy's listing pages don't render emojis (user report 2026-07-27) - they
# show as broken glyphs, so they are stripped from every fetched title and
# description at the source. Text symbols that DO render (TM, (R), (C),
# plain bullets) are deliberately kept. Blocks, by code point: pictographs/
# emoji/flags plane, misc symbols + dingbats, emoji stars and arrows, watch/
# hourglass, media controls, info sign, variation selectors, zero-width
# joiner, combining keycap, JP emoji marks.
_EMOJI_BLOCKS = (
    (0x1F000, 0x1FBFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF),
    (0x231A, 0x231B), (0x23E9, 0x23FA), (0x2139, 0x2139),
    (0xFE00, 0xFE0F), (0x200D, 0x200D), (0x20E3, 0x20E3),
    (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299),
)
_EMOJI_RE = re.compile(
    "[" + "".join(chr(a) + "-" + chr(b) if a != b else chr(a)
                  for a, b in _EMOJI_BLOCKS) + "]+")


def strip_emojis(text):
    out = _EMOJI_RE.sub("", str(text or ""))
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def sanitize_description(html, limit=45000):
    if not html:
        return ""

    html = str(html)
    html = _SCRIPT_STYLE_RE.sub("", html)

    # Keep only a small safe-formatting allowlist. strip=True drops disallowed
    # tags (div/span/font/a/img/script/style/...) but keeps their text content,
    # so paragraphs and lists survive while inline styling, scripts and links
    # to eBay/social/seller pages don't.
    cleaned = bleach.clean(html, tags=ALLOWED_TAGS, attributes={}, strip=True)

    # Remove seller-boilerplate sentences the tag-level cleaning can't catch
    # since they're plain text, not markup.
    # Non-breaking spaces arrive as entities and defeat every "\s" in the
    # rules below; they carry no meaning, so make them plain spaces first.
    cleaned = re.sub(r"(?:&nbsp;|&#160;|\xa0)", " ", cleaned)
    cleaned = _strip_nav_menus(cleaned)
    cleaned = _NOISE_RE.sub("", cleaned)
    cleaned = _RETAIL_TEMPLATE_RE.sub("", cleaned)
    # Topic rule FIRST so whole seller sentences die intact; the phrase
    # blocklist then only mops up non-sentence banner fragments.
    cleaned = _SELLER_TOPIC_RE.sub("", cleaned)
    cleaned = _TEMPLATE_JUNK_RE.sub("", cleaned)
    # Shape rules (see above): navigation strips and cross-sell tails first,
    # so their product names and prices never reach the sentence rules as
    # if they were this product's content; then money, footers, the
    # seller's story, and finally the voice rules.
    cleaned = _NAV_STRIP_RE.sub(" ", cleaned)
    cleaned = _cut_cross_sell(cleaned)
    cleaned = _MONEY_RE.sub("", cleaned)
    cleaned = _FOOTER_RE.sub("", cleaned)
    cleaned = _STORY_RE.sub("", cleaned)
    cleaned = _POLICY_RE.sub("", cleaned)
    cleaned = _TAGLINE_RE.sub("", cleaned)
    cleaned = _FIRST_PERSON_RE.sub("", cleaned)
    cleaned = _FIRST_PERSON_US_RE.sub("", cleaned)
    cleaned = _SECOND_PERSON_POLICY_RE.sub("", cleaned)
    cleaned = _NAV_LABEL_RE.sub(" ", cleaned)
    cleaned = _RULER_RE.sub(" ", cleaned)
    # Debris the sentence rules leave behind: runs of bare punctuation where
    # deleted sentences used to sit, and tags now holding only punctuation.
    cleaned = re.sub(r"(?:\s*[,;:\-–—]\s*){2,}", " ", cleaned)
    cleaned = re.sub(r"(?<=>)\s*[,;:\-–—.]+\s*(?=<)", "", cleaned)
    cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
    # Menu leftovers: one-word navigation bullets and the tags emptied by
    # the junk removal (run twice so lists emptied of items collapse too).
    cleaned = re.sub(r"(?i)<li>\s*(?:Feedback|Returns|Contact(?: Us)?|Our Store|Menu|Home|Shop)\s*</li>", "", cleaned)
    for _ in range(3):
        cleaned = re.sub(r"<(li|p|ul|ol|b|strong|i|em)>" + _EMPTYISH + r"</\1>", "", cleaned)
    cleaned = re.sub(r"(?:\s*<br\s*/?>\s*){2,}", "<br>", cleaned)
    cleaned = re.sub(r"(?:&nbsp;|&#160;)(?:\s*(?:&nbsp;|&#160;))+", " ", cleaned)
    cleaned = _EMOJI_RE.sub("", cleaned)

    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if len(cleaned) > limit:
        cleaned = cleaned[:limit]

    return cleaned


_IMAGE_CHECK_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; OnBuySyncBot/1.0; +https://onbuy.com)"}


def _check_image(url, timeout=4.0):
    """Return url if it resolves to a reachable HTTPS image, else None."""
    if not url or not url.lower().startswith("https://"):
        return None
    try:
        # Several image CDNs (observed on both Wikimedia and eBay's own) return
        # 403/405 for requests with no User-Agent, treating them as bots - always send one.
        resp = requests.head(url, timeout=timeout, allow_redirects=True, headers=_IMAGE_CHECK_HEADERS)
        if resp.status_code in (403, 405):  # some CDNs reject HEAD outright
            resp = requests.get(url, timeout=timeout, stream=True, headers={**_IMAGE_CHECK_HEADERS, "Range": "bytes=0-0"})
        content_type = resp.headers.get("Content-Type", "")
        if resp.status_code < 400 and content_type.startswith("image/"):
            return url
        logger.info("Rejected image (status=%s, content-type=%s): %s", resp.status_code, content_type, url)
    except requests.exceptions.RequestException as exc:
        logger.info("Rejected image (unreachable: %s): %s", exc, url)
    return None


def validate_images(urls, max_images=10, max_workers=8):
    """Validate a list of image URLs concurrently, preserving input order,
    keeping only HTTPS URLs that resolve to a real image, capped at max_images.
    """
    candidates = [u.strip() for u in urls if u and u.strip()][: max_images * 2]
    if not candidates:
        return []

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_check_image, url): url for url in candidates}
        for future in as_completed(futures):
            url = futures[future]
            results[url] = future.result()

    valid = [u for u in candidates if results.get(u)]
    return valid[:max_images]
