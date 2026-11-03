"""The Amazon dispatcher (2026-11-02, user: "make it dynamic - the hourly Amazon runs of each account depend on the number of products
added"). Runs every hour as its own small workflow and answers one question - is this account's Amazon sync due? - from live facts: the
number of Amazon products on the sheet, this key's Keepa budget (read from Keepa itself, so a plan upgrade is picked up by itself) and the
time since the last planned sync. When it is due it launches run_amazon.yml with the batch size the plan wants (refresh_all=yes so the run
refreshes filled rows like a scheduled one, max_products=<slice>); it never launches into a running or waiting sync.

Added 2026-11-03 after the first night's logs: (1) KEY-MATES - two accounts share each Keepa key, and when GitHub delivered both ticks within
minutes they started together, fought over one bucket ("out of tokens") and YRA's run ran out of retries; the dispatcher now waits (up to
AMAZON_KEY_WAIT_MIN, default 150) while a key-mate's Amazon sync is running - KEEPA_MATE_REPOS lists them, the repositories are public so
their runs are readable - and AMAZON_DISPATCH_DELAY_S staggers the second account of a pair so it can see the first's launch; an account
that has waited more than two intervals plus two hours goes anyway. (2) CATCH-UP - GitHub delivers only a fraction of scheduled ticks, so
after a gap the batch grows to cover the real elapsed time (never beyond one run's cap). (3) A FAILED last sync is retried within 90 minutes.

Safety: repo variable AMAZON_DYNAMIC=0 means the old fixed cadence (every 4 hours, the workflow's own batch variable); any trouble while
planning (sheet or Keepa unreachable) falls back to that cadence for the tick - a bug here must never silence the syncs. One AMAZON_PLAN|
line goes to the log for the dashboard. Costs no Keepa tokens (GET /token is free) and writes nothing to the sheet."""
import json
import os
import sys
import time

import requests

import amazon_plan

API = "https://api.github.com"
WORKFLOW = "run_amazon.yml"
POLL_S = 180


def env_num(name, default, cast=float):
    raw = (os.getenv(name) or "").strip()
    try:
        return cast(raw) if raw else default
    except ValueError:
        return default


def gh(method, path, **kw):
    headers = {"Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    return requests.request(method, f"{API}{path}", headers=headers, timeout=30, **kw)


def recent_runs(repo):
    resp = gh("GET", f"/repos/{repo}/actions/workflows/{WORKFLOW}/runs", params={"per_page": 20})
    resp.raise_for_status()
    return [{k: x.get(k) for k in ("status", "conclusion", "event", "display_title", "created_at")}
            for x in resp.json().get("workflow_runs", [])]


def count_products():
    """Amazon-tab rows the sync can work on: a SKU and an Amazon link (rows waiting for either are left out, exactly as the sync leaves them)."""
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials

    import keepa_client
    from retry_utils import with_retry

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(os.environ["SHEET_NAME"]), what="sheet open", max_attempts=3)
    values = with_retry(book.worksheet((os.getenv("SHEET_TAB") or "Amazon").strip()).get_all_values, what="Amazon tab read", max_attempts=3)
    header = [str(h).strip() for h in values[0]]
    sku_i, url_i = header.index("SKU"), header.index("Supplier URL")
    n = 0
    for row in values[1:]:
        sku = str(row[sku_i]).strip() if sku_i < len(row) else ""
        url = str(row[url_i]).strip() if url_i < len(row) else ""
        if sku and url and keepa_client.parse_asin(url):
            n += 1
    return n


def keepa_tokens_per_day():
    """The key's real refill (tokens a minute x 1,440) from Keepa's free token endpoint; None when it cannot be read."""
    key = (os.getenv("KEEPA_API_KEY") or "").strip()
    if not key:
        return None
    try:
        rate = float(requests.get("https://api.keepa.com/token", params={"key": key}, timeout=20).json().get("refillRate") or 0)
    except Exception:                                              # the URL carries the key: never print the exception text
        return None
    return rate * 1440 if rate > 0 else None


