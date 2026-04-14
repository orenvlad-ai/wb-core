"""Общие synthetic helpers для smoke-check registry upload."""

from __future__ import annotations

import json
from pathlib import Path

from packages.contracts.registry_upload_bundle_v1 import (
    ConfigV2Item,
    FormulaV2Item,
    MetricV2Item,
    RegistryUploadBundleV1,
)

LEGACY_CONFIG_CAP = 256
LEGACY_METRICS_CAP = 64
LEGACY_FORMULAS_CAP = 128
SYNTHETIC_CONFIG_COUNT = 300
SYNTHETIC_METRIC_COUNT = 300
SYNTHETIC_FORMULA_COUNT = 150
SYNTHETIC_BUNDLE_VERSION = "registry_upload_uncapped_counts__2026-04-14T11:48:59Z"
SYNTHETIC_UPLOADED_AT = "2026-04-14T11:48:59Z"


def build_synthetic_oversized_bundle(
    *,
    config_count: int = SYNTHETIC_CONFIG_COUNT,
    metric_count: int = SYNTHETIC_METRIC_COUNT,
    formula_count: int = SYNTHETIC_FORMULA_COUNT,
) -> RegistryUploadBundleV1:
    return RegistryUploadBundleV1(
        bundle_version=SYNTHETIC_BUNDLE_VERSION,
        uploaded_at=SYNTHETIC_UPLOADED_AT,
        config_v2=[
            ConfigV2Item(
                nm_id=900000 + index,
                enabled=True,
                display_name=f"Synthetic config {index}",
                group="synthetic",
                display_order=index,
            )
            for index in range(1, config_count + 1)
        ],
        metrics_v2=[
            MetricV2Item(
                metric_key=f"synthetic_metric_{index:03d}",
                enabled=True,
                scope="SKU",
                label_ru=f"Синтетическая метрика {index}",
                calc_type="metric",
                calc_ref=f"synthetic_metric_{index:03d}",
                show_in_data=True,
                format="integer",
                display_order=index,
                section="Synthetic",
            )
            for index in range(1, metric_count + 1)
        ],
        formulas_v2=[
            FormulaV2Item(
                formula_id=f"F_SYNTHETIC_{index:03d}",
                expression="1",
                description=f"Synthetic formula {index}",
            )
            for index in range(1, formula_count + 1)
        ],
    )


def write_runtime_registry_fixture(path: Path, bundle: RegistryUploadBundleV1) -> None:
    payload = {
        "registry_id": "metric_runtime_registry",
        "version": "synthetic_uncapped_counts_v1",
        "items": [
            {
                "metric_key": metric.metric_key,
                "metric_kind": "direct",
                "value_unit": "count",
                "value_scale": "integer",
                "missing_policy": "propagate_missing",
                "period_agg": "scope_aggregate",
                "formula_id": None,
                "ratio_num_key": None,
                "ratio_den_key": None,
                "source_family": "synthetic",
                "source_module": "registry_upload_smoke_support",
                "is_runtime_enabled": True,
            }
            for metric in bundle.metrics_v2
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
