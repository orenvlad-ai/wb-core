"""Контракты pilot registry bundle."""

from dataclasses import dataclass
from typing import Any, Literal, Optional


@dataclass(frozen=True)
class ConfigV2Item:
    nm_id: int
    enabled: bool
    display_name: str
    group: str
    display_order: int


@dataclass(frozen=True)
class MetricV2Item:
    metric_key: str
    enabled: bool
    scope: str
    label_ru: str
    calc_type: Literal["metric", "formula", "ratio"]
    calc_ref: str
    show_in_data: bool
    format: str
    display_order: int
    section: str


@dataclass(frozen=True)
class FormulaV2Item:
    formula_id: str
    expression: str
    description: str


@dataclass(frozen=True)
class RuntimeMetricItem:
    metric_key: str
    metric_kind: Literal["direct", "formula", "ratio"]
    value_unit: str
    value_scale: str
    missing_policy: str
    period_agg: str
    formula_id: Optional[str]
    ratio_num_key: Optional[str]
    ratio_den_key: Optional[str]
    source_family: str
    source_module: str
    is_runtime_enabled: bool


@dataclass(frozen=True)
class RegistryPilotBundle:
    config_v2: list[ConfigV2Item]
    metrics_v2: list[MetricV2Item]
    formulas_v2: list[FormulaV2Item]
    metric_runtime_registry: list[RuntimeMetricItem]
    bridge_export_bundle: dict[str, Any]


@dataclass(frozen=True)
class RegistryPilotValidationReport:
    sku_count: int
    metric_count: int
    formula_count: int
    runtime_metric_count: int
    metric_kinds: list[str]
    bridge_paths: list[str]
