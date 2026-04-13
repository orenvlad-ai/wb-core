"""Контракты registry upload bundle v1."""

from dataclasses import dataclass
from typing import Literal


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
class RegistryUploadBundleV1:
    bundle_version: str
    uploaded_at: str
    config_v2: list[ConfigV2Item]
    metrics_v2: list[MetricV2Item]
    formulas_v2: list[FormulaV2Item]


@dataclass(frozen=True)
class RegistryUploadBundleV1ValidationReport:
    bundle_version: str
    config_count: int
    metric_count: int
    formula_count: int
    scope_values: list[str]
    calc_types: list[str]
    runtime_metric_keys_checked: int
