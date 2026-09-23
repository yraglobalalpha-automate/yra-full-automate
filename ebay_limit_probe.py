"""Daily probe of this application's eBay API rate limits.

The Application Growth Check was filed on 2026-09-23 to raise the Buy
Browse daily limit past the default 5,000. eBay does not notify by
email reliably, but the Developer Analytics API (getRateLimits) reports
the live limits for our own keyset - so this probe runs daily, prints
every buy-context limit, and sends an alert email the moment the Browse
daily limit differs from EBAY_LIMIT_BASELINE (repo variable, default
5000). After an approval: raise EBAY_DAILY_CALL_BUDGET to ~96% of the
new limit and set EBAY_LIMIT_BASELINE to the new value to re-arm the
alert for future changes.
"""
import os

import requests

from generate_xml import get_ebay_token

BASELINE = int(os.getenv("EBAY_LIMIT_BASELINE") or "5000")


def main():
    token = get_ebay_token()
    resp = requests.get(
        "https://api.ebay.com/developer/analytics/v1_beta/rate_limit/",
        headers={"Authorization": f"Bearer {token}"},
        params={"api_context": "buy", "api_name": "browse"},
        timeout=30,
    )
    resp.raise_for_status()
    browse_daily = None
    for rl in resp.json().get("rateLimits", []):
        ctx, api = rl.get("apiContext"), rl.get("apiName")
        for res in rl.get("resources", []):
            for rate in res.get("rates", []):
                window = rate.get("timeWindow")
                line = (f"{ctx}/{api}/{res.get('name')}: limit {rate.get('limit')} "
                        f"remaining {rate.get('remaining')} window {window}s reset {rate.get('reset')}")
                print(line)
                if window == 86400 and str(api).lower() == "browse" and browse_daily is None:
                    browse_daily = int(rate.get("limit") or 0)
    if browse_daily is None:
        print("Browse daily limit not found in the answer - nothing to compare")
        return
    print(f"BROWSE DAILY LIMIT: {browse_daily} (baseline {BASELINE})")
    if browse_daily != BASELINE:
        print("LIMIT CHANGED - alerting")
        try:
            import notify
            notify.send_alert_email(
                f"eBay call limit changed: {browse_daily}/day",
                f"The Browse API daily limit for this application is now {browse_daily} "
                f"(baseline {BASELINE}). If this is the Growth Check approval: raise the "
                "EBAY_DAILY_CALL_BUDGET repo variable to about 96% of the new limit and "
                "set EBAY_LIMIT_BASELINE to the new value.")
        except Exception as exc:  # noqa: BLE001
            print(f"alert email failed: {exc}")


if __name__ == "__main__":
    main()
