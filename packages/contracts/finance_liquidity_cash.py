"""Versioned identity constants for the implemented cash vertical slice."""

from __future__ import annotations

FINANCE_CASH_CONTRACT = "finance_cash_v1"
FINANCE_CASH_SCHEMA_VERSION = 3
# 8766 is owned by wb-core-data-mcp in the current deployment topology.
FINANCE_CASH_DEFAULT_PORT = 8767
FINANCE_CASH_API_PREFIX = "/v1/finance"
FINANCE_CASH_UI_PREFIX = "/finance/"

# Reserved identifiers only. Cash v1 neither persists nor resolves these links.
FINANCE_DOCUMENT_SUPPLY_LINK_CONTRACT = "finance_document_supply_link_v1"
FINANCE_PAYMENT_OBLIGATION_ALLOCATION_CONTRACT = (
    "finance_payment_obligation_allocation_v1"
)
FINANCE_OPERATION_STATEMENT_MATCH_CONTRACT = "finance_operation_statement_match_v1"
