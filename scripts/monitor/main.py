"""CLI entrypoint for the genetics-results-suite monitoring system.

Orchestrates health checks, BigQuery summary, and log alerting,
then formats results for Slack and stdout.

Usage:
    python -m monitor.main --all
    python -m monitor.main --health --bq-summary
"""

import argparse
import logging
import os
import sys

from monitor.slack import send_slack_message

logger = logging.getLogger("monitor")


# ---------------------------------------------------------------------------
# Slack block formatting helpers
# ---------------------------------------------------------------------------

_PROFILE_FLAGS = {"finngen": "\U0001f1eb\U0001f1ee", "daly": "\U0001f1fa\U0001f1f8"}


def _flag() -> str:
    profile = os.environ.get("CONFIG_PROFILE", "")
    return _PROFILE_FLAGS.get(profile, "")


def _format_health_blocks(results: list) -> list[dict]:
    """Format CheckResult list into Slack blocks."""
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{_flag()} Health"}},
    ]

    # group by service
    by_service: dict[str, list] = {}
    for r in results:
        by_service.setdefault(r.service, []).append(r)

    for service, checks in by_service.items():
        failed = [c for c in checks if c.status == "fail"]
        all_ok = len(failed) == 0
        icon = ":large_green_circle:" if all_ok else ":red_circle:"

        lines = [f"{icon} *{service}* — {len(checks)} checks, {len(failed)} failed"]
        for c in failed[:15]:
            lines.append(f"    :red_circle: `{c.check}` — {c.error or 'unknown error'}")
        if len(failed) > 15:
            lines.append(f"    _...and {len(failed) - 15} more_")

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        })

    return blocks


def _format_bq_blocks(results: list[dict]) -> list[dict]:
    """Format BigQuerySummary results (list of dicts) into Slack blocks."""
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{_flag()} BigQuery"}},
    ]

    lines = []
    for v in results:
        missing = v.get("missing_resources", [])
        status = ":red_circle:" if missing or v.get("error") else ":large_green_circle:"
        row_count = v.get("row_count")
        rc = f"{row_count:,}" if row_count is not None else "N/A"
        res_count = v.get("resource_count")
        res = str(res_count) if res_count is not None else "N/A"
        line = f"{status} *{v['view']}* — {rc} rows, {res} resources"
        if v.get("error"):
            line += f" — error: {v['error']}"
        if missing:
            line += f"\n    missing: `{'`, `'.join(missing)}`"
        lines.append(line)

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
    })
    return blocks


def _format_alert_blocks(results: list) -> list[dict]:
    """Format ServiceAlerts list into Slack blocks."""
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{_flag()} Alerts"}},
    ]

    if not results:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":large_green_circle: No new alerts."},
        })
        return blocks

    for sa in results:
        lines = [f":warning: *{sa.service}* — {len(sa.alerts)} new alert(s)"]
        for alert in sa.alerts[:10]:
            preview = alert.message[:120].replace("\n", " ")
            lines.append(
                f"    [{alert.severity}] x{alert.count} — {preview}"
            )
        if len(sa.alerts) > 10:
            lines.append(f"    _...and {len(sa.alerts) - 10} more_")

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        })

    return blocks


# ---------------------------------------------------------------------------
# Stdout formatting
# ---------------------------------------------------------------------------

def _print_health(results: list) -> None:
    print("\n=== Health Checks ===")
    by_service: dict[str, list] = {}
    for r in results:
        by_service.setdefault(r.service, []).append(r)

    for service, checks in by_service.items():
        failed = [c for c in checks if c.status == "fail"]
        tag = "OK" if not failed else "FAIL"
        print(f"  [{tag}] {service} — {len(checks)} checks, {len(failed)} failed")
        for c in failed:
            print(f"        FAIL {c.check}: {c.error or 'unknown'}")


