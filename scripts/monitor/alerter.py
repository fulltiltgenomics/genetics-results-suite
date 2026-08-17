"""Cloud Logging alerter with SQLite-based deduplication."""

import hashlib
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from google.cloud import logging as cloud_logging

logger = logging.getLogger("monitor.alerter")


@dataclass
class AlertEntry:
    message: str
    severity: str
    count: int
    first_seen: str


@dataclass
class ServiceAlerts:
    service: str
    alerts: list[AlertEntry] = field(default_factory=list)


# patterns stripped during message normalization for dedup
_NORMALIZE_PATTERNS = [
    re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.\d]*Z?"),  # ISO timestamps
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I),  # UUIDs
    re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),  # IPv4 addresses
    re.compile(r"\b[0-9a-f]{16,}\b", re.I),  # long hex strings (trace/span IDs)
    re.compile(r"request[_-]?id[=: ]+\S+", re.I),  # request ID key-value pairs
]

# (container, message regex) pairs whose log entries should be dropped before alerting.
# oauth2-proxy logs probing traffic at WARNING which would otherwise spam Slack.
_IGNORE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("oauth2-proxy", re.compile(r"Invalid redirect provided")),
    ("oauth2-proxy", re.compile(r"Invalid redirect generated")),
    ("oauth2-proxy", re.compile(r"Error while parsing OAuth2 state")),  # stale/bot callbacks
    # the IdP put ?error=... on the callback: crawlers mangling the authorize URL
    # (invalid_scope), scanners injecting into it (unsupported_response_type), or a login
    # page left open past the Keycloak auth session (temporarily_unavailable). All are
    # client-side and unactionable; a real scope misconfiguration shows up as nobody
    # being able to sign in, not as a log line worth paging on.
    ("oauth2-proxy", re.compile(r"Error while parsing OAuth2 callback")),
]

# GKE's logging agent tags everything a container writes to stderr as severity=ERROR
# regardless of content, so the entry severity is meaningless for the many services that
# log normally to stderr (uvicorn, postgres, batch scripts). Recover the level the app
# itself reported from the message text and alert on that instead.
_LEVEL_PATTERNS = [
    # python logging / uvicorn: "INFO:     Application startup complete."
    re.compile(r"^\s*(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b[: ]"),
    # nginx: "2026/07/12 17:34:56 [error] 30#30: *2480 ..."
    re.compile(r"\[(?P<level>debug|info|notice|warn|error|crit|alert|emerg)\]"),
    # postgres: "2026-07-12 15:51:32.554 UTC [27] LOG:  checkpoint complete: ..."
    re.compile(
        r"\[\d+\]\s+(?P<level>DEBUG[1-5]?|LOG|INFO|NOTICE|WARNING|ERROR|FATAL|PANIC"
        r"|STATEMENT|DETAIL|HINT|CONTEXT):"
    ),
]

_LEVEL_RANK = {
    "DEBUG": 10,
    "INFO": 20, "LOG": 20, "NOTICE": 20,
    "STATEMENT": 20, "DETAIL": 20, "HINT": 20, "CONTEXT": 20,
    "WARN": 30, "WARNING": 30,
    "ERROR": 40, "CRIT": 40, "CRITICAL": 40,
    "ALERT": 50, "EMERG": 50, "EMERGENCY": 50, "FATAL": 50, "PANIC": 50,
}
_MIN_ALERT_RANK = _LEVEL_RANK["WARNING"]


def _should_ignore(container: str, message: str) -> bool:
    return any(c == container and p.search(message) for c, p in _IGNORE_PATTERNS)


def _embedded_level(message: str) -> str | None:
    """The log level the application itself reported, if the message carries one."""
    for pattern in _LEVEL_PATTERNS:
        match = pattern.search(message)
        if match:
            return match.group("level").upper().rstrip("12345")
    return None


def _effective_severity(message: str, gcp_severity: str | None) -> str:
    """The app's own level, falling back to GCP's when the message carries none."""
    return _embedded_level(message) or gcp_severity or "WARNING"


def _below_threshold(severity: str) -> bool:
    """True when the level is known to be below WARNING. Unknown levels alert (fail open)."""
    rank = _LEVEL_RANK.get(severity)
    return rank is not None and rank < _MIN_ALERT_RANK


