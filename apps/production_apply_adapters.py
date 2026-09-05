"""Allowlist of reusable production-data adapters.

An adapter is registered here only when its domain owns a repeated operation.
One-off WBC recovery programs do not belong in this registry.
"""

from __future__ import annotations

from typing import Any, Protocol

from apps.production_apply_contract import AmbiguousSubmit
from apps.wb_fbs_mapping_evidence_production_adapter import (
    WbFbsMappingEvidenceProductionAdapter,
)
from apps.web_vitrina_management_history import WebVitrinaManagementHistoryAdapter
from apps.web_vitrina_wb_history_recovery import WebVitrinaWbHistoryRecoveryAdapter


class Adapter(Protocol):
    def preview(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]: ...
    def apply(self, request: dict[str, Any], operation_id: str, preview: dict[str, Any]) -> dict[str, Any]: ...
    def readback(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]: ...


ADAPTERS: dict[str, Adapter] = {
    "web_vitrina_management_history_v1": WebVitrinaManagementHistoryAdapter(),
    "web_vitrina_wb_history_recovery_v1": WebVitrinaWbHistoryRecoveryAdapter(),
    "wb_fbs_mapping_evidence_v1": WbFbsMappingEvidenceProductionAdapter(),
}


__all__ = ["ADAPTERS", "Adapter", "AmbiguousSubmit"]
