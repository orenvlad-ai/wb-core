"""Durable fail-closed write epoch for the warehouse accounting domain.

The existing file-backed business-data barrier blocks HTTP writes and the
maintenance contract drains the scheduled warehouse writer.  This module is a
second, SQLite-local defence: once a later reviewed cutover transaction appends
``held``, every covered accounting table rejects writes.  The same transaction
may temporarily append ``applying`` while holding ``BEGIN IMMEDIATE`` and must
finish at ``readback_required`` before it commits.

Stage 6 installs the empty contract and triggers only.  It exposes no
production acquisition or apply entrypoint.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping


CONTRACT_NAME = "warehouse_domain_write_epoch_v1"
EVENTS_TABLE = "sheet_vitrina_v1_warehouse_domain_write_epoch_events"
ACTIVE_BLOCKING_PHASES = (
    "held",
    "readback_required",
    "recovery_required",
    "recovery_readback_required",
    "reconciled",
)
PHASES = (
    "held",
    "applying",
    "readback_required",
    "recovery_required",
    "recovery_applying",
    "recovery_readback_required",
    "reconciled",
    "released",
    "aborted",
)

# Cache/shadow ingestion tables are intentionally absent.  WB/FBS collectors
# may continue to persist observations while the accounting boundary is held.
WAREHOUSE_DOMAIN_TABLES = (
    "sheet_vitrina_v1_supplier_shipments",
    "sheet_vitrina_v1_supplier_shipment_lines",
    "sheet_vitrina_v1_supplier_ff_cost_layers",
    "sheet_vitrina_v1_supplier_ff_cost_layer_lines",
    "sheet_vitrina_v1_wb_supply_cost_layers",
    "sheet_vitrina_v1_ff_stock_operations",
    "sheet_vitrina_v1_ff_stock_operation_lines",
    "sheet_vitrina_v1_ff_stock_reservation_operations",
    "sheet_vitrina_v1_ff_stock_reservation_lines",
    "sheet_vitrina_v1_ff_stock_wb_supply_lifecycle",
    "sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint",
    "sheet_vitrina_v1_ff_inventory_reconciliations",
    "sheet_vitrina_v1_ff_inventory_cost_bases",
    "sheet_vitrina_v1_ff_overhead_documents",
    "sheet_vitrina_v1_wb_supply_box_corrections",
    "sheet_vitrina_v1_own_capital_events",
    "sheet_vitrina_v1_own_capital_wb_outstanding",
    "sheet_vitrina_v1_warehouse_functional_versions",
    "sheet_vitrina_v1_warehouse_functional_cutovers",
    "sheet_vitrina_v1_warehouse_functional_active",
    "sheet_vitrina_v1_warehouse_functional_balances",
    "sheet_vitrina_v1_warehouse_functional_ff_reservations",
    "sheet_vitrina_v1_warehouse_functional_documents",
    "sheet_vitrina_v1_warehouse_functional_document_lines",
    "sheet_vitrina_v1_warehouse_functional_read_models",
    "sheet_vitrina_v1_warehouse_functional_events",
    "sheet_vitrina_v1_warehouse_business_projection_revisions",
    "sheet_vitrina_v1_warehouse_business_projection_rows",
    "sheet_vitrina_v1_warehouse_business_projection_current_rows",
    "sheet_vitrina_v1_warehouse_business_projection_state",
    "sheet_vitrina_v1_ff_facilities",
    "sheet_vitrina_v1_ff_facility_changes",
    "sheet_vitrina_v1_warehouse_business_operations",
    "sheet_vitrina_v1_ff_pool_movement_lines",
    "sheet_vitrina_v1_warehouse_business_operation_relations",
    "sheet_vitrina_v1_ff_pool_feature_epochs",
    "sheet_vitrina_v1_ff_pool_balances",
    "sheet_vitrina_v1_ff_pool_documents",
    "sheet_vitrina_v1_ff_pool_document_lines",
    "sheet_vitrina_v1_ff_pool_document_expense_lines",
    "sheet_vitrina_v1_ff_pool_document_relations",
    "sheet_vitrina_v1_wb_supply_ff_origin_assignments",
    "sheet_vitrina_v1_ff_pool_cutover_manifests",
    "sheet_vitrina_v1_ff_pool_cutover_allocation_lines",
    "sheet_vitrina_v1_ff_pool_cutover_order_classifications",
    "sheet_vitrina_v1_ff_pool_cutover_fbw_origins",
    "sheet_vitrina_v1_ff_pool_cutover_checkpoints",
    "sheet_vitrina_v1_ff_pool_cutover_opening_reservations",
    "sheet_vitrina_v1_ff_pool_cutover_late_pre_t_cases",
    "sheet_vitrina_v1_ff_pool_cutover_pending_shipments",
    "sheet_vitrina_v1_ff_pool_fbs_lifecycle_events",
    "sheet_vitrina_v1_ff_pool_fbs_lifecycle_current",
    "sheet_vitrina_v1_ff_pool_fbs_reconciliation_lane",
    "sheet_vitrina_v1_ff_pool_fbs_drain_state",
    "sheet_vitrina_v1_ff_pool_fbs_late_evidence",
)


class WarehouseDomainWriteBlocked(RuntimeError):
    """A warehouse accounting write reached an active domain epoch."""


def ensure_warehouse_domain_write_guard_schema(conn: sqlite3.Connection) -> None:
    """Install one empty append-only epoch log and guards for existing tables."""

    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {EVENTS_TABLE}(
            event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            epoch_id TEXT NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ({_sql_values(PHASES)})),
            manifest_digest TEXT NOT NULL,
            deployed_sha TEXT NOT NULL,
            event_at TEXT NOT NULL
                CHECK(substr(event_at,-1,1)='Z' AND julianday(event_at) IS NOT NULL),
            actor TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(details_json)),
            UNIQUE(epoch_id,phase),
            CHECK(length(trim(epoch_id)) BETWEEN 8 AND 120),
            CHECK(manifest_digest GLOB 'sha256:*' AND length(manifest_digest)=71),
            CHECK(length(deployed_sha)=40 AND deployed_sha NOT GLOB '*[^0-9a-f]*'),
            CHECK(length(trim(actor)) BETWEEN 1 AND 160)
        );
        CREATE INDEX IF NOT EXISTS warehouse_domain_write_epoch_by_identity
        ON {EVENTS_TABLE}(epoch_id,event_sequence,phase);

        CREATE TRIGGER IF NOT EXISTS warehouse_domain_write_epoch_transition
        BEFORE INSERT ON {EVENTS_TABLE}
        BEGIN
            SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM {EVENTS_TABLE})
              AND NEW.phase<>'held'
              THEN RAISE(ABORT,'warehouse domain epoch must start held') END;
            SELECT CASE WHEN EXISTS(SELECT 1 FROM {EVENTS_TABLE})
              AND NEW.epoch_id<>(SELECT epoch_id FROM {EVENTS_TABLE}
                                 ORDER BY event_sequence DESC LIMIT 1)
              AND (SELECT phase FROM {EVENTS_TABLE}
                   ORDER BY event_sequence DESC LIMIT 1) NOT IN ('released','aborted')
              THEN RAISE(ABORT,'another warehouse domain epoch is active') END;
            SELECT CASE WHEN EXISTS(SELECT 1 FROM {EVENTS_TABLE})
              AND NEW.epoch_id<>(SELECT epoch_id FROM {EVENTS_TABLE}
                                 ORDER BY event_sequence DESC LIMIT 1)
              AND NEW.phase<>'held'
              THEN RAISE(ABORT,'new warehouse domain epoch must start held') END;
            SELECT CASE WHEN EXISTS(
                SELECT 1 FROM {EVENTS_TABLE}
                WHERE epoch_id=NEW.epoch_id
                  AND (manifest_digest<>NEW.manifest_digest OR deployed_sha<>NEW.deployed_sha)
              ) THEN RAISE(ABORT,'warehouse domain epoch identity drift') END;
            SELECT CASE WHEN NEW.epoch_id=(SELECT epoch_id FROM {EVENTS_TABLE}
                                           ORDER BY event_sequence DESC LIMIT 1)
              AND NOT (
                ((SELECT phase FROM {EVENTS_TABLE} ORDER BY event_sequence DESC LIMIT 1)='held'
                  AND NEW.phase IN ('applying','aborted'))
                OR ((SELECT phase FROM {EVENTS_TABLE} ORDER BY event_sequence DESC LIMIT 1)='applying'
                  AND NEW.phase IN ('readback_required','recovery_required'))
                OR ((SELECT phase FROM {EVENTS_TABLE} ORDER BY event_sequence DESC LIMIT 1)='readback_required'
                  AND NEW.phase IN ('reconciled','recovery_required'))
                OR ((SELECT phase FROM {EVENTS_TABLE} ORDER BY event_sequence DESC LIMIT 1)='recovery_required'
                  AND NEW.phase='recovery_applying')
                OR ((SELECT phase FROM {EVENTS_TABLE} ORDER BY event_sequence DESC LIMIT 1)='recovery_applying'
                  AND NEW.phase='recovery_readback_required')
                OR ((SELECT phase FROM {EVENTS_TABLE} ORDER BY event_sequence DESC LIMIT 1)='recovery_readback_required'
                  AND NEW.phase IN ('reconciled','recovery_required'))
                OR ((SELECT phase FROM {EVENTS_TABLE} ORDER BY event_sequence DESC LIMIT 1)='reconciled'
                  AND NEW.phase='released')
              )
              THEN RAISE(ABORT,'invalid warehouse domain epoch transition') END;
        END;
        CREATE TRIGGER IF NOT EXISTS warehouse_domain_write_epoch_no_update
        BEFORE UPDATE ON {EVENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'warehouse domain epoch events are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS warehouse_domain_write_epoch_no_delete
        BEFORE DELETE ON {EVENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'warehouse domain epoch events are append-only'); END;
        """
    )
    install_warehouse_domain_table_guards(conn)


