#!/usr/bin/env python3
"""Offline assertions about the monitor's report delivery, alert dedup and view derivation.

Run: python3 scripts/test-monitor.py   (needs PyYAML and requests; no network, no GCP)
Exit 0 = pass, 1 = a property is broken, 2 = the harness could not run.

Cloud Logging and the Slack webhook are the seams replaced here: log entries are fed to
LogAlerter through a stub client, and send_slack_message is swapped in main's namespace, so
what is driven is the decision logic over a real SQLite dedup database.
"""

import os
import sys
import tempfile
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checks  # noqa: E402
from checks import check  # noqa: E402

try:
    import requests  # noqa: F401
    import yaml  # noqa: F401
except ImportError as exc:
    checks.die(f"{exc.name} is missing")


class _Entry:
    def __init__(self, container, message, severity="ERROR"):
        self.resource = types.SimpleNamespace(labels={"container_name": container})
        self.payload = message
        self.severity = severity
        self.timestamp = datetime(2026, 10, 22, 7, 0, tzinfo=timezone.utc)


class _LoggingClient:
    entries: list = []

    def __init__(self, project=None):
        pass

    def list_entries(self, **_):
        return list(self.entries)


def _stub_google():
    google = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    google.cloud = cloud
    logging_stub = types.ModuleType("google.cloud.logging")
    logging_stub.Client = _LoggingClient
    logging_stub.ASCENDING = "asc"
    sys.modules["google.cloud.logging"] = logging_stub
    cloud.logging = logging_stub
    bigquery_stub = types.ModuleType("google.cloud.bigquery")
    bigquery_stub.Client = lambda project=None: None
    sys.modules["google.cloud.bigquery"] = bigquery_stub
    cloud.bigquery = bigquery_stub
    api_core = sys.modules.setdefault("google.api_core", types.ModuleType("google.api_core"))
    exceptions = types.ModuleType("google.api_core.exceptions")
    exceptions.NotFound = type("NotFound", (Exception,), {})
    sys.modules["google.api_core.exceptions"] = exceptions
    api_core.exceptions = exceptions


_stub_google()
from monitor import alerter, bq_summary, main, views  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_main(db, entries, delivered):
    """One monitor run, alerts only, against the dedup DB `db`. Returns (exit, posted blocks)."""
    _LoggingClient.entries = entries
    posted = []

    def send(url, blocks):
        posted.append(blocks)
        return delivered

    main.send_slack_message = send
    sys.argv = ["monitor", "--alerts"]
    os.environ.update(GCP_PROJECT="p", MONITOR_DB_PATH=db, SLACK_WEBHOOK_URL="https://hook")
    return main.main(), posted


def alert_text(posted):
    return " ".join(str(b) for blocks in posted for b in blocks)


def test_delivery(tmp):
    db = os.path.join(tmp, "monitor.db")
    entries = [_Entry("chat-backend", "ERROR: boom 1")]

    code, posted = run_main(db, entries, delivered=False)
    check("failed delivery exits non-zero", code == 1, str(code))
    check("failed delivery attempted a post", len(posted) == 1)

    code, posted = run_main(db, entries, delivered=True)
    check("retry after a failed delivery re-sends the alert", "boom" in alert_text(posted), alert_text(posted))
    check("successful delivery exits 0", code == 0, str(code))

    code, posted = run_main(db, entries, delivered=True)
    check("a delivered alert is not re-sent", "boom" not in alert_text(posted), alert_text(posted))
    check("run with only seen alerts exits 0", code == 0, str(code))

    os.environ.pop("SLACK_WEBHOOK_URL")
    db2 = os.path.join(tmp, "nohook.db")
    _LoggingClient.entries = entries
    sys.argv = ["monitor", "--alerts"]
    os.environ["MONITOR_DB_PATH"] = db2
    check("no webhook configured: exits 0", main.main() == 0)
    a = alerter.LogAlerter()
    check("no webhook configured: stdout is the report, so the alert is marked seen", a.check() == [])


def test_check_is_read_only(tmp):
    os.environ.update(GCP_PROJECT="p", MONITOR_DB_PATH=os.path.join(tmp, "ro.db"))
    _LoggingClient.entries = [_Entry("db-api", "ERROR: x"), _Entry("db-api", "ERROR: x")]
    a = alerter.LogAlerter()
    first = a.check()
    check("one grouped alert with count 2", [(s.service, [e.count for e in s.alerts]) for s in first] == [("db-api", [2])])
    check("alert carries its dedup key", bool(first and first[0].alerts[0].dedup_key))
    check("check() commits nothing", len(a.check()) == 1)
    a.mark_seen(first)
    check("mark_seen suppresses the next check", a.check() == [])
    a.mark_seen(first)
    check("mark_seen is idempotent", a.check() == [])


def test_views(tmp):
    with open(os.path.join(ROOT, "configs", "datasets.yaml")) as fh:
        config = yaml.safe_load(fh)
    monitored = views.monitored_views(config)
    tables = list(config["tables"])
    check("every monitored view is a table", set(monitored) <= set(tables), str(set(monitored) - set(tables)))
    check("API_VIEWS are monitored", views.API_VIEWS <= set(monitored), str(views.API_VIEWS - set(monitored)))
    check("credible_sets_v is monitored", "credible_sets_v" in monitored)
    check("a single-source view is not monitored", "gene_annotations_v" not in monitored)
    check("only colocalization_v carries resource1/resource2", views.dual_resource_views(config) == {"colocalization_v"},
          str(views.dual_resource_views(config)))

    synthetic = {
        "tables": {"b_v": {}, "a_v": {}, "c_v": {}, "coloc_credsets_v": {}},
        "dataset_to_resource_rules": [
            {"pattern": "X%", "resource": "x", "applies_to": ["a_v", "coloc_credsets_v", "missing_v"]},
            {"pattern": "Y", "resource": "y", "applies_to": ["b_v"]},
            {"pattern": "*", "resource": "LOWER(dataset)"},
        ],
        "profiles": {"p": {"datasets": {"dx": {"resource": "x"}, "dy": {"resource": "y"}}}},
    }
    check("tables order, rule-covered only, unknown views dropped",
          views.monitored_views(synthetic) == ["b_v", "a_v", "coloc_credsets_v"],
          str(views.monitored_views(synthetic)))

    s = bq_summary.BigQuerySummary(project="p", config_path="unused", profile="p")
    s._config = synthetic
    got = {k: sorted(v) for k, v in s._get_config_expected().items()}
    check("rule views expect their profile's resources; API views expect none from rules",
          got == {"b_v": ["y"], "a_v": ["x"], "coloc_credsets_v": []}, str(got))

    s = bq_summary.BigQuerySummary(project="p", config_path=os.path.join(tmp, "absent.yaml"), profile="p")
    out = s.run()
    check("no derivable views reports an error rather than an empty report",
          len(out) == 1 and "no monitored views" in (out[0]["error"] or ""), str(out))


def main_():
    with tempfile.TemporaryDirectory() as tmp:
        test_views(tmp)
        test_check_is_read_only(tmp)
        test_delivery(tmp)

    print(f"\n{checks.CHECKS - len(checks.FAILURES)}/{checks.CHECKS} passed")
    return 1 if checks.FAILURES else 0


if __name__ == "__main__":
    sys.exit(main_())
