"""Keepa answers handed from a lock-free PREFETCH job to the short, locked SYNC job (2026-10-05).

Keepa answers at 20 tokens a minute, so a full Amazon tab (1,500-2,000 ASINs) costs 30-90 minutes of WAITING. That wait sat
inside the onbuy-api lock - the whole Amazon sync was one locked job - and held the eBay sync, the backfill and the oversell
guard behind it for up to two hours; the OnBuy token fetched at the start was long expired by the first push as well.
run_amazon.yml now has two jobs: `prefetch` (no lock) runs generate_xml.py with KEEPA_PREFETCH_ONLY=1 - the same batch
selection, no OnBuy contact, no sheet writes - and saves the answers here; `sync` (locked) runs with KEEPA_CACHE=1, reads them
back and fetches live only what is missing.

The cache is ONE gzip JSON object per tab in a PRIVATE Supabase Storage bucket: this repository, its run logs and its artifacts are
public, and Keepa's answers carry Amazon prices (= supplier costs). Nothing here ever raises - any trouble means "no cache" and
the sync fetches live exactly as it always did.
"""
import gzip
import json
import logging
import os
import re
import zlib
from datetime import datetime, timezone

import requests

logger = logging.getLogger("onbuy_sync")

BUCKET = os.getenv("KEEPA_CACHE_BUCKET") or "keepa-cache"
FORMAT = 1
_TS = "%Y-%m-%dT%H:%M:%SZ"


def object_path(tab):
    return re.sub(r"[^a-z0-9]+", "_", str(tab or "tab").lower()).strip("_") + ".json.gz"


def _conn():
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
    return (url, key) if url and key else (None, None)


def _headers(key, extra=None):
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    headers.update(extra or {})
    return headers


def _stamp(moment):
    return moment.astimezone(timezone.utc).strftime(_TS)


def _parse(text):
    try:
        return datetime.strptime(str(text), _TS).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _upper(asins):
    return [str(a).strip().upper() for a in asins if str(a or "").strip()]


def usable(payload, run_id, now, max_age_hours, require_same_run):
    """Pure: may this cached payload be used now? The sync only trusts the cache of ITS OWN workflow run (require_same_run),
    so a failed prefetch never lets it silently use the previous cycle's data; a prefetch may reuse a young cache of an earlier
    attempt of the same cycle (a re-run) instead of spending the tokens twice."""
    if not isinstance(payload, dict) or payload.get("v") != FORMAT:
        return False
    if not isinstance(payload.get("products"), dict) or not isinstance(payload.get("asked"), list):
        return False
    done = _parse(payload.get("finished_at"))
    if done is None:
        return False
    age_hours = (now - done).total_seconds() / 3600.0
    if age_hours > max_age_hours or age_hours < -0.25:       # a little negative = clock skew between runners
        return False
    if require_same_run and (not run_id or str(payload.get("run_id")) != str(run_id)):
        return False
    return True


def split(asins, payload):
    """Pure: (answers already cached for these ASINs, the ASINs still to fetch live). An ASIN the cached fetch ASKED about but
    Keepa did not return is Keepa-unknown - a definitive answer, never asked again."""
    asked = set(_upper(payload.get("asked") or []))
    products = {str(k).upper(): v for k, v in (payload.get("products") or {}).items()}
    wanted = _upper(asins)
    have = {a: products[a] for a in wanted if a in products}
    need = [a for a in wanted if a not in asked]
    return have, need


def _ensure_bucket(url, key):
    try:
        resp = requests.post(f"{url}/storage/v1/bucket", headers=_headers(key, {"Content-Type": "application/json"}),
                             json={"id": BUCKET, "name": BUCKET, "public": False}, timeout=30)
        return resp.status_code in (200, 201, 400, 409)     # 400/409 = it already exists
    except requests.exceptions.RequestException:
        return False


def save(tab, products, asked, run_id, started_at, now=None):
    """Writes the cache object. Returns True when it is stored; never raises."""
    url, key = _conn()
    if not url:
        logger.warning("Keepa cache: SUPABASE_URL/SUPABASE_SERVICE_KEY not set - nothing cached")
        return False
    now = now or datetime.now(timezone.utc)
    payload = {"v": FORMAT, "tab": str(tab), "run_id": str(run_id or ""), "started_at": _stamp(started_at),
               "finished_at": _stamp(now), "asked": sorted(set(_upper(asked))),
               "products": {str(k).upper(): v for k, v in (products or {}).items()}}
    try:
        data = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"), compresslevel=6)
    except (TypeError, ValueError) as exc:
        logger.warning("Keepa cache: could not serialise the answers (%s) - nothing cached", exc)
        return False
    endpoint = f"{url}/storage/v1/object/{BUCKET}/{object_path(tab)}"
    headers = _headers(key, {"Content-Type": "application/gzip", "x-upsert": "true"})
    for attempt in (1, 2):
        try:
            resp = requests.post(endpoint, headers=headers, data=data, timeout=180)
        except requests.exceptions.RequestException as exc:
            logger.warning("Keepa cache: upload failed (%s) - nothing cached", str(exc)[:120])
            return False
        if resp.status_code in (200, 201):
            logger.info("Keepa cache: %d answer(s) for %d ASIN(s) stored (%d KB)", len(payload["products"]),
                        len(payload["asked"]), len(data) // 1024)
            return True
        if attempt == 1 and resp.status_code in (400, 404) and "bucket" in resp.text.lower():
            _ensure_bucket(url, key)                         # first ever run: create the private bucket, retry once
            continue
        logger.warning("Keepa cache: upload refused (HTTP %s) - nothing cached", resp.status_code)
        return False
    return False


def load(tab):
    """The cached payload, or None (no object, no configuration, any trouble). Never raises."""
    url, key = _conn()
    if not url:
        return None
    try:
        resp = requests.get(f"{url}/storage/v1/object/authenticated/{BUCKET}/{object_path(tab)}",
                            headers=_headers(key), timeout=180)
        if resp.status_code != 200:
            return None
        return json.loads(gzip.decompress(resp.content).decode("utf-8"))
    except (requests.exceptions.RequestException, OSError, EOFError, ValueError, zlib.error) as exc:
        logger.warning("Keepa cache: could not read the cache (%s) - fetching live", str(exc)[:120])
        return None


def answers(keepa, asins, tab, use_cache, prefetch_only, run_id, max_age_hours, reuse_hours, now=None):
    """The Keepa answers for these ASINs: from the cache where it holds them, live for the rest.
    -> (products, how many ASINs the cache answered, the cache payload that was used or None).
    Only a cache is ever skipped silently; a live fetch that fails raises what keepa.fetch_products raises."""
    now = now or datetime.now(timezone.utc)
    wanted = _upper(asins)
    have, need, payload = {}, list(wanted), None
    if use_cache or prefetch_only:
        loaded = load(tab)
        if usable(loaded, run_id, now, reuse_hours if prefetch_only else max_age_hours, require_same_run=not prefetch_only):
            payload = loaded
            have, need = split(wanted, payload)
    products = dict(have)
    if need:
        products.update(keepa.fetch_products(need))
    return products, len(wanted) - len(need), payload


def store(tab, products, asins, payload, run_id, started_at):
    """Saves a finished prefetch, merged with the cache it reused (a re-run of the same cycle). Never raises."""
    merged = {str(k).upper(): v for k, v in ((payload or {}).get("products") or {}).items()}
    merged.update({str(k).upper(): v for k, v in (products or {}).items()})
    asked = set(_upper(asins)) | set(_upper((payload or {}).get("asked") or []))
    return save(tab, merged, asked, run_id, started_at)
