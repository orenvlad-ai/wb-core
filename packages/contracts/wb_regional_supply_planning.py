"""Contracts for the WB regional supply planning assistant."""

from __future__ import annotations


CONTRACT_NAME = "sheet_vitrina_v1_wb_regional_supply_planning"
CONTRACT_VERSION = "v2_planning_zones"

STATUS_READY = "ready"
STATUS_BLOCKED = "blocked"
STATUS_EMPTY = "empty"
STATUS_NO_LAST_CALCULATION = "no_last_calculation"
STATUS_UPSTREAM_ERROR = "upstream_error"
STATUS_NO_OPTIONS = "no_options"

PACKAGE_TYPE_BOX = "box"
PACKAGE_TYPES = (PACKAGE_TYPE_BOX,)
BOX_TYPE_IDS = (1, 2)

ROUTE_DIRECT = "direct"
ROUTE_TRANSIT = "transit"

WAREHOUSE_SCOPE_SAME_DISTRICT = "same_district"
WAREHOUSE_SCOPE_OUTSIDE_DISTRICT = "outside_district"
WAREHOUSE_SCOPE_UNMAPPED = "unmapped"
