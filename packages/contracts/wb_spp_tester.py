"""Contracts and constants for the WB SPP tester operator block."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping


SPP_TEST_CONTRACT_PREFIX = "sheet_vitrina_v1_prices_spp_test"
SPP_TEST_MODE_SAFE_SLOW = "safe_slow"
SPP_TEST_MAX_MEASUREMENTS_MIN = 3
SPP_TEST_MAX_MEASUREMENTS_MAX = 12
SPP_TEST_DEFAULT_PRECISION_RUB = 2
SPP_TEST_DEFAULT_MAX_MEASUREMENTS = 8

SPP_TEST_ACTIVE_STATUSES = {
    "planning",
    "measuring",
    "cooldown",
    "restoring",
    "running",
}
SPP_TEST_FINAL_STATUSES = {
    "complete",
    "threshold_detected",
    "threshold_not_detected",
    "inconclusive",
    "manual_restore_required",
    "failed",
}

SppTestJobStatus = Literal[
    "planning",
    "measuring",
    "cooldown",
    "restoring",
    "complete",
    "manual_restore_required",
    "failed",
]

SppTestResultStatus = Literal[
    "threshold_detected",
    "threshold_not_detected",
    "inconclusive",
    "manual_restore_required",
]


@dataclass(frozen=True)
class SppTestPointPlan:
    target_discounted_price: float
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_discounted_price": self.target_discounted_price,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class SppTestPlan:
    nm_id: int
    range_min_discounted: float
    range_max_discounted: float
    precision_rub: float
    max_measurements: int
    mode: str
    initial_points: list[SppTestPointPlan]
    refinement_budget: int
    estimated_duration_seconds: int
    request_budget: Mapping[str, int]
    restore_route: list[Mapping[str, Any]]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "nmID": self.nm_id,
            "range_min_discounted": self.range_min_discounted,
            "range_max_discounted": self.range_max_discounted,
            "precision_rub": self.precision_rub,
            "max_measurements": self.max_measurements,
            "mode": self.mode,
            "initial_points": [point.to_dict() for point in self.initial_points],
            "refinement_budget": self.refinement_budget,
            "estimated_duration_seconds": self.estimated_duration_seconds,
            "request_budget": dict(self.request_budget),
            "restore_route": [dict(item) for item in self.restore_route],
            "warnings": list(self.warnings),
        }
