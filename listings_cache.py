"""Shared one-sweep /listings cache for multi-step jobs (nightly reconcile).

The nightly job's five steps (scan x2, zero x2, audit) each swept the full
account-wide /listings list - five identical fetches inside one hour. On
Arden (~15k live listings with the legacy KEEP catalog) that is ~750 GET
calls against OnBuy's 240/hour quota, so the later steps died on 429 no
matter how patiently each page waited (nightlies 2026-09-23/24). Steps of
one job now share a single sweep through this file cache: the first step
fetches and saves, the rest load.

Set LISTINGS_CACHE to a file path to enable (the nightly workflow does);
unset, every script fetches fresh exactly as before. A cache older than
LISTINGS_CACHE_MAX_AGE seconds (default 5400) is ignored, so a leftover
file can never feed stale data to a later job. Sharing one snapshot is
consistent because every step pushes sheet-truth: a later step at worst
re-pushes the same sheet value an earlier step already set.
"""
import json
import os
import time


def load():
    path = (os.getenv("LISTINGS_CACHE") or "").strip()
    if not path or not os.path.exists(path):
        return None
    age = time.time() - os.path.getmtime(path)
    max_age = int(os.getenv("LISTINGS_CACHE_MAX_AGE") or "5400")
    if age > max_age:
        print(f"listings cache: {path} is {int(age)}s old (max {max_age}) - fetching fresh")
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            items = json.load(fh)
    except Exception as exc:  # noqa: BLE001 - unreadable cache = fetch fresh
        print(f"listings cache: could not read {path} ({exc}) - fetching fresh")
        return None
    if not isinstance(items, list):
        return None
    print(f"listings cache: reusing {len(items)} item(s) from {path} ({int(age)}s old)")
    return items


def save(items):
    path = (os.getenv("LISTINGS_CACHE") or "").strip()
    if not path:
        return
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh)
    os.replace(tmp, path)
    print(f"listings cache: wrote {len(items)} item(s) to {path}")
