"""
Health checker for genetics-results-suite cluster services.

Verifies service liveness via health endpoints and dataset accessibility
via the results-api /api/v1/datasets endpoint.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests
import yaml

logger = logging.getLogger(__name__)

# data types that are served by results-api and can be verified
API_SERVED_DATA_TYPES = frozenset({
    "gwas", "eqtl", "pqtl", "sqtl", "caqtl", "metaboqtl", "mixed",
    "exome", "gene_based", "expression", "gene_disease", "chromatin_peaks",
})


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

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def run_all(self) -> list[CheckResult]:
        """Run all health checks and return results. Never raises."""
        results: list[CheckResult] = []
        results.extend(self.check_service_health())
        results.extend(self.check_datasets())
        return results

    def check_service_health(self) -> list[CheckResult]:
        """Liveness checks for all ClusterIP services."""
        checks = [
            ("results-api", f"{self.results_api_url}/healthz"),
            ("chat-backend", f"{self.chat_backend_url}/healthz"),
            ("frontend", f"{self.frontend_url}/"),
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
