"""Allowlist of reusable production-data adapters.

An adapter is registered here only when its domain owns a repeated operation.
One-off WBC recovery programs do not belong in this registry.
"""

from __future__ import annotations

from typing import Any, Protocol

from apps.production_apply_contract import AmbiguousSubmit
from packages.application.wb_finance_payout_apply import FinancePayoutAdapter
from packages.application.fbs_accounting_apply import FbsAccountingAdapter
from apps.wb_fbs_mapping_evidence_production_adapter import (
    WbFbsMappingEvidenceProductionAdapter,
)
from packages.application.supplier_shipment_invoice_revision import SupplierInvoiceRevisionAdapter
from apps.web_vitrina_management_history import WebVitrinaManagementHistoryAdapter
from apps.web_vitrina_wb_history_recovery import WebVitrinaWbHistoryRecoveryAdapter
from apps.ads_partial_publication import AdsPartialPublicationAdapter
from apps.finance_daily_publication import FinanceDailyPublicationAdapter
from apps.web_vitrina_web_source_publication import WebSourcePublicationAdapter
from apps.inventory_retention_publication import InventoryRetentionPublicationAdapter


class Adapter(Protocol):
    def preview(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]: ...
    def apply(self, request: dict[str, Any], operation_id: str, preview: dict[str, Any]) -> dict[str, Any]: ...
    def readback(self, request: dict[str, Any], operation_id: str) -> dict[str, Any]: ...


ADAPTERS: dict[str, Adapter] = {
    "inventory_retention_publication_v1": InventoryRetentionPublicationAdapter(),
    "web_source_publication_v1": WebSourcePublicationAdapter(),
    "finance_daily_publication_v1": FinanceDailyPublicationAdapter(),
    "ads_partial_publication_v1": AdsPartialPublicationAdapter(),
    "fbs_snapshot_accounting_v1": FbsAccountingAdapter(),
    "finance_payout_reconcile_v1": FinancePayoutAdapter(),
    "supplier_invoice_revision_v1": SupplierInvoiceRevisionAdapter(),
    "web_vitrina_management_history_v1": WebVitrinaManagementHistoryAdapter(),
    "web_vitrina_wb_history_recovery_v1": WebVitrinaWbHistoryRecoveryAdapter(),
    "wb_fbs_mapping_evidence_v1": WbFbsMappingEvidenceProductionAdapter(),
}


__all__ = ["ADAPTERS", "Adapter", "AmbiguousSubmit"]
