"""READ-ONLY and FREE: Keepa's own account of the key this repository holds.

GET https://api.keepa.com/token costs no tokens. It answers how many tokens the key holds now, its refill rate and the
milliseconds until the next refill - the refill CLOCK belongs to the key (the account), so two repositories whose
KEEPA_API_KEY secret is the SAME key report the same clock (server time + refillIn modulo one minute stays constant) and
the same bucket, while different keys keep different clocks. GitHub never shows a secret's value, so this is how a
repository's key can be told apart from another's without ever printing either one.

Three readings, 20 seconds apart: the clock must repeat for the fingerprint to count. Nothing is written anywhere."""
import json
import os
import sys
import time

import requests

key = (os.getenv("KEEPA_API_KEY") or "").strip()
if not key:
    sys.exit("KEEPA_API_KEY is not set in this repository")

for n in range(3):
    if n:
        time.sleep(20)
    try:
        resp = requests.get("https://api.keepa.com/token", params={"key": key}, timeout=30)
        body = resp.json()
    except Exception as exc:                                       # the URL carries the key: never print the exception text
        print(f"READING {n + 1}: request failed ({type(exc).__name__})")
        continue
    left, rate, refill_in, stamp = body.get("tokensLeft"), body.get("refillRate"), body.get("refillIn"), body.get("timestamp")
    phase = (stamp + refill_in) % 60000 if isinstance(stamp, (int, float)) and isinstance(refill_in, (int, float)) else None
    print(f"KEEPA_READING|n={n + 1}|http={resp.status_code}|tokens_left={left}|refill_rate={rate}|refill_in_ms={refill_in}|server_ms={stamp}|clock_phase_ms={phase}")
    extra = {k: v for k, v in body.items() if k not in ("tokensLeft", "refillRate", "refillIn", "timestamp")}
    if extra and n == 0:
        print("KEEPA_OTHER|" + json.dumps(extra, default=str).replace(key, "***")[:300])
