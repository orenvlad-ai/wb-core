"""Allowlist of reusable production-data adapters.

An adapter is registered here only when its domain owns a repeated operation.
One-off WBC recovery programs do not belong in this registry.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from importlib import import_module
from typing import Any, Protocol

from apps.production_apply_contract import AmbiguousSubmit
class Adapter(Protocol):
    def preview(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]: ...
    def apply(self, request: dict[str, Any], operation_id: str, preview: dict[str, Any]) -> dict[str, Any]: ...
    def readback(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]: ...


_ADAPTER_TYPES: dict[str, tuple[str, str]] = {
    "inventory_retention_publication_v1": (
        "apps.inventory_retention_publication", "InventoryRetentionPublicationAdapter",
    ),
    "web_source_publication_v1": (
        "apps.web_vitrina_web_source_publication", "WebSourcePublicationAdapter",
    ),
    "finance_daily_publication_v1": (
        "apps.finance_daily_publication", "FinanceDailyPublicationAdapter",
    ),
    "ads_partial_publication_v1": (
        "apps.ads_partial_publication", "AdsPartialPublicationAdapter",
    ),
    "fbs_snapshot_accounting_v1": (
        "packages.application.fbs_accounting_apply", "FbsAccountingAdapter",
    ),
    "finance_payout_reconcile_v1": (
        "packages.application.wb_finance_payout_apply", "FinancePayoutAdapter",
    ),
    "supplier_invoice_revision_v1": (
        "packages.application.supplier_shipment_invoice_revision", "SupplierInvoiceRevisionAdapter",
    ),
    "web_vitrina_management_history_v1": (
        "apps.web_vitrina_management_history", "WebVitrinaManagementHistoryAdapter",
    ),
    "web_vitrina_wb_history_recovery_v1": (
        "apps.web_vitrina_wb_history_recovery", "WebVitrinaWbHistoryRecoveryAdapter",
    ),
    "wb_fbs_mapping_evidence_v1": (
        "apps.wb_fbs_mapping_evidence_production_adapter",
        "WbFbsMappingEvidenceProductionAdapter",
    ),
    "search_cluster_cleaner_manual_v1": (
        "apps.search_cluster_cleaner_production_adapter",
        "SearchClusterCleanerProductionAdapter",
    ),
}


class LazyAdapterRegistry(Mapping[str, Adapter]):
    """Instantiate an adapter only after the launcher has selected its name."""

    def __init__(self, specs: Mapping[str, tuple[str, str]]) -> None:
        self._specs = dict(specs)
        self._instances: dict[str, Adapter] = {}

    def __getitem__(self, name: str) -> Adapter:
        if name not in self._specs:
            raise KeyError(name)
        adapter = self._instances.get(name)
        if adapter is None:
            module_name, type_name = self._specs[name]
            adapter_type = getattr(import_module(module_name), type_name)
            adapter = adapter_type()
            self._instances[name] = adapter
        return adapter

    def __iter__(self) -> Iterator[str]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)


ADAPTERS: Mapping[str, Adapter] = LazyAdapterRegistry(_ADAPTER_TYPES)


__all__ = ["ADAPTERS", "Adapter", "AmbiguousSubmit", "LazyAdapterRegistry"]
