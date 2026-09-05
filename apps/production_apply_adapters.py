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


class Adapter(Protocol):
    def preview(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]: ...
    def apply(self, request: dict[str, Any], operation_id: str, preview: dict[str, Any]) -> dict[str, Any]: ...
    def readback(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]: ...


ADAPTERS: dict[str, Adapter] = {
    "wb_fbs_mapping_evidence_v1": WbFbsMappingEvidenceProductionAdapter(),
}


__all__ = ["ADAPTERS", "Adapter", "AmbiguousSubmit"]