def launch(repo, size):
    inputs = {"refresh_all": "yes", "source": "plan"}
    if size:
        inputs["max_products"] = str(int(size))
    resp = gh("POST", f"/repos/{repo}/actions/workflows/{WORKFLOW}/dispatches",
              json={"ref": os.getenv("GITHUB_REF_NAME") or "main", "inputs": inputs})
    return resp.status_code == 204, resp.status_code


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    force = (os.getenv("FORCE") or "").strip().lower() in ("yes", "1", "true")
    dynamic = (os.getenv("AMAZON_DYNAMIC") or "1").strip().lower() not in ("0", "no", "false", "off")
    mates = [m.strip() for m in (os.getenv("KEEPA_MATE_REPOS") or "").replace(";", ",").split(",") if m.strip()]

    runs = recent_runs(repo)
    info, note = None, ""
    if dynamic:
        try:
            tokens = keepa_tokens_per_day()
            info = amazon_plan.plan(
                count_products(), tokens or 28800, env_num("KEEPA_KEY_STORES", 2, int),
                env_num("AMAZON_MAX_CYCLES_PER_DAY", amazon_plan.CYCLES_MAX), env_num("AMAZON_RUN_SLICE_MAX", amazon_plan.SLICE_MAX, int),
                utilization=env_num("KEEPA_UTILIZATION_PERCENT", amazon_plan.UTILIZATION * 100.0) / 100.0)
            if not tokens:
                note = "Keepa refill unreadable - assumed 20 a minute"
        except Exception as exc:                                    # noqa: BLE001 - planning must never stop the syncs
            note = f"plan failed ({type(exc).__name__}) - using the old cadence this tick"
    else:
        note = "AMAZON_DYNAMIC is off - the old cadence"
    if info is None:
        info = {"runs": amazon_plan.LEGACY_RUNS, "interval_h": 24.0 / amazon_plan.LEGACY_RUNS, "slice": None, "limited_by": "legacy"}

    if info.get("runs") == 0:
        print(amazon_plan.line(info, False, "no Amazon products to sync yet", False, extra=note))
        return 0

    now = time.time()
    last = amazon_plan.last_planned(runs)
    due, reason = amazon_plan.decide(now, last, amazon_plan.is_busy(runs, now), info["interval_h"], force=force)

    blocker, waited = None, 0
    if due and not force and mates:
        delay = env_num("AMAZON_DISPATCH_DELAY_S", 0, int)
        if delay:
            time.sleep(delay)                                       # the second account of a pair lets the first one's launch become visible
        blocker = amazon_plan.mate_running(mates, recent_runs, time.time())
        if blocker and not amazon_plan.overdue(time.time(), last, info["interval_h"]):
            started, cap = time.time(), env_num("AMAZON_KEY_WAIT_MIN", 150, int) * 60
            while blocker and time.time() - started < cap:
                time.sleep(POLL_S)
                blocker = amazon_plan.mate_running(mates, recent_runs, time.time())
            waited = round((time.time() - started) / 60)
            runs = recent_runs(repo)                                # the world moved while we waited: our own runs may have changed
            now = time.time()
            last = amazon_plan.last_planned(runs)
            due, reason = amazon_plan.decide(now, last, amazon_plan.is_busy(runs, now), info["interval_h"])
            if blocker and due:
                due, reason = False, f"{reason} - but {blocker}'s sync kept the shared Keepa key busy for {waited} min"
        elif blocker:
            note = (note + "; " if note else "") + f"{blocker} is syncing on the same key, but this account is overdue - going anyway"
            blocker = None

    now = time.time()
    elapsed = None if last is None else (now - last["ts"]) / 3600.0
    size = amazon_plan.catch_up_slice(info, elapsed, env_num("AMAZON_RUN_SLICE_MAX", amazon_plan.SLICE_MAX, int)) if info.get("slice") else None
    launched = False
    if due:
        launched, status = launch(repo, size)
        if not launched:
            reason = f"{reason} - but GitHub refused the launch (HTTP {status})"
    print(amazon_plan.line(info, due, reason, launched, elapsed_h=elapsed, extra=note, slice_now=size, waited_min=waited, mate=blocker))
    for w in info.get("warnings") or []:
        print(f"WARNING {w}")
    return 1 if due and not launched else 0


if __name__ == "__main__":
    sys.exit(main())
