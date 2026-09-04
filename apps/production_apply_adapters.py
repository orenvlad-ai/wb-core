"""Allowlist of reusable production-data adapters.

An adapter is registered here only when its domain owns a repeated operation.
One-off WBC recovery programs do not belong in this registry.
"""

from __future__ import annotations

from typing import Any, Protocol


class Adapter(Protocol):
    def preview(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]: ...
    def apply(self, request: dict[str, Any], operation_id: str, preview: dict[str, Any]) -> dict[str, Any]: ...
    def readback(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]: ...


# Deliberately empty after retirement of historical WBC recovery modes.
# A future repeated operation adds one reviewed, domain-owned adapter object.
ADAPTERS: dict[str, Adapter] = {}
