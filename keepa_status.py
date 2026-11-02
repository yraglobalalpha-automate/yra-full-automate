"""READ-ONLY and FREE: which Keepa key does this repository hold, and what does Keepa say about it?

1. KEY FINGERPRINT - the first 8 hex characters of SHA-256 of the KEEPA_API_KEY secret. A one-way hash of a long random key
   cannot be turned back into the key; two repositories holding the SAME key print the same 8 characters, different keys
   differ. GitHub never shows a secret's value, so this is how "which repositories share a key" is answered exactly - at
   rest or under load, no timing needed.
2. KEEPA'S OWN BUCKET - GET https://api.keepa.com/token costs no tokens: tokens left and the refill rate, three readings
   20 seconds apart. Repositories on one key move together; keys on separate buckets drift apart. (Keepa ticks every key on
   one global minute clock, so the clock itself is NOT a fingerprint, and two idle full buckets both read 1,200 - the hash
   is what settles it.)

Nothing is written anywhere and the key itself is never printed."""
import hashlib
import json
import os
import sys
import time

import requests

key = (os.getenv("KEEPA_API_KEY") or "").strip()
if not key:
    sys.exit("KEEPA_API_KEY is not set in this repository")

print(f"KEEPA_KEY_FINGERPRINT|sha256_first8={hashlib.sha256(key.encode()).hexdigest()[:8]}")

for n in range(3):
    if n:
        time.sleep(20)
    try:
        resp = requests.get("https://api.keepa.com/token", params={"key": key}, timeout=30)
        body = resp.json()
    except Exception as exc:                                       # the URL carries the key: never print the exception text
        print(f"READING {n + 1}: request failed ({type(exc).__name__})")
        continue
    left, rate, refill_in = body.get("tokensLeft"), body.get("refillRate"), body.get("refillIn")
    print(f"KEEPA_READING|n={n + 1}|http={resp.status_code}|tokens_left={left}|refill_rate={rate}|refill_in_ms={refill_in}")
    extra = {k: v for k, v in body.items() if k not in ("tokensLeft", "refillRate", "refillIn", "timestamp")}
    if extra and n == 0:
        print("KEEPA_OTHER|" + json.dumps(extra, default=str).replace(key, "***")[:300])