def install_warehouse_domain_table_guards(conn: sqlite3.Connection) -> None:
    """Attach fail-closed triggers to every currently materialized writer table."""

    existing = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    phases = _sql_values(ACTIVE_BLOCKING_PHASES)
    for table in WAREHOUSE_DOMAIN_TABLES:
        if table not in existing:
            continue
        suffix = table.removeprefix("sheet_vitrina_v1_")
        for action in ("INSERT", "UPDATE", "DELETE"):
            trigger = f"warehouse_domain_guard_{suffix}_{action.lower()}"
            conn.execute(
                f"""CREATE TRIGGER IF NOT EXISTS {trigger}
                    BEFORE {action} ON {table}
                    WHEN COALESCE(
                        (SELECT phase FROM {EVENTS_TABLE}
                         ORDER BY event_sequence DESC LIMIT 1),
                        'released'
                    ) IN ({phases})
                    BEGIN
                        SELECT RAISE(ABORT,'warehouse domain write barrier active');
                    END"""
            )


def warehouse_domain_write_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return bounded current epoch status without mutating schema or rows."""

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if EVENTS_TABLE not in tables:
        return {
            "contract_name": CONTRACT_NAME,
            "status": "schema_absent_default_open",
            "active": False,
            "phase": "absent",
            "epoch_id": "",
            "manifest_digest": "",
            "deployed_sha": "",
            "event_sequence": 0,
        }
    row = conn.execute(
        f"""SELECT event_sequence,epoch_id,phase,manifest_digest,deployed_sha,
                   event_at,actor,details_json
            FROM {EVENTS_TABLE} ORDER BY event_sequence DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        return {
            "contract_name": CONTRACT_NAME,
            "status": "inactive",
            "active": False,
            "phase": "absent",
            "epoch_id": "",
            "manifest_digest": "",
            "deployed_sha": "",
            "event_sequence": 0,
        }
    phase = str(row[2])
    try:
        details = json.loads(str(row[7] or "{}"))
    except json.JSONDecodeError:
        details = {}
    return {
        "contract_name": CONTRACT_NAME,
        "status": "active" if phase in ACTIVE_BLOCKING_PHASES else phase,
        "active": phase in ACTIVE_BLOCKING_PHASES,
        "phase": phase,
        "epoch_id": str(row[1]),
        "manifest_digest": str(row[3]),
        "deployed_sha": str(row[4]),
        "event_sequence": int(row[0]),
        "event_at": str(row[5]),
        "actor": str(row[6]),
        "details": details if isinstance(details, Mapping) else {},
    }


def assert_warehouse_domain_write_allowed(
    conn: sqlite3.Connection,
    *,
    writer: str,
) -> None:
    """Code-level guard for future writers without an owned SQL table yet."""

    status = warehouse_domain_write_status(conn)
    if status["active"]:
        raise WarehouseDomainWriteBlocked(
            f"warehouse domain write barrier active for {writer}: "
            f"{status['epoch_id']}:{status['phase']}"
        )


def _sql_values(values: tuple[str, ...]) -> str:
    return ",".join("'" + value.replace("'", "''") + "'" for value in values)
