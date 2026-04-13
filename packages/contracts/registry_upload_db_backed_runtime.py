"""Контракты DB-backed runtime слоя для registry upload."""

from dataclasses import dataclass

from packages.contracts.registry_upload_bundle_v1 import (
    ConfigV2Item,
    FormulaV2Item,
    MetricV2Item,
)


@dataclass(frozen=True)
class RegistryUploadDbBackedCurrentState:
    bundle_version: str
    activated_at: str
    config_v2: list[ConfigV2Item]
    metrics_v2: list[MetricV2Item]
    formulas_v2: list[FormulaV2Item]
