"""[Ported 2026-10-07 for remove_wrong_content.py, which uses title_similarity(); the adoption check below is NOT wired into this
repo's sync - it is only the shared name comparison here.]

A live OnBuy listing may only be ADOPTED by a sheet row when it is plausibly the same product (2026-10-07).

When a row that the pipeline has not created yet carries a SKU that is already a live listing, the sync adopts that listing: it sends
price and stock to it and marks the row Synced - it never touches the page content, because product content is only written when a
product is created. That is right for an old product entered without "Synced", and silently wrong when the SKU (a barcode) was
re-used for a DIFFERENT product: the row shows the new product, the OnBuy page keeps the old one, and the listing sells it.

Seen on Arden: SKU 2258075893340-Messam was created on 2026-07-04 as a Shark WandVac; about 2026-09-27 the same SKU appeared on a row
for a Roxel speaker. The sync adopted the live listing, the nightly scan flagged the mismatch every night and zeroed it, the sync (and
the old audit fix) put the stock back, and on 2026-10-07 a customer bought the Shark WandVac page. OnBuy cannot be asked to change the
page: re-submitting the Roxel content was answered "success" with no write permission (the barcode is bound to the Shark product).
A barcode is one product for ever - the Roxel row needs a new one.

adoption_conflict() is the check; it only compares what the listing is CALLED with what the row's title says, so it blocks clearly
different products (a quarter or less of the shorter name's words shared) and lets a re-worded title of the same product through.
"""
import re

# Below this share of the shorter name's words found in the other, the two names are different products. The nightly scan's
# "mismatch" line is 0.5; the guard is more lenient (a wrongly refused adoption stops a row, a wrongly allowed one is only what
# happens today, and the nightly scan still catches it). Real different products share almost nothing (0.0 for the Shark /
# Roxel pair); the same product re-worded keeps a third or more (0.33 for two Beldray titles).
MIN_SIMILARITY = 0.25
# Filler words every title carries; they must not make two different products look alike.
_FILLER = frozenset("a an and as at by for from in of on or the to with x".split())


def _tokens(text):
    return {t for t in re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split() if t not in _FILLER}


def title_similarity(a, b):
    """Share of the shorter name's words that the other name also holds (0..1; 0 when either has no words)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def adoption_conflict(listing_name, row_title, min_similarity=MIN_SIMILARITY):
    """'' when adopting is fine (or cannot be judged: either name missing); otherwise the sentence for the row's Sync Status."""
    listing_name, row_title = str(listing_name or "").strip(), str(row_title or "").strip()
    if not listing_name or not row_title:
        return ""
    if title_similarity(listing_name, row_title) >= min_similarity:
        return ""
    return (f"SKU already live on OnBuy as a different product ({listing_name[:70]}) - use a NEW SKU (barcode) for this product; "
            "an existing listing keeps its old page")
