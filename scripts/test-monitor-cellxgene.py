#!/usr/bin/env python3
"""Offline assertions about the monitor's CELLxGENE release check and its log ignore pattern.

Run: python3 scripts/test-monitor-cellxgene.py   (needs PyYAML and requests; no network, no GCP)
Exit 0 = pass, 1 = a property is broken, 2 = the harness could not run.

The GCS read and the BigQuery query are the two seams HealthChecker exposes
(_read_cellxgene_current, _loaded_cellxgene_releases); both are replaced here, so what is
driven is the decision logic: which condition alerts, which is silenced by CELLXGENE_LIVE,
and which deployments are checked at all.
"""

import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checks  # noqa: E402
from checks import check  # noqa: E402

try:
    import requests  # noqa: F401
    import yaml  # noqa: F401
except ImportError as exc:
    checks.die(f"{exc.name} is missing")

# alerter imports google.cloud.logging at module level; only its regex list is used here
if "google.cloud.logging" not in sys.modules:
    google = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    stub = types.ModuleType("google.cloud.logging")
    sys.modules["google.cloud.logging"] = stub
    google.cloud, cloud.logging = cloud, stub

from monitor import alerter, health  # noqa: E402

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
REL = "2026-10-30-020000"


def pointer(days_old=3, release=REL, **extra):
    written = (NOW - timedelta(days=days_old)).isoformat().replace("+00:00", "Z")
    return json.dumps({"release": release, "written_at": written, "datasets": 40, **extra}).encode()


def run(profile="daly", live=False, current=pointer(), loaded=frozenset({REL}), max_age=None):
    c = health.HealthChecker(config_profile=profile)
    c.cellxgene_live = live
    if max_age is not None:
        c.cellxgene_max_age_days = max_age

    def read():
        if isinstance(current, Exception):
            raise current
        return current

    def load():
        if isinstance(loaded, Exception):
            raise loaded
        return None if loaded is None else set(loaded)

    c._read_cellxgene_current, c._loaded_cellxgene_releases = read, load
    return {r.check: r for r in c.check_cellxgene_release(now=NOW)}


def failed(res):
    return sorted(k for k, r in res.items() if r.status == "fail")


def main():
    res = run()
    check("healthy release: nothing fails", failed(res) == [], str(failed(res)))
    check("healthy release: all three checks reported", sorted(res) == ["bigquery_release", "release_age", "release_pointer"])

    for live in (False, True):
        for name, cur in (("missing", None), ("unreadable", OSError("denied")), ("bad json", b"{")):
            res = run(live=live, current=cur)
            want = ["release_pointer"] if live else []
            check(f"pointer {name}, live={live}", failed(res) == want and list(res) == ["release_pointer"], str(failed(res)))

    res = run(current=pointer(days_old=11))
    check("pointer older than the default 10 days fails", failed(res) == ["release_age"], str(failed(res)))
    err = res["release_age"].error
    check("age alert names the Cloud Scheduler job and PAUSED", "Cloud Scheduler" in err and "PAUSED" in err, err)
    check("pointer at 9 days passes", failed(run(current=pointer(days_old=9))) == [])
    check("max age is configurable", failed(run(current=pointer(days_old=3), max_age=2)) == ["release_age"])
    check("age applies while not live once a pointer exists", failed(run(live=False, current=pointer(days_old=30))) == ["release_age"])
    check("unparseable written_at fails", failed(run(current=json.dumps({"release": REL, "written_at": "yesterday"}).encode())) == ["release_age"])

    for name, loaded in (("ahead", {"2026-11-01-020000"}), ("behind", {"2026-10-01-020000"}), ("empty", set()), ("mixed", {REL, "2026-10-01-020000"})):
        res = run(loaded=loaded)
        check(f"BigQuery {name} of CURRENT fails", failed(res) == ["bigquery_release"], str(failed(res)))
    check("mismatch alert names both sides", REL in run(loaded={"x"})["bigquery_release"].error)
    check("mismatch alerts regardless of the live switch", failed(run(live=False, loaded={"x"})) == ["bigquery_release"])

    check("missing table, not live: silent", failed(run(live=False, loaded=None)) == [])
    check("missing table, live: fails", failed(run(live=True, loaded=None)) == ["bigquery_release"])
    check("BigQuery query error fails", failed(run(loaded=RuntimeError("quota"))) == ["bigquery_release"])

    # the gate: run_all reaches the check only on a profile that holds the data
    calls = []
    for profile in ("daly", "finngen"):
        c = health.HealthChecker(config_profile=profile)
        c.check_service_health = c.check_datasets = lambda: []
        c.check_cellxgene_release = lambda: calls.append(profile) or []
        c.run_all()
    check("daly profile runs the check, finngen does not", calls == ["daly"], str(calls))

    ign = alerter._should_ignore
    refresh = "ERROR:app.services.data_access_cell_type_expression:cell-type expression: refresh failed, still serving none: no CURRENT"
    initial = "ERROR:app.services.data_access_cell_type_expression:cell-type expression: initial release load failed: 404"
    for live, suppressed in (("false", True), ("", True), ("true", False)):
        os.environ["CELLXGENE_LIVE"] = live
        for name, msg in (("refresh-failed-none", refresh), ("initial-load-failed", initial)):
            check(f"{name} with CELLXGENE_LIVE={live!r}: {'ignored' if suppressed else 'alerts'}",
                  ign("results-api", msg) == suppressed)
    os.environ["CELLXGENE_LIVE"] = "false"
    check("same message from another container is not ignored", not ign("chat-backend", refresh))
    check("refresh failure while serving a release still alerts", not ign("results-api", refresh.replace("none", REL)))
    check("another results-api error still alerts", not ign("results-api", "ERROR: cell-type expression: refresh timed out"))
    os.environ.pop("CELLXGENE_LIVE")

    print(f"\n{checks.CHECKS - len(checks.FAILURES)}/{checks.CHECKS} passed")
    return 1 if checks.FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
