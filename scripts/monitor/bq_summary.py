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

        For each dataset in the active profile, find which views its resource
        should appear in by checking the dataset_to_resource_rules applies_to
        fields. The fallback rule (pattern "*") means any resource that doesn't
        match an explicit rule still appears in whatever views it's relevant to,
        so we use the profile dataset's resource directly and determine applicable
        views from the rules that produce that resource.
        """
        expected: dict[str, set[str]] = {v: set() for v in VIEWS}

        profile_data = self.config.get("profiles", {}).get(self.profile, {})
        datasets = profile_data.get("datasets", {})
        if not datasets:
            logger.warning("no datasets found for profile %s", self.profile)
            return expected

        rules = self.config.get("dataset_to_resource_rules", [])

        # build a map: resource -> set of views it can appear in
        # from explicit rules (non-wildcard)
        resource_to_views: dict[str, set[str]] = {}
        for rule in rules:
            resource = rule.get("resource")
            applies_to = rule.get("applies_to", [])
            if resource and applies_to:
                resource_to_views.setdefault(resource, set()).update(applies_to)

        # for each dataset in the profile, determine which views its resource belongs to
        for ds_config in datasets.values():
            resource = ds_config.get("resource")
            if not resource:
                continue

            if resource in resource_to_views:
                for view in resource_to_views[resource]:
                    expected[view].add(resource)
            else:
                # fallback rule — resource appears via lowercased dataset name
                # these resources appear in credible_sets_v/colocalization_v/coloc_credsets_v
                # (the default views for fine-mapping data) unless data_type indicates otherwise
                data_type = ds_config.get("data_type", "")
                if data_type == "exome":
                    expected["exome_variant_results_v"].add(resource)
                elif data_type == "gene_based":
                    expected["gene_burden_results_v"].add(resource)
                elif data_type in ("gwas", "eqtl", "pqtl", "sqtl", "caqtl", "metaboqtl", "mixed"):
                    for v in ("credible_sets_v", "colocalization_v", "coloc_credsets_v"):
                        expected[v].add(resource)
                # expression, gene_disease, chromatin_peaks are not in BQ views

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
