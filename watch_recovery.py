"""Recovery watch (2026-09-04): 1,125 SKUs our records show as created
on the platform are refused by the seller API ("SKU does not exist") -
1,079 never became addressable, 46 synced once and then stopped. The full
list went to support on 4 September; their earlier recreations of the
same class came back within a day, so the rest should follow - the user
asked to be NOTIFIED when they do, without having to keep checking.

Every scheduled run asks check-winning about the whole baseline (one call
per 500 SKUs) and emails via the existing alert route when recovery
crosses a new milestone: 10%, 25%, 50%, 90%, and fully recovered. The
highest milestone already announced is committed back to the repo in
recovery_state.txt, so a stuck percentage never repeats an email, and a
fully-recovered state short-circuits future runs before any API call.

check-winning answering is the LEADING indicator - by-SKU updates can lag
it slightly - so the email says prices flow as the regular syncs touch the
rows, not that everything is already Synced. No sheet access, no writes
anywhere except the state file in git.
"""
import csv
import io
import os
import time

import notify
from onbuy_client import OnBuyClient
from retry_utils import PermanentError, RateLimitError

BASELINE = "recovery_baseline.csv"
STATE = "recovery_state.txt"
CHECK_BATCH = int(os.getenv("CHECK_BATCH") or "500")
MILESTONES = (10, 25, 50, 90, 100)


def read_state():
    try:
        return int(io.open(STATE, encoding="utf-8").read().strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0


def main():
    rows = list(csv.DictReader(io.open(BASELINE, encoding="utf-8-sig")))
    baseline = [r["sku"].strip() for r in rows if r["sku"].strip()]
    notified = read_state()
    print(f"baseline: {len(baseline)} SKU(s) | highest milestone announced: {notified}%")
    if notified >= 100:
        print("already fully recovered and announced - nothing to do")
        return

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")

    refused, served = set(), set()
    queue = [baseline[i:i + CHECK_BATCH] for i in range(0, len(baseline), CHECK_BATCH)]
    while queue:
        chunk = queue.pop(0)
        try:
            res = onbuy.check_winning(chunk) or []
        except RateLimitError:
            print("burst limit - waiting 90s")
            time.sleep(90)
            queue.insert(0, chunk)
            continue
        except PermanentError as exc:
            if len(chunk) > 10:
                half = len(chunk) // 2
                queue.insert(0, chunk[half:])
                queue.insert(0, chunk[:half])
                continue
            print(f"check-winning failed for {len(chunk)} SKU(s): {str(exc)[:120]}")
            refused.update(chunk)
            continue
        answered = set()
        for item in res:
            item = item or {}
            sku = str(item.get("sku") or "").strip()
            if not sku:
                continue
            answered.add(sku)
            (refused if str(item.get("error") or "").strip() else served).add(sku)
        refused.update(s for s in chunk if s not in answered)
        time.sleep(1.0)

    recovered = len(baseline) - len(refused)
    pct = round(100 * recovered / len(baseline))
    bucket = max((m for m in MILESTONES if pct >= m), default=0)
    if not refused:
        bucket = 100
    print(f"RECOVERY {recovered}/{len(baseline)} ({pct}%) | remaining refused: {len(refused)} "
          f"| bucket {bucket} | announced {notified}")

    if bucket <= notified:
        print("no new milestone - no email")
        return

    remaining_sample = ", ".join(sorted(refused)[:10])
    if bucket >= 100:
        subject = f"All {len(baseline)} never-live SKUs have recovered"
        body = (
            f"Every one of the {len(baseline)} SKUs reported to OnBuy on 4 September is "
            "answering on the seller API again.\n\n"
            "Nothing to do: the regular syncs push price and stock as they touch each row, "
            "and rows flip to Synced as those land. The recovery watch stands down now - "
            "this is its final email.\n"
        )
    else:
        subject = f"Unaddressable SKUs recovering: {recovered} of {len(baseline)} ({pct}%)"
        body = (
            f"OnBuy's fix is rolling out. {recovered} of the {len(baseline)} SKUs reported on "
            f"4 September now answer on the seller API; {len(refused)} still return "
            "\"SKU does not exist\".\n\n"
            f"Still refused (first 10): {remaining_sample}\n\n"
            "No action needed: prices and stock flow automatically as the regular syncs touch "
            "the recovered rows. The watch checks every 6 hours and emails again at the next "
            "milestone (10/25/50/90/100%).\n"
        )
    notify.send_alert_email(subject, body)
    print(f"MILESTONE EMAIL SENT: {subject}")
    io.open(STATE, "w", encoding="utf-8", newline="").write(str(bucket) + "\n")
    print(f"state advanced to {bucket}")


if __name__ == "__main__":
    main()
