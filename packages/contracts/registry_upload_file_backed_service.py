"""Контракты file-backed service для registry upload."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class RegistryUploadAcceptedCounts:
    config_v2: int
    metrics_v2: int
    formulas_v2: int


@dataclass(frozen=True)
class RegistryUploadResult:
    status: Literal["accepted", "rejected"]
    bundle_version: str
    accepted_counts: RegistryUploadAcceptedCounts
    validation_errors: list[str]
    activated_at: str | None


@dataclass(frozen=True)
class RegistryUploadCurrentMarker:
    bundle_version: str
    activated_at: str
    accepted_bundle_path: str
    upload_result_path: str