def _print_bq(results: list[dict]) -> None:
    print("\n=== BigQuery Data Summary ===")
    for v in results:
        missing = v.get("missing_resources", [])
        tag = "OK" if not missing and not v.get("error") else "ISSUE"
        row_count = v.get("row_count")
        rc = f"{row_count:,}" if row_count is not None else "N/A"
        res_count = v.get("resource_count")
        res = str(res_count) if res_count is not None else "N/A"
        print(f"  [{tag}] {v['view']} — {rc} rows, {res} resources")
        if v.get("error"):
            print(f"        error: {v['error']}")
        if missing:
            print(f"        missing: {', '.join(missing)}")


def _print_alerts(results: list) -> None:
    print("\n=== Log Alerts ===")
    if not results:
        print("  No new alerts.")
        return
    for sa in results:
        print(f"  {sa.service} — {len(sa.alerts)} new alert(s)")
        for alert in sa.alerts[:10]:
            preview = alert.message[:120].replace("\n", " ")
            print(f"      [{alert.severity}] x{alert.count} — {preview}")
        if len(sa.alerts) > 10:
            print(f"      ...and {len(sa.alerts) - 10} more")


# ---------------------------------------------------------------------------
# Module runners (each tolerates initialization failures)
# ---------------------------------------------------------------------------

def _run_health() -> list | None:
    try:
        from monitor.health import HealthChecker
        checker = HealthChecker()
        return checker.run_all()
    except Exception as exc:
        logger.warning("health check module unavailable: %s", exc)
        return None


def _run_bq_summary() -> list[dict] | None:
    try:
        from monitor.bq_summary import BigQuerySummary
        summary = BigQuerySummary()
        return summary.run()
    except Exception as exc:
        logger.warning("bq summary module unavailable: %s", exc)
        return None


def _run_alerts() -> list | None:
    try:
        from monitor.alerter import LogAlerter
        alerter = LogAlerter()
        return alerter.check()
    except Exception as exc:
        logger.warning("alerter module unavailable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    parser = argparse.ArgumentParser(description="Genetics results suite monitor")
    parser.add_argument("--health", action="store_true", help="Run health checks")
    parser.add_argument("--bq-summary", action="store_true", help="Run BQ data summary")
    parser.add_argument("--alerts", action="store_true", help="Run log alerting")
    parser.add_argument("--all", action="store_true", help="Run all checks (default)")
    args = parser.parse_args()

    run_all = args.all or not (args.health or args.bq_summary or args.alerts)

    health_results = None
    bq_results = None
    alert_results = None

    if run_all or args.health:
        logger.info("running health checks")
        health_results = _run_health()

    if run_all or args.bq_summary:
        logger.info("running bq summary")
        bq_results = _run_bq_summary()

    if run_all or args.alerts:
        logger.info("running log alerter")
        alert_results = _run_alerts()

    # stdout output
    if health_results is not None:
        _print_health(health_results)
    if bq_results is not None:
        _print_bq(bq_results)
    if alert_results is not None:
        _print_alerts(alert_results)

    # detect failures
    has_failure = False
    if health_results is not None:
        has_failure = has_failure or any(r.status == "fail" for r in health_results)
    if bq_results is not None:
        has_failure = has_failure or any(
            v.get("error") or v.get("missing_resources") for v in bq_results
        )
    if alert_results is not None:
        has_failure = has_failure or any(sa.alerts for sa in alert_results)

    # build Slack blocks
    slack_blocks: list[dict] = []
    if health_results is not None:
        slack_blocks.extend(_format_health_blocks(health_results))
        slack_blocks.append({"type": "divider"})
    if bq_results is not None:
        slack_blocks.extend(_format_bq_blocks(bq_results))
        slack_blocks.append({"type": "divider"})
    if alert_results is not None:
        slack_blocks.extend(_format_alert_blocks(alert_results))

    if has_failure:
        alert_user = os.environ.get("SLACK_ALERT_USER_ID")
        if alert_user:
            slack_blocks.append({"type": "divider"})
            slack_blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"<@{alert_user}> issues detected"},
            })

    # send to Slack if webhook is configured
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if webhook_url and slack_blocks:
        logger.info("sending results to slack")
        send_slack_message(webhook_url, slack_blocks)
    elif not webhook_url:
        logger.info("SLACK_WEBHOOK_URL not set, skipping slack notification")

    return 1 if has_failure else 0


if __name__ == "__main__":
    sys.exit(main())
