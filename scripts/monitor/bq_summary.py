"""BigQuery data summary module for monitoring.

Queries BQ views for row counts and resource coverage. Uses a hybrid approach
for expected resources:
- credible_sets, exome, gene_based: config-driven (dataset_to_resource_rules)
- colocalization: API-driven (results-api knows actual coloc pairs)

Collection resources (e.g. eqtl_catalogue) appear as individual sub-resources
in BQ (qtd000001, ...) but as the collection name in the API. The comparison
normalizes BQ resources by collapsing sub-resources back to their collection.
"""

import logging
import os
from dataclasses import dataclass, field

import requests
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
    "asm_qtl_v",
]

# colocalization_v uses resource1/resource2 instead of resource
_DUAL_RESOURCE_VIEWS = {"colocalization_v"}

# views where expected resources come from config rules
_CONFIG_VIEWS = {"credible_sets_v", "exome_variant_results_v", "gene_burden_results_v", "asm_qtl_v"}

# views where expected resources come from the API (coloc pairs)
_API_VIEWS = {"colocalization_v", "coloc_credsets_v"}


@dataclass
class ViewSummary:
    view: str
    row_count: int | None = None
    resource_count: int | None = None
    actual_resources: set = field(default_factory=set)
    expected_resources: set = field(default_factory=set)
    missing_resources: set = field(default_factory=set)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "view": self.view,
            "row_count": self.row_count,
            "resource_count": self.resource_count,
            "actual_resources": sorted(self.actual_resources),
            "expected_resources": sorted(self.expected_resources),
            "missing_resources": sorted(self.missing_resources),
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
        results_api_url: str | None = None,
        api_secret: str | None = None,
    ):
        self.project = project or os.environ["GCP_PROJECT"]
        self.bq_dataset = bq_dataset or os.environ.get("BQ_DATASET", "genetics_results")
        self.config_path = config_path or os.environ.get(
            "DATASETS_CONFIG_PATH", "configs/datasets.yaml"
        )
        self.profile = profile or os.environ.get("CONFIG_PROFILE", "daly")
        self.results_api_url = (
            results_api_url
            or os.environ.get("RESULTS_API_URL", "http://results-api.genetics.svc.cluster.local:4000")
        )
        self.api_secret = api_secret or os.environ.get("INTERNAL_API_SECRET", "")
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

    def _get_collection_map(self) -> dict[str, str]:
        """Build prefix -> collection_resource_name map.
        E.g. {'qtd': 'eqtl_catalogue'} so qtd000001 -> eqtl_catalogue."""
        cmap: dict[str, str] = {}
        for res_name, res_config in self.config.get("resources", {}).items():
            prefix = res_config.get("collection_id_prefix")
            if prefix:
                cmap[prefix] = res_name
        return cmap

    def _normalize_resources(self, resources: set[str]) -> set[str]:
        """Collapse collection sub-resources to their parent name.
        qtd000001 -> eqtl_catalogue, etc. Non-collection resources pass through."""
        cmap = self._get_collection_map()
        if not cmap:
            return resources

        normalized = set()
        for r in resources:
            matched = False
            for prefix, collection_name in cmap.items():
                if r.startswith(prefix):
                    normalized.add(collection_name)
                    matched = True
                    break
            if not matched:
                normalized.add(r)
        return normalized

    def _map_to_bq_resource(self, api_resource: str) -> str:
        """Map an API resource name to its BQ resource name.

        The BQ views use CASE/WHEN with SQL LIKE patterns from
        dataset_to_resource_rules. An API resource like 'ukbb_finucane'
        matches 'UKB%' (case-insensitive) and becomes 'ukbb' in BQ.
        Collection resources (eqtl_catalogue) stay as-is since they're
        handled by _normalize_resources on the BQ side.
        """
        rules = self.config.get("dataset_to_resource_rules", [])
        for rule in rules:
            pattern = rule.get("pattern", "")
            resource = rule.get("resource")
            if not resource or pattern == "*":
                continue
            # convert SQL LIKE pattern to a prefix check (all patterns use trailing %)
            if pattern.endswith("%"):
                prefix = pattern[:-1].lower()
                if api_resource.lower().startswith(prefix):
                    return resource
        return api_resource

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

        profile_resources = {
            ds.get("resource") for ds in datasets.values() if ds.get("resource")
        }

        for resource in profile_resources:
            if resource not in resource_to_views:
                continue
            for view in resource_to_views[resource]:
                if view in _CONFIG_VIEWS:
                    expected[view].add(resource)

        return expected

    def _get_api_coloc_expected(self) -> dict[str, set[str]]:
        """Expected resources for coloc views from the API's dataset products."""
        expected: dict[str, set[str]] = {v: set() for v in _API_VIEWS}

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
            logger.warning("could not fetch datasets from API for coloc expectations: %s", e)
            return expected

        for ds in api_datasets:
            resource = ds.get("resource")
            products = ds.get("products", {})
            if resource and "colocalization" in products:
                # map API resource to BQ resource (e.g. ukbb_finucane -> ukbb)
                bq_resource = self._map_to_bq_resource(resource)
                for view in _API_VIEWS:
                    expected[view].add(bq_resource)

        return expected

    def _get_expected_resources(self) -> dict[str, set[str]]:
        """Combine config-based and API-based expectations."""
        expected = self._get_config_expected()
        coloc_expected = self._get_api_coloc_expected()
        for view, resources in coloc_expected.items():
            expected[view] = resources
        return expected

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

            raw_resources = set()
            resource_result = self.client.query(resource_sql).result()
            for row in resource_result:
                if row.r:
                    raw_resources.add(row.r)

            # normalize: collapse collection sub-resources to parent name
            summary.actual_resources = self._normalize_resources(raw_resources)
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
            results.append(summary.to_dict())

        return results
