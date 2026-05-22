"""BigQuery data summary module for monitoring.

Queries BQ views for row counts and resource coverage, then compares
actual resources against expected resources derived from the results-api
/api/v1/datasets endpoint (which knows which products each dataset supports).
"""

import logging
import os
from dataclasses import dataclass, field

import requests
from google.cloud import bigquery
from google.api_core.exceptions import NotFound

logger = logging.getLogger(__name__)

VIEWS = [
    "credible_sets_v",
    "colocalization_v",
    "coloc_credsets_v",
    "exome_variant_results_v",
    "gene_burden_results_v",
]

# colocalization views use resource1/resource2 instead of resource
_COLOC_VIEWS = {"colocalization_v"}


@dataclass
class ViewSummary:
    view: str
    row_count: int | None = None
    resource_count: int | None = None
    actual_resources: set = field(default_factory=set)
    expected_resources: set = field(default_factory=set)
    missing_resources: set = field(default_factory=set)
    unexpected_resources: set = field(default_factory=set)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "view": self.view,
            "row_count": self.row_count,
            "resource_count": self.resource_count,
            "actual_resources": sorted(self.actual_resources),
            "expected_resources": sorted(self.expected_resources),
            "missing_resources": sorted(self.missing_resources),
            "unexpected_resources": sorted(self.unexpected_resources),
            "error": self.error,
        }


    # API product key -> BQ views that product's resource should appear in
_PRODUCT_TO_VIEWS: dict[str, list[str]] = {
    "credible_sets": ["credible_sets_v"],
    "colocalization": ["colocalization_v", "coloc_credsets_v"],
    "exome_results": ["exome_variant_results_v"],
    "gene_based_results": ["gene_burden_results_v"],
}


class BigQuerySummary:
    """Queries BQ views and compares actual vs expected resource coverage."""

    def __init__(
        self,
        project: str | None = None,
        bq_dataset: str | None = None,
        results_api_url: str | None = None,
        api_secret: str | None = None,
    ):
        self.project = project or os.environ["GCP_PROJECT"]
        self.bq_dataset = bq_dataset or os.environ.get("BQ_DATASET", "genetics_results")
        self.results_api_url = (
            results_api_url
            or os.environ.get("RESULTS_API_URL", "http://results-api.genetics.svc.cluster.local:4000")
        )
        self.api_secret = api_secret or os.environ.get("INTERNAL_API_SECRET", "")
        self.client = bigquery.Client(project=self.project)

    def _get_expected_resources(self) -> dict[str, set[str]]:
        """Derive expected resources per view from the results-api.

        Calls /api/v1/datasets to get each dataset's products (credible_sets,
        colocalization, exome_results, gene_based_results) and maps the
        dataset's resource to the corresponding BQ views.
        """
        expected: dict[str, set[str]] = {v: set() for v in VIEWS}

        try:
            headers = {}
            if self.api_secret:
                headers["Authorization"] = f"Bearer {self.api_secret}"
            resp = requests.get(
                f"{self.results_api_url}/api/v1/datasets",
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            api_datasets = resp.json()
        except Exception as e:
            logger.warning("could not fetch datasets from API, skipping expected resource comparison: %s", e)
            return expected

        for ds in api_datasets:
            resource = ds.get("resource")
            products = ds.get("products", {})
            if not resource or not products:
                continue

            for product_key, views in _PRODUCT_TO_VIEWS.items():
                if product_key in products:
                    for view in views:
                        expected[view].add(resource)

        return expected

    def _query_view(self, view: str) -> ViewSummary:
        """Query a single BQ view for row count and distinct resources."""
        summary = ViewSummary(view=view)
        full_view = f"`{self.project}.{self.bq_dataset}.{view}`"

        try:
            if view in _COLOC_VIEWS:
                count_sql = f"SELECT COUNT(*) AS row_count FROM {full_view}"
                resource_sql = (
                    f"SELECT DISTINCT r FROM ("
                    f"  SELECT resource1 AS r FROM {full_view} "
                    f"  UNION DISTINCT "
                    f"  SELECT resource2 AS r FROM {full_view}"
                    f")"
                )

                count_result = self.client.query(count_sql).result()
                for row in count_result:
                    summary.row_count = row.row_count

                resource_result = self.client.query(resource_sql).result()
                for row in resource_result:
                    if row.r:
                        summary.actual_resources.add(row.r)
            else:
                count_sql = f"SELECT COUNT(*) AS row_count FROM {full_view}"
                resource_sql = f"SELECT DISTINCT resource FROM {full_view} WHERE resource IS NOT NULL"

                count_result = self.client.query(count_sql).result()
                for row in count_result:
                    summary.row_count = row.row_count

                resource_result = self.client.query(resource_sql).result()
                for row in resource_result:
                    summary.actual_resources.add(row.resource)

            summary.resource_count = len(summary.actual_resources)

        except NotFound:
            summary.error = f"view {view} does not exist"
            logger.warning(summary.error)
        except Exception as e:
            summary.error = str(e)
            logger.error("error querying %s: %s", view, e)

        return summary

    def run(self) -> list[dict]:
        """Query all views and return structured comparison results."""
        expected_by_view = self._get_expected_resources()
        results = []

        for view in VIEWS:
            summary = self._query_view(view)
            summary.expected_resources = expected_by_view.get(view, set())
            summary.missing_resources = summary.expected_resources - summary.actual_resources
            summary.unexpected_resources = summary.actual_resources - summary.expected_resources
            results.append(summary.to_dict())

        return results
