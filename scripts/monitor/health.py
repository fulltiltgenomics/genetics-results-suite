"""
Health checker for genetics-results-suite cluster services.

Verifies service liveness via health endpoints and dataset accessibility
via the results-api /api/v1/datasets endpoint.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests
import yaml

logger = logging.getLogger(__name__)

# data types that are served by results-api and can be verified
API_SERVED_DATA_TYPES = frozenset({
    "gwas", "eqtl", "pqtl", "sqtl", "caqtl", "metaboqtl", "mixed",
    "exome", "gene_based", "expression", "gene_disease", "chromatin_peaks",
})

# profiles whose deployments hold the single-cell expression data. daly and daly-staging both run
# profile "daly" and share one bucket, but only daly's tfvars sets expression_data_enabled (the
# flag creates the bucket, so staging must leave it off); the profile is what both pods agree on.
CELLXGENE_PROFILES = frozenset({"daly"})


@dataclass
class CheckResult:
    """Outcome of a single health check."""
    service: str
    check: str
    status: str  # "ok", "warn", "fail"
    response_time_ms: float = 0.0
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class HealthChecker:
    """Runs health checks against in-cluster services and verifies datasets."""

    def __init__(
        self,
        results_api_url: str | None = None,
        chat_backend_url: str | None = None,
        frontend_url: str | None = None,
        api_secret: str | None = None,
        datasets_config_path: str | None = None,
        config_profile: str | None = None,
        timeout: float = 15.0,
    ):
        self.results_api_url = (
            results_api_url
            or os.environ.get("RESULTS_API_URL", "http://results-api.genetics.svc.cluster.local:4000")
        )
        self.chat_backend_url = (
            chat_backend_url
            or os.environ.get("CHAT_BACKEND_URL", "http://chat-backend.genetics.svc.cluster.local:8000")
        )
        self.frontend_url = (
            frontend_url
            or os.environ.get("FRONTEND_URL", "http://frontend.genetics.svc.cluster.local:3000")
        )
        self.mcp_server_url = os.environ.get(
            "MCP_SERVER_URL", "http://mcp-server.genetics.svc.cluster.local:8080"
        )
        self.db_api_url = os.environ.get(
            "DB_API_URL", "http://db-api.genetics.svc.cluster.local:8080"
        )
        self.api_secret = api_secret or os.environ.get("INTERNAL_API_SECRET", "")
        self.datasets_config_path = (
            datasets_config_path
            or os.environ.get("DATASETS_CONFIG_PATH", "/app/configs/datasets.yaml")
        )
        self.config_profile = (
            config_profile
            or os.environ.get("CONFIG_PROFILE", "finngen")
        )
        self.timeout = timeout
        self.cellxgene_bucket = os.environ.get("CELLXGENE_BUCKET", "daly-expression-data")
        self.cellxgene_root = os.environ.get("CELLXGENE_ROOT", "cellxgene")
        # off until the initial full driver run has published a release: before that a missing
        # pointer or table is the expected state, not an outage
        self.cellxgene_live = os.environ.get("CELLXGENE_LIVE", "false").lower() == "true"
        self.cellxgene_max_age_days = float(os.environ.get("CELLXGENE_MAX_AGE_DAYS", "10"))
        self.bq_project = os.environ.get("GCP_PROJECT", "")
        self.bq_dataset = os.environ.get("BQ_DATASET", "genetics_results")

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def run_all(self) -> list[CheckResult]:
        """Run all health checks and return results. Never raises."""
        results: list[CheckResult] = []
        results.extend(self.check_service_health())
        results.extend(self.check_datasets())
        if self.config_profile in CELLXGENE_PROFILES:
            results.extend(self.check_cellxgene_release())
        return results

    def check_cellxgene_release(self, now: datetime | None = None) -> list[CheckResult]:
        """The CELLxGENE release pointer: present, recent, and agreeing with BigQuery."""
        now = now or datetime.now(timezone.utc)
        results: list[CheckResult] = []

        def unavailable(check: str, what: str, why: str) -> CheckResult:
            if self.cellxgene_live:
                return CheckResult("cellxgene", check, "fail", error=f"{what}: {why}")
            return CheckResult(
                "cellxgene", check, "ok",
                details={"note": f"{what}: {why}; CELLXGENE_LIVE is false, so not expected yet"},
            )

        pointer_uri = f"gs://{self.cellxgene_bucket}/{self.cellxgene_root}/releases/CURRENT"
        try:
            raw = self._read_cellxgene_current()
            current = json.loads(raw) if raw is not None else None
            if current is not None and not isinstance(current.get("release"), str):
                raise ValueError("no string 'release' field")
        except Exception as exc:
            logger.warning("cellxgene: could not read %s: %s", pointer_uri, exc)
            return [unavailable("release_pointer", pointer_uri, f"unreadable ({exc})")]
        if current is None:
            return [unavailable("release_pointer", pointer_uri, "missing")]
        release = current["release"]
        results.append(CheckResult(
            "cellxgene", "release_pointer", "ok",
            details={"release": release, "datasets": current.get("datasets")},
        ))

        try:
            written = datetime.fromisoformat(str(current["written_at"]).replace("Z", "+00:00"))
            if written.tzinfo is None:
                written = written.replace(tzinfo=timezone.utc)
            age_days = (now - written).total_seconds() / 86400
        except Exception:
            results.append(CheckResult(
                "cellxgene", "release_age", "fail",
                error=f"{pointer_uri} has no parseable written_at ({current.get('written_at')!r})",
            ))
        else:
            if age_days > self.cellxgene_max_age_days:
                results.append(CheckResult(
                    "cellxgene", "release_age", "fail",
                    error=(
                        f"release {release} was written {age_days:.1f} days ago "
                        f"(limit {self.cellxgene_max_age_days:g}); the weekly refresh is not "
                        f"landing. Check the Cloud Scheduler job's state first: a recreated job "
                        f"comes back PAUSED and nothing else announces it. Then the driver's "
                        f"latest Batch job and runs/<run_id>/report.json"
                    ),
                    details={"age_days": round(age_days, 1)},
                ))
            else:
                results.append(CheckResult(
                    "cellxgene", "release_age", "ok", details={"age_days": round(age_days, 1)},
                ))

        table = f"{self.bq_project}.{self.bq_dataset}.cellxgene_datasets"
        try:
            loaded = self._loaded_cellxgene_releases()
        except Exception as exc:
            logger.error("cellxgene: BigQuery release query failed: %s", exc)
            results.append(CheckResult(
                "cellxgene", "bigquery_release", "fail", error=f"could not query {table}: {exc}",
            ))
            return results
        if loaded is None:
            results.append(unavailable("bigquery_release", table, "table does not exist"))
        elif loaded != {release}:
            hint = " (the pointer says bigquery_loaded=false)" if current.get("bigquery_loaded") is False else ""
            results.append(CheckResult(
                "cellxgene", "bigquery_release", "fail",
                error=(
                    f"BigQuery holds {sorted(loaded) or 'no rows'} but CURRENT is {release}{hint}. "
                    f"BigQuery ahead means the loader was killed after its commit; behind means "
                    f"a load failed. Re-run the driver or the loader to converge"
                ),
            ))
        else:
            results.append(CheckResult(
                "cellxgene", "bigquery_release", "ok", details={"release": release},
            ))
        return results

    def check_service_health(self) -> list[CheckResult]:
        """Liveness checks for all ClusterIP services."""
        checks = [
            ("results-api", f"{self.results_api_url}/healthz"),
            ("chat-backend", f"{self.chat_backend_url}/healthz"),
            ("frontend", f"{self.frontend_url}/"),
            ("mcp-server", f"{self.mcp_server_url}/healthz"),
            ("db-api", f"{self.db_api_url}/health"),
        ]
        results = []
        for service, url in checks:
            results.append(self._http_get(service, "liveness", url))
        return results

    def check_datasets(self) -> list[CheckResult]:
        """Load datasets.yaml, then verify each API-served dataset is
        accessible via results-api /api/v1/datasets."""
        results: list[CheckResult] = []

        datasets_config = self._load_datasets_config()
        if datasets_config is None:
            results.append(CheckResult(
                service="monitor",
                check="load_datasets_config",
                status="fail",
                error=f"could not load {self.datasets_config_path}",
            ))
            return results

        expected_datasets = self._extract_expected_datasets(datasets_config)
        if not expected_datasets:
            results.append(CheckResult(
                service="monitor",
                check="parse_datasets_config",
                status="warn",
                error=f"no API-served datasets found in profile '{self.config_profile}'",
            ))
            return results

        # fetch actual dataset list from results-api (without stats to keep it fast)
        api_result = self._http_get(
            "results-api",
            "datasets_endpoint",
            f"{self.results_api_url}/api/v1/datasets?include_stats=false",
            auth=True,
        )
        results.append(api_result)

        if api_result.status == "fail":
            # can't verify individual datasets if the endpoint is down
            for ds_id in sorted(expected_datasets):
                results.append(CheckResult(
                    service="results-api",
                    check=f"dataset:{ds_id}",
                    status="fail",
                    error="datasets endpoint unavailable",
                ))
            return results

        # parse response to get set of served dataset IDs
        served_ids: set[str] = set()
        try:
            api_datasets = api_result.details.get("response_json", [])
            for ds in api_datasets:
                ds_id = ds.get("dataset_id")
                if ds_id:
                    served_ids.add(ds_id)
        except Exception as exc:
            logger.warning("failed to parse datasets response: %s", exc)

        # compare expected vs actual
        for ds_id in sorted(expected_datasets):
            if ds_id in served_ids:
                results.append(CheckResult(
                    service="results-api",
                    check=f"dataset:{ds_id}",
                    status="ok",
                    details={"resource": expected_datasets[ds_id].get("resource")},
                ))
            else:
                results.append(CheckResult(
                    service="results-api",
                    check=f"dataset:{ds_id}",
                    status="fail",
                    error="dataset not found in API response",
                    details={"resource": expected_datasets[ds_id].get("resource")},
                ))

        return results

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _http_get(
        self,
        service: str,
        check_name: str,
        url: str,
        auth: bool = False,
    ) -> CheckResult:
        """Perform an HTTP GET and return a CheckResult."""
        headers = {}
        if auth and self.api_secret:
            headers["Authorization"] = f"Bearer {self.api_secret}"

        start = time.monotonic()
        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code < 400:
                result = CheckResult(
                    service=service,
                    check=check_name,
                    status="ok",
                    response_time_ms=round(elapsed_ms, 1),
                )
                # try to capture JSON for further processing
                try:
                    result.details["response_json"] = resp.json()
                except Exception:
                    pass
                return result
            else:
                return CheckResult(
                    service=service,
                    check=check_name,
                    status="fail",
                    response_time_ms=round(elapsed_ms, 1),
                    error=f"HTTP {resp.status_code}",
                    details={"body": resp.text[:500]},
                )
        except requests.exceptions.Timeout:
            elapsed_ms = (time.monotonic() - start) * 1000
            return CheckResult(
                service=service,
                check=check_name,
                status="fail",
                response_time_ms=round(elapsed_ms, 1),
                error=f"timeout after {self.timeout}s",
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return CheckResult(
                service=service,
                check=check_name,
                status="fail",
                response_time_ms=round(elapsed_ms, 1),
                error=str(exc),
            )

    def _read_cellxgene_current(self) -> bytes | None:
        """The pointer's bytes, or None when the object does not exist."""
        from google.api_core.exceptions import NotFound
        from google.cloud import storage

        blob = storage.Client(project=self.bq_project or None).bucket(self.cellxgene_bucket).blob(
            f"{self.cellxgene_root}/releases/CURRENT"
        )
        try:
            return blob.download_as_bytes(timeout=self.timeout)
        except NotFound:
            return None

    def _loaded_cellxgene_releases(self) -> set[str] | None:
        """Distinct releases in cellxgene_datasets, or None when the table does not exist."""
        from google.api_core.exceptions import NotFound
        from google.cloud import bigquery

        client = bigquery.Client(project=self.bq_project)
        try:
            rows = client.query(
                f"SELECT DISTINCT release FROM `{self.bq_project}.{self.bq_dataset}.cellxgene_datasets`"
            ).result()
            return {r.release for r in rows}
        except NotFound:
            return None

    def _load_datasets_config(self) -> dict | None:
        """Load and parse datasets.yaml. Returns None on failure."""
        try:
            with open(self.datasets_config_path, "r") as f:
                return yaml.safe_load(f)
        except Exception as exc:
            logger.error("failed to load datasets config from %s: %s", self.datasets_config_path, exc)
            return None

    def _extract_expected_datasets(self, config: dict) -> dict[str, dict]:
        """Return dict of {dataset_id: dataset_entry} for datasets in the
        active profile that are served by results-api."""
        profile = config.get("profiles", {}).get(self.config_profile, {})
        datasets = profile.get("datasets", {})
        if not datasets:
            return {}

        expected = {}
        for ds_id, entry in datasets.items():
            if not isinstance(entry, dict):
                continue
            data_type = entry.get("data_type", "")
            # include datasets that have a metadata_file or a recognized API data type
            if entry.get("metadata_file") or data_type in API_SERVED_DATA_TYPES:
                expected[ds_id] = entry
        return expected
