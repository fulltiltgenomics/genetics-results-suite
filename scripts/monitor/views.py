"""Which BigQuery views the monitor checks, derived from configs/datasets.yaml.

Kept free of GCP imports so scripts/gen-doc-blocks.py can render the monitored-views
table from the same derivation the CronJob runs.
"""

# expected resources for these come from the results-api's coloc pairs rather than from
# the rules: a rule names every resource whose coloc *could* be loaded, while the API knows
# which pairs actually were. datasets.yaml carries no field for this choice, so it lives here
API_VIEWS = frozenset({"colocalization_v", "coloc_credsets_v"})


def monitored_views(config: dict) -> list[str]:
    """Every `tables` entry that some `dataset_to_resource_rules` entry names in `applies_to`.

    Those are the views whose per-resource coverage can be compared against an expected set;
    a view no rule names is registry-authoritative or single-source, so there is nothing to
    compare. Ordered as in `tables`.
    """
    named = {
        view
        for rule in config.get("dataset_to_resource_rules") or []
        for view in rule.get("applies_to") or []
    }
    return [view for view in config.get("tables") or {} if view in named]


def dual_resource_views(config: dict) -> set[str]:
    """Views carrying `resource1`/`resource2` instead of a single `resource` column."""
    return {
        view
        for view, entry in (config.get("tables") or {}).items()
        if {"resource1", "resource2"} <= set((entry or {}).get("columns") or {})
    }


def expected_source(view: str) -> str:
    return "api" if view in API_VIEWS else "rules"
