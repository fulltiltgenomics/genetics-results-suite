"""BigQuery data summary module for monitoring.

Queries BQ views for row counts and resource coverage, then compares
actual resources against expected resources derived from datasets.yaml.
"""

import logging
import os
from dataclasses import dataclass, field

import yaml
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


class BigQuerySummary:
    """Queries BQ views and compares actual vs expected resource coverage."""

    def __init__(
        self,
        project: str | None = None,
        bq_dataset: str | None = None,
        config_path: str | None = None,
        profile: str | None = None,
    ):
        self.project = project or os.environ["GCP_PROJECT"]
        self.bq_dataset = bq_dataset or os.environ.get("BQ_DATASET", "genetics_results")
        self.config_path = config_path or os.environ.get(
            "DATASETS_CONFIG_PATH", "configs/datasets.yaml"
        )
        self.profile = profile or os.environ.get("CONFIG_PROFILE", "daly")
        self.client = bigquery.Client(project=self.project)
        self._config = None

    @property
    def config(self) -> dict:
        if self._config is None:
            self._config = self._load_config()
        return self._config

    def _load_config(self) -> dict:
        try:
            with open(self.config_path) as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error("failed to load datasets config from %s: %s", self.config_path, e)
            return {}

    def _get_expected_resources(self) -> dict[str, set[str]]:
        """Derive expected resources per view from profile datasets and mapping rules.

        Only resources with explicit dataset_to_resource_rules entries (with
        applies_to) are expected in BQ views. Datasets that fall through to
        the wildcard rule (e.g. external summary stats) are not expected —
        having a gwas data_type doesn't mean the data was fine-mapped into BQ.

        Datasets with pseudo_credible_sets are excluded from colocalization
        and coloc_credsets views since they lack formal fine-mapping.
        """
        expected: dict[str, set[str]] = {v: set() for v in VIEWS}

        profile_data = self.config.get("profiles", {}).get(self.profile, {})
        datasets = profile_data.get("datasets", {})
        if not datasets:
            logger.warning("no datasets found for profile %s", self.profile)
            return expected

        rules = self.config.get("dataset_to_resource_rules", [])

        # build a map: resource -> set of views from explicit (non-wildcard) rules
        resource_to_views: dict[str, set[str]] = {}
        for rule in rules:
            resource = rule.get("resource")
            applies_to = rule.get("applies_to", [])
            if resource and applies_to:
                resource_to_views.setdefault(resource, set()).update(applies_to)

        # coloc views that pseudo_credible_sets datasets should be excluded from
        coloc_views = {"colocalization_v", "coloc_credsets_v"}

        # collect which resources have pseudo_credible_sets
        pseudo_resources: set[str] = set()
        for ds_config in datasets.values():
            if ds_config.get("pseudo_credible_sets"):
                r = ds_config.get("resource")
                if r:
                    pseudo_resources.add(r)

        for ds_config in datasets.values():
            resource = ds_config.get("resource")
            if not resource or resource not in resource_to_views:
                continue

            for view in resource_to_views[resource]:
                if resource in pseudo_resources and view in coloc_views:
                    continue
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