def _normalize_message(msg: str) -> str:
    """Strip volatile tokens so semantically identical messages produce the same hash."""
    for pattern in _NORMALIZE_PATTERNS:
        msg = pattern.sub("", msg)
    return re.sub(r"\s+", " ", msg).strip()


def _dedup_key(container: str, message: str) -> str:
    normalized = _normalize_message(message)
    return hashlib.sha256(f"{container}|{normalized}".encode()).hexdigest()


class LogAlerter:
    """Queries Cloud Logging for warnings/errors and returns deduplicated, grouped alerts."""

    def __init__(self):
        self.project = os.environ["GCP_PROJECT"]
        self.namespace = os.environ.get("K8S_NAMESPACE", "genetics")
        self.lookback_hours = int(os.environ.get("ALERT_LOOKBACK_HOURS", "8"))
        self.dedup_ttl_hours = int(os.environ.get("ALERT_DEDUP_TTL_HOURS", "24"))
        self.db_path = os.environ.get("MONITOR_DB_PATH", "/tmp/monitor.db")

        self._init_db()
        self.client = cloud_logging.Client(project=self.project)

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS seen_alerts (
                dedup_key TEXT PRIMARY KEY,
                first_seen REAL NOT NULL
            )"""
        )
        conn.commit()
        conn.close()

    def _cleanup_expired(self):
        cutoff = time.time() - self.dedup_ttl_hours * 3600
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM seen_alerts WHERE first_seen < ?", (cutoff,))
        conn.commit()
        conn.close()

    def _is_new(self, key: str) -> bool:
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT 1 FROM seen_alerts WHERE dedup_key = ?", (key,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO seen_alerts (dedup_key, first_seen) VALUES (?, ?)",
                (key, time.time()),
            )
            conn.commit()
        conn.close()
        return row is None

    def _query_logs(self) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)
        timestamp_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        log_filter = (
            f'resource.type="k8s_container"'
            f' AND resource.labels.namespace_name="{self.namespace}"'
            f' AND severity >= "WARNING"'
            f' AND timestamp >= "{timestamp_str}"'
        )

        entries = []
        dropped = 0
        for entry in self.client.list_entries(
            filter_=log_filter,
            order_by=cloud_logging.ASCENDING,
            page_size=1000,
        ):
            container = (
                entry.resource.labels.get("container_name", "unknown")
                if entry.resource and entry.resource.labels
                else "unknown"
            )
            payload = entry.payload
            if isinstance(payload, dict):
                message = payload.get("message", str(payload))
            elif isinstance(payload, str):
                message = payload
            else:
                message = str(payload)

            if _should_ignore(container, message):
                dropped += 1
                continue

            severity = _effective_severity(message, entry.severity)
            if _below_threshold(severity):
                dropped += 1
                continue

            entries.append({
                "container": container,
                "message": message,
                "severity": severity,
                "timestamp": (
                    entry.timestamp.isoformat() if entry.timestamp else timestamp_str
                ),
            })

        if dropped:
            logger.info(
                "dropped %d non-alerting log entries (ignored or below WARNING once "
                "reclassified from the message text)", dropped
            )

        return entries

    def check(self) -> list[ServiceAlerts]:
        """Query logs, deduplicate, group by service, and return new alerts."""
        self._cleanup_expired()

        raw_entries = self._query_logs()

        # group by (container, dedup_key) and count occurrences, keeping only new alerts
        grouped: dict[str, dict[str, dict]] = {}  # container -> {dedup_key -> info}

        for entry in raw_entries:
            key = _dedup_key(entry["container"], entry["message"])
            container = entry["container"]

            if container not in grouped:
                grouped[container] = {}

            if key in grouped[container]:
                grouped[container][key]["count"] += 1
            else:
                grouped[container][key] = {
                    "message": entry["message"],
                    "severity": entry["severity"],
                    "count": 1,
                    "first_seen": entry["timestamp"],
                    "dedup_key": key,
                }

        # filter to only new (not previously seen) alerts
        results: list[ServiceAlerts] = []
        for container in sorted(grouped):
            service_alerts = ServiceAlerts(service=container)
            for info in grouped[container].values():
                if self._is_new(info["dedup_key"]):
                    service_alerts.alerts.append(
                        AlertEntry(
                            message=info["message"],
                            severity=info["severity"],
                            count=info["count"],
                            first_seen=info["first_seen"],
                        )
                    )
            if service_alerts.alerts:
                results.append(service_alerts)

        return results
