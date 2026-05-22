"""BigQuery data summary module for monitoring.

Queries BQ views for row counts and resource coverage. Uses a hybrid approach
for expected resources:
- credible_sets, exome, gene_based: config-driven (dataset_to_resource_rules)
- colocalization: API-driven (results-api knows actual coloc pairs)
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

# colocalization_v uses resource1/resource2 instead of resource
_DUAL_RESOURCE_VIEWS = {"colocalization_v"}

# views where we compare expected vs actual resources (config-driven)
# coloc views are excluded: resource names in BQ don't map 1:1 to API
# resources (e.g. eqtl_catalogue -> qtd*, ukbb_finucane -> ukbb), and
# coloc integrity follows from credible sets being correct
_CHECKED_VIEWS = {"credible_sets_v", "exome_variant_results_v", "gene_burden_results_v"}


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
        self._config: dict | None = None

    @property
    def config(self) -> dict:
        if self._config is None:
            try:
                with open(self.config_path) as f:
                    self._config = yaml.safe_load(f)
            except Exception as e:
                logger.error("failed to load datasets config: %s", e)
                self._config = {}
        return self._config

    def _get_config_expected(self) -> dict[str, set[str]]:
        """Expected resources for credible_sets, exome, gene_based views
        from explicit dataset_to_resource_rules entries."""
        expected: dict[str, set[str]] = {v: set() for v in VIEWS}

        profile_data = self.config.get("profiles", {}).get(self.profile, {})
        datasets = profile_data.get("datasets", {})
        rules = self.config.get("dataset_to_resource_rules", [])

        # resource -> views from explicit (non-wildcard) rules
        resource_to_views: dict[str, set[str]] = {}
        for rule in rules:
            resource = rule.get("resource")
            applies_to = rule.get("applies_to", [])
            if resource and applies_to:
                resource_to_views.setdefault(resource, set()).update(applies_to)

        # only add resources that are in the active profile AND have explicit rules
        profile_resources = {
            ds.get("resource") for ds in datasets.values() if ds.get("resource")
        }

        for resource in profile_resources:
            if resource not in resource_to_views:
                continue
            for view in resource_to_views[resource]:
                if view in _CHECKED_VIEWS:
                    expected[view].add(resource)

        return expected

    def _get_expected_resources(self) -> dict[str, set[str]]:
        """Get expected resources for checked views."""
        return self._get_config_expected()

    def _get_collection_prefixes(self) -> list[str]:
        """Get collection_id_prefix values from resources config (e.g. 'qtd')."""
        prefixes = []
        for res_config in self.config.get("resources", {}).values():
            prefix = res_config.get("collection_id_prefix")
            if prefix:
                prefixes.append(prefix)
        return prefixes

    def _count_top_level_resources(self, resources: set[str]) -> int:
        """Count resources, collapsing collection sub-resources into one.
        E.g. qtd000001..qtd000700 count as 1 (eqtl_catalogue)."""
        prefixes = self._get_collection_prefixes()
        if not prefixes:
            return len(resources)

        top_level = set()
        for r in resources:
            collapsed = False
            for prefix in prefixes:
                if r.startswith(prefix):
                    top_level.add(f"{prefix}*")
                    collapsed = True
                    break
            if not collapsed:
                top_level.add(r)
        return len(top_level)

    def _query_view(self, view: str) -> ViewSummary:
        """Query a single BQ view for row count and distinct resources."""
        summary = ViewSummary(view=view)
        full_view = f"`{self.project}.{self.bq_dataset}.{view}`"

        try:
            if view in _DUAL_RESOURCE_VIEWS:
                count_sql = f"SELECT COUNT(*) AS row_count FROM {full_view}"
                resource_sql = (
                    f"SELECT DISTINCT r FROM ("
                    f"  SELECT resource1 AS r FROM {full_view} "
                    f"  UNION DISTINCT "
                    f"  SELECT resource2 AS r FROM {full_view}"
                    f")"
                )
            else:
                count_sql = f"SELECT COUNT(*) AS row_count FROM {full_view}"
                resource_sql = f"SELECT DISTINCT resource AS r FROM {full_view} WHERE resource IS NOT NULL"

            count_result = self.client.query(count_sql).result()
            for row in count_result:
                summary.row_count = row.row_count

            resource_result = self.client.query(resource_sql).result()
            for row in resource_result:
                if row.r:
                    summary.actual_resources.add(row.r)

            summary.resource_count = self._count_top_level_resources(summary.actual_resources)

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
            # don't flag unexpected — many legitimate resources exist via wildcard rules
            results.append(summary.to_dict())

        return results
