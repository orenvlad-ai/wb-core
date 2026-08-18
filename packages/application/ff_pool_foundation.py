"""Inert facility/pool accounting foundation below the aggregate FF stage.

This module owns only additive schema and read-only feature/parity helpers.
It deliberately does not post documents, materialize balances, change the
canonical FF ledger, or publish warehouse/Vitrina values.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import sqlite3
from typing import Any, Iterable, Mapping

from packages.application.canonical_rub_money import (
    CANONICAL_RUB_MONEY_POLICY,
    compare_canonical_rub_money,
)
from packages.contracts.ff_pool_foundation import (
    FfPoolFeatureState,
    FfPoolParityResult,
)


CONTRACT_NAME = "ff_facility_pool_foundation"
CONTRACT_VERSION = 1
POOLS = ("FBS", "FBO")
RELATION_TYPES = ("correction_of", "storno_of", "late_expense_for")

FACILITIES_TABLE = "sheet_vitrina_v1_ff_facilities"
FACILITY_CHANGES_TABLE = "sheet_vitrina_v1_ff_facility_changes"
FACILITY_PROFILES_TABLE = "sheet_vitrina_v1_ff_facility_profiles"
OPERATIONS_TABLE = "sheet_vitrina_v1_warehouse_business_operations"
LINES_TABLE = "sheet_vitrina_v1_ff_pool_movement_lines"
RELATIONS_TABLE = "sheet_vitrina_v1_warehouse_business_operation_relations"
FEATURE_EPOCHS_TABLE = "sheet_vitrina_v1_ff_pool_feature_epochs"
BALANCES_TABLE = "sheet_vitrina_v1_ff_pool_balances"
PARITY_TABLE = "sheet_vitrina_v1_ff_pool_parity_diagnostics"

ZERO = Decimal("0")


def ensure_ff_pool_foundation_schema(conn: sqlite3.Connection) -> None:
    """Create only bounded, empty foundation tables/indexes/triggers."""

    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {FACILITIES_TABLE}(
            facility_id TEXT PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            active INTEGER NOT NULL CHECK(active IN (0,1)),
            display_timezone TEXT NOT NULL,
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            updated_at TEXT NOT NULL
                CHECK(substr(updated_at,-1,1)='Z' AND julianday(updated_at) IS NOT NULL),
            CHECK(length(trim(facility_id)) BETWEEN 1 AND 80),
            CHECK(length(trim(code)) BETWEEN 1 AND 80),
            CHECK(length(trim(name)) BETWEEN 1 AND 200),
            CHECK(length(trim(display_timezone)) BETWEEN 1 AND 100)
        );
        CREATE INDEX IF NOT EXISTS ff_facilities_by_active_code
        ON {FACILITIES_TABLE}(active,code);

        CREATE TABLE IF NOT EXISTS {FACILITY_PROFILES_TABLE}(
            facility_id TEXT PRIMARY KEY REFERENCES {FACILITIES_TABLE}(facility_id),
            city TEXT NOT NULL DEFAULT '',
            future_fields_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(future_fields_json)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(length(trim(city)) <= 120)
        );
        CREATE INDEX IF NOT EXISTS ff_facility_profiles_by_city
        ON {FACILITY_PROFILES_TABLE}(city COLLATE NOCASE,facility_id);

        CREATE TABLE IF NOT EXISTS {FACILITY_CHANGES_TABLE}(
            change_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            request_identity TEXT NOT NULL,
            facility_id TEXT NOT NULL REFERENCES {FACILITIES_TABLE}(facility_id),
            action TEXT NOT NULL CHECK(action IN ('created','renamed','activated','deactivated','timezone_changed')),
            actor TEXT NOT NULL,
            previous_json TEXT NOT NULL DEFAULT '{{}}',
            current_json TEXT NOT NULL,
            changed_at TEXT NOT NULL
                CHECK(substr(changed_at,-1,1)='Z' AND julianday(changed_at) IS NOT NULL),
            CHECK(length(trim(change_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(request_id)) BETWEEN 8 AND 120),
            CHECK(length(trim(request_identity)) BETWEEN 1 AND 80),
            UNIQUE(request_id,action),
            CHECK(length(trim(actor)) BETWEEN 1 AND 160)
        );
        CREATE INDEX IF NOT EXISTS ff_facility_changes_by_facility_time
        ON {FACILITY_CHANGES_TABLE}(facility_id,changed_at DESC,change_id DESC);

        CREATE TABLE IF NOT EXISTS {OPERATIONS_TABLE}(
            operation_id TEXT PRIMARY KEY,
            operation_type TEXT NOT NULL,
            source_system TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            idempotency_epoch INTEGER NOT NULL
                CHECK(typeof(idempotency_epoch)='integer' AND idempotency_epoch > 0),
            business_date TEXT NOT NULL
                CHECK(length(business_date)=10 AND date(business_date)=business_date),
            posted_at TEXT NOT NULL
                CHECK(substr(posted_at,-1,1)='Z' AND julianday(posted_at) IS NOT NULL),
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            CHECK(length(trim(operation_id)) BETWEEN 1 AND 120),
            CHECK(length(trim(operation_type)) BETWEEN 1 AND 80),
            CHECK(length(trim(source_system)) BETWEEN 1 AND 80),
            CHECK(length(trim(source_type)) BETWEEN 1 AND 80),
            CHECK(length(trim(source_id)) BETWEEN 1 AND 240),
            CHECK(length(trim(source_revision)) BETWEEN 1 AND 240),
            UNIQUE(source_system,source_type,source_id,source_revision,idempotency_epoch)
        );
        CREATE INDEX IF NOT EXISTS warehouse_business_operations_by_date
        ON {OPERATIONS_TABLE}(business_date,operation_id);
        CREATE INDEX IF NOT EXISTS warehouse_business_operations_by_source
        ON {OPERATIONS_TABLE}(source_system,source_type,source_id,idempotency_epoch);

        CREATE TABLE IF NOT EXISTS {LINES_TABLE}(
            operation_id TEXT NOT NULL
                REFERENCES {OPERATIONS_TABLE}(operation_id),
            line_no INTEGER NOT NULL
                CHECK(typeof(line_no)='integer' AND line_no > 0),
            facility_id TEXT NOT NULL
                REFERENCES {FACILITIES_TABLE}(facility_id),
            pool TEXT NOT NULL CHECK(pool IN ('FBS','FBO')),
            nm_id INTEGER NOT NULL
                CHECK(typeof(nm_id)='integer' AND nm_id > 0),
            quantity_delta INTEGER NOT NULL
                CHECK(typeof(quantity_delta)='integer'),
            capital_delta_rub TEXT NOT NULL CHECK(
                typeof(capital_delta_rub)='text'
                AND length(capital_delta_rub) BETWEEN 1 AND 80
                AND capital_delta_rub NOT GLOB '*[^0-9.-]*'
                AND instr(substr(capital_delta_rub,2),'-')=0
                AND length(capital_delta_rub)-length(replace(capital_delta_rub,'.','')) <= 1
                AND capital_delta_rub NOT IN ('','-','.','-.')
                AND substr(capital_delta_rub,-1,1) <> '.'
                AND (
                    substr(capital_delta_rub,1,1) BETWEEN '0' AND '9'
                    OR (
                        substr(capital_delta_rub,1,1)='-'
                        AND substr(capital_delta_rub,2,1) BETWEEN '0' AND '9'
                    )
                )
            ),
            wac_snapshot_rub TEXT CHECK(
                wac_snapshot_rub IS NULL OR (
                    typeof(wac_snapshot_rub)='text'
                    AND length(wac_snapshot_rub) BETWEEN 1 AND 80
                    AND wac_snapshot_rub NOT GLOB '*[^0-9.-]*'
                    AND instr(substr(wac_snapshot_rub,2),'-')=0
                    AND length(wac_snapshot_rub)-length(replace(wac_snapshot_rub,'.','')) <= 1
                    AND wac_snapshot_rub NOT IN ('','-','.','-.')
                    AND substr(wac_snapshot_rub,-1,1) <> '.'
                    AND (
                        substr(wac_snapshot_rub,1,1) BETWEEN '0' AND '9'
                        OR (
                            substr(wac_snapshot_rub,1,1)='-'
                            AND substr(wac_snapshot_rub,2,1) BETWEEN '0' AND '9'
                        )
                    )
                )
            ),
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            PRIMARY KEY(operation_id,line_no)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_movement_lines_by_balance_key
        ON {LINES_TABLE}(facility_id,pool,nm_id,operation_id,line_no);
        CREATE INDEX IF NOT EXISTS ff_pool_movement_lines_by_nm
        ON {LINES_TABLE}(nm_id,facility_id,pool,operation_id);

        CREATE TABLE IF NOT EXISTS {RELATIONS_TABLE}(
            parent_id TEXT NOT NULL REFERENCES {OPERATIONS_TABLE}(operation_id),
            child_id TEXT NOT NULL REFERENCES {OPERATIONS_TABLE}(operation_id),
            relation_type TEXT NOT NULL
                CHECK(relation_type IN ('correction_of','storno_of','late_expense_for')),
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            PRIMARY KEY(parent_id,child_id,relation_type),
            UNIQUE(child_id,relation_type),
            CHECK(parent_id <> child_id)
        );
        CREATE INDEX IF NOT EXISTS warehouse_business_relations_by_parent
        ON {RELATIONS_TABLE}(parent_id,relation_type,child_id);

        CREATE TABLE IF NOT EXISTS {FEATURE_EPOCHS_TABLE}(
            epoch INTEGER PRIMARY KEY
                CHECK(typeof(epoch)='integer' AND epoch > 0),
            writer_enabled INTEGER NOT NULL CHECK(writer_enabled IN (0,1)),
            reader_enabled INTEGER NOT NULL CHECK(reader_enabled IN (0,1)),
            source_revision TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL),
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            CHECK(reader_enabled <= writer_enabled)
        );

        CREATE TABLE IF NOT EXISTS {BALANCES_TABLE}(
            facility_id TEXT NOT NULL REFERENCES {FACILITIES_TABLE}(facility_id),
            pool TEXT NOT NULL CHECK(pool IN ('FBS','FBO')),
            nm_id INTEGER NOT NULL
                CHECK(typeof(nm_id)='integer' AND nm_id > 0),
            projection_epoch INTEGER NOT NULL
                REFERENCES {FEATURE_EPOCHS_TABLE}(epoch),
            quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer'),
            capital_rub TEXT NOT NULL CHECK(
                typeof(capital_rub)='text'
                AND length(capital_rub) BETWEEN 1 AND 80
                AND capital_rub NOT GLOB '*[^0-9.-]*'
                AND instr(substr(capital_rub,2),'-')=0
                AND length(capital_rub)-length(replace(capital_rub,'.','')) <= 1
                AND capital_rub NOT IN ('','-','.','-.')
                AND substr(capital_rub,-1,1) <> '.'
                AND (
                    substr(capital_rub,1,1) BETWEEN '0' AND '9'
                    OR (
                        substr(capital_rub,1,1)='-'
                        AND substr(capital_rub,2,1) BETWEEN '0' AND '9'
                    )
                )
            ),
            wac_rub TEXT CHECK(
                wac_rub IS NULL OR (
                    typeof(wac_rub)='text'
                    AND length(wac_rub) BETWEEN 1 AND 80
                    AND wac_rub NOT GLOB '*[^0-9.-]*'
                    AND instr(substr(wac_rub,2),'-')=0
                    AND length(wac_rub)-length(replace(wac_rub,'.','')) <= 1
                    AND wac_rub NOT IN ('','-','.','-.')
                    AND substr(wac_rub,-1,1) <> '.'
                    AND (
                        substr(wac_rub,1,1) BETWEEN '0' AND '9'
                        OR (
                            substr(wac_rub,1,1)='-'
                            AND substr(wac_rub,2,1) BETWEEN '0' AND '9'
                        )
                    )
                )
            ),
            source_watermark TEXT NOT NULL,
            updated_at TEXT NOT NULL
                CHECK(substr(updated_at,-1,1)='Z' AND julianday(updated_at) IS NOT NULL),
            PRIMARY KEY(facility_id,pool,nm_id)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_balances_by_pool_nm
        ON {BALANCES_TABLE}(pool,nm_id,facility_id);
        CREATE INDEX IF NOT EXISTS ff_pool_balances_by_epoch_key
        ON {BALANCES_TABLE}(projection_epoch,facility_id,pool,nm_id);

        CREATE TABLE IF NOT EXISTS {PARITY_TABLE}(
            diagnostic_id TEXT PRIMARY KEY,
            feature_epoch INTEGER NOT NULL
                REFERENCES {FEATURE_EPOCHS_TABLE}(epoch),
            aggregate_revision TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pass','mismatch')),
            detail_row_count INTEGER NOT NULL
                CHECK(typeof(detail_row_count)='integer' AND detail_row_count > 0),
            aggregate_row_count INTEGER NOT NULL
                CHECK(typeof(aggregate_row_count)='integer' AND aggregate_row_count >= 0),
            detail_quantity INTEGER NOT NULL CHECK(typeof(detail_quantity)='integer'),
            aggregate_quantity INTEGER NOT NULL CHECK(typeof(aggregate_quantity)='integer'),
            detail_capital_rub TEXT NOT NULL CHECK(
                typeof(detail_capital_rub)='text'
                AND length(detail_capital_rub) BETWEEN 1 AND 80
                AND detail_capital_rub NOT GLOB '*[^0-9.-]*'
                AND instr(substr(detail_capital_rub,2),'-')=0
                AND length(detail_capital_rub)-length(replace(detail_capital_rub,'.','')) <= 1
                AND detail_capital_rub NOT IN ('','-','.','-.')
                AND substr(detail_capital_rub,-1,1) <> '.'
                AND (
                    substr(detail_capital_rub,1,1) BETWEEN '0' AND '9'
                    OR (
                        substr(detail_capital_rub,1,1)='-'
                        AND substr(detail_capital_rub,2,1) BETWEEN '0' AND '9'
                    )
                )
            ),
            aggregate_capital_rub TEXT NOT NULL CHECK(
                typeof(aggregate_capital_rub)='text'
                AND length(aggregate_capital_rub) BETWEEN 1 AND 80
                AND aggregate_capital_rub NOT GLOB '*[^0-9.-]*'
                AND instr(substr(aggregate_capital_rub,2),'-')=0
                AND length(aggregate_capital_rub)-length(replace(aggregate_capital_rub,'.','')) <= 1
                AND aggregate_capital_rub NOT IN ('','-','.','-.')
                AND substr(aggregate_capital_rub,-1,1) <> '.'
                AND (
                    substr(aggregate_capital_rub,1,1) BETWEEN '0' AND '9'
                    OR (
                        substr(aggregate_capital_rub,1,1)='-'
                        AND substr(aggregate_capital_rub,2,1) BETWEEN '0' AND '9'
                    )
                )
            ),
            detail_fingerprint TEXT NOT NULL,
            aggregate_fingerprint TEXT NOT NULL,
            mismatched_nm_ids_json TEXT NOT NULL,
            checked_at TEXT NOT NULL
                CHECK(substr(checked_at,-1,1)='Z' AND julianday(checked_at) IS NOT NULL),
            details_json TEXT NOT NULL DEFAULT '{{}}'
        );
        CREATE INDEX IF NOT EXISTS ff_pool_parity_by_epoch_time
        ON {PARITY_TABLE}(feature_epoch,checked_at DESC,diagnostic_id DESC);

        CREATE TRIGGER IF NOT EXISTS ff_facilities_stable_identity
        BEFORE UPDATE ON {FACILITIES_TABLE}
        WHEN NEW.facility_id <> OLD.facility_id OR NEW.code <> OLD.code
        BEGIN
            SELECT RAISE(ABORT,'ff facility identity is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS ff_facilities_no_delete
        BEFORE DELETE ON {FACILITIES_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'ff facilities are retained');
        END;
        CREATE TRIGGER IF NOT EXISTS ff_facility_changes_no_update
        BEFORE UPDATE ON {FACILITY_CHANGES_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'ff facility audit is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS ff_facility_changes_no_delete
        BEFORE DELETE ON {FACILITY_CHANGES_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'ff facility audit is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS warehouse_business_operations_no_update
        BEFORE UPDATE ON {OPERATIONS_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'posted warehouse operation is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS warehouse_business_operations_no_delete
        BEFORE DELETE ON {OPERATIONS_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'posted warehouse operation is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_movement_lines_no_update
        BEFORE UPDATE ON {LINES_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'posted pool movement line is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_movement_lines_no_delete
        BEFORE DELETE ON {LINES_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'posted pool movement line is append-only');
        END;

        CREATE TRIGGER IF NOT EXISTS warehouse_business_relation_forward
        BEFORE INSERT ON {RELATIONS_TABLE}
        BEGIN
            SELECT CASE WHEN (
                SELECT julianday(posted_at) FROM {OPERATIONS_TABLE}
                WHERE operation_id=NEW.parent_id
            ) > (
                SELECT julianday(posted_at) FROM {OPERATIONS_TABLE}
                WHERE operation_id=NEW.child_id
            ) THEN RAISE(ABORT,'warehouse relation must point forward') END;
            SELECT CASE WHEN NOT EXISTS(
                SELECT 1 FROM {OPERATIONS_TABLE} AS child
                WHERE child.operation_id=NEW.child_id
                  AND child.operation_type=CASE NEW.relation_type
                    WHEN 'correction_of' THEN 'correction'
                    WHEN 'storno_of' THEN 'storno'
                    WHEN 'late_expense_for' THEN 'late_expense'
                  END
            ) THEN RAISE(ABORT,'warehouse relation child type mismatch') END;
        END;
        CREATE TRIGGER IF NOT EXISTS warehouse_business_relation_no_cycle
        BEFORE INSERT ON {RELATIONS_TABLE}
        BEGIN
            SELECT CASE WHEN EXISTS(
                WITH RECURSIVE descendants(operation_id) AS (
                    SELECT child_id FROM {RELATIONS_TABLE}
                    WHERE parent_id=NEW.child_id
                    UNION
                    SELECT relation.child_id
                    FROM {RELATIONS_TABLE} AS relation
                    JOIN descendants
                      ON relation.parent_id=descendants.operation_id
                )
                SELECT 1 FROM descendants WHERE operation_id=NEW.parent_id
            ) THEN RAISE(ABORT,'warehouse operation relation cycle') END;
        END;
        CREATE TRIGGER IF NOT EXISTS warehouse_business_relations_no_update
        BEFORE UPDATE ON {RELATIONS_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'warehouse operation relation is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS warehouse_business_relations_no_delete
        BEFORE DELETE ON {RELATIONS_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'warehouse operation relation is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_feature_epochs_no_update
        BEFORE UPDATE ON {FEATURE_EPOCHS_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'ff pool feature epoch is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_feature_epochs_no_delete
        BEFORE DELETE ON {FEATURE_EPOCHS_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'ff pool feature epoch is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_parity_no_update
        BEFORE UPDATE ON {PARITY_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'ff pool parity diagnostic is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_parity_no_delete
        BEFORE DELETE ON {PARITY_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'ff pool parity diagnostic is append-only');
        END;
        """
    )


def canonical_decimal_text(value: Any) -> str:
    """Return a finite, non-exponent Decimal string for SQLite TEXT storage."""

    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid Decimal value: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError("Decimal value must be finite")
    if amount == ZERO:
        return "0"
    return format(amount, "f")


def read_ff_pool_feature_state(
    conn: sqlite3.Connection,
    *,
    aggregate_revision: str = "",
) -> FfPoolFeatureState:
    """Resolve the latest configuration; absence is the feature-off default."""

    row = _latest_feature_epoch(conn)
    if row is None:
        return FfPoolFeatureState(
            epoch=0,
            writer_configured=False,
            reader_configured=False,
            writer_effective=False,
            reader_effective=False,
            parity_status="not_evaluated",
            reason="feature_epoch_absent_default_off",
        )
    epoch, writer_enabled, reader_enabled = int(row[0]), bool(row[1]), bool(row[2])
    diagnostic = conn.execute(
        f"""SELECT status,aggregate_revision,detail_fingerprint
            FROM {PARITY_TABLE}
            WHERE feature_epoch=?
            ORDER BY checked_at DESC,diagnostic_id DESC LIMIT 1""",
        (epoch,),
    ).fetchone()
    parity_status = str(diagnostic[0]) if diagnostic is not None else "not_evaluated"
    revision_matches = bool(
        diagnostic is not None
        and aggregate_revision
        and str(diagnostic[1]) == aggregate_revision
    )
    detail_matches = bool(
        diagnostic is not None
        and str(diagnostic[2]) == _current_detail_fingerprint(conn, epoch)
    )
    reader_effective = bool(
        reader_enabled
        and writer_enabled
        and parity_status == "pass"
        and revision_matches
        and detail_matches
    )
    if not writer_enabled:
        reason = "feature_epoch_writer_off"
    elif not reader_enabled:
        reason = "reader_not_configured"
    elif parity_status == "pass" and not aggregate_revision:
        reason = "current_aggregate_revision_required_fail_closed"
    elif parity_status == "pass" and not revision_matches:
        reason = "current_aggregate_revision_drift_fail_closed"
    elif parity_status == "pass" and not detail_matches:
        reason = "current_detail_projection_drift_fail_closed"
    elif parity_status == "pass":
        reason = "current_epoch_parity_passed"
    elif parity_status == "mismatch":
        reason = "current_epoch_parity_mismatch_fail_closed"
    else:
        reason = "current_epoch_parity_not_proven_fail_closed"
    return FfPoolFeatureState(
        epoch=epoch,
        writer_configured=writer_enabled,
        reader_configured=reader_enabled,
        writer_effective=writer_enabled,
        reader_effective=reader_effective,
        parity_status=parity_status,
        reason=reason,
    )


def evaluate_ff_pool_aggregate_parity(
    conn: sqlite3.Connection,
    aggregate_rows: Iterable[Mapping[str, Any]],
) -> FfPoolParityResult:
    """Compare current-epoch detail to caller-owned aggregate FF readback.

    The function is query-only.  It never updates the aggregate FF balance or
    any public projection, including on mismatch.
    """

    epoch_row = _latest_feature_epoch(conn)
    if epoch_row is None:
        return _parity_result(status="feature_off", feature_epoch=0)
    if not bool(epoch_row[1]):
        return _parity_result(status="feature_off", feature_epoch=int(epoch_row[0]))
    epoch = int(epoch_row[0])
    reader_configured = bool(epoch_row[2])
    detail_rows = conn.execute(
        f"""SELECT facility_id,pool,nm_id,quantity,capital_rub
            FROM {BALANCES_TABLE}
            WHERE projection_epoch=?
            ORDER BY nm_id,facility_id,pool""",
        (epoch,),
    ).fetchall()
    if not detail_rows:
        return _parity_result(status="detail_empty", feature_epoch=epoch)

    # Capital may contain an authoritative fractional-kopeck tail with up to
    # 80 stored characters.  Preserve it exactly for audit/recovery, but gate
    # operational parity at the centralized canonical RUB minor-unit boundary.
    with localcontext() as context:
        context.prec = 160
        detail_by_nm: dict[int, tuple[int, Decimal]] = {}
        for row in detail_rows:
            nm_id = _exact_integer(row[2], field_name="detail nm_id", positive=True)
            quantity = _exact_integer(row[3], field_name="detail quantity")
            capital = _decimal(row[4], field_name="detail capital_rub")
            prior_quantity, prior_capital = detail_by_nm.get(nm_id, (0, ZERO))
            detail_by_nm[nm_id] = (
                prior_quantity + quantity,
                prior_capital + capital,
            )

        aggregate_by_nm: dict[int, tuple[int, Decimal]] = {}
        for item in aggregate_rows:
            nm_id = _exact_integer(
                item.get("nm_id"), field_name="aggregate nm_id", positive=True
            )
            if nm_id in aggregate_by_nm:
                raise ValueError(f"duplicate aggregate FF nm_id: {nm_id}")
            aggregate_by_nm[nm_id] = (
                _exact_integer(item.get("quantity"), field_name="aggregate quantity"),
                _decimal(item.get("capital_rub"), field_name="aggregate capital_rub"),
            )

        all_nm_ids = sorted(set(detail_by_nm) | set(aggregate_by_nm))
        quantity_mismatches: list[int] = []
        canonical_capital_mismatches: list[int] = []
        raw_capital_mismatches: list[int] = []
        raw_residual_by_nm: dict[int, Decimal] = {}
        for nm_id in all_nm_ids:
            detail_quantity_for_nm, detail_capital_for_nm = detail_by_nm.get(
                nm_id, (0, ZERO)
            )
            aggregate_quantity_for_nm, aggregate_capital_for_nm = aggregate_by_nm.get(
                nm_id, (0, ZERO)
            )
            if detail_quantity_for_nm != aggregate_quantity_for_nm:
                quantity_mismatches.append(nm_id)
            comparison = compare_canonical_rub_money(
                detail_capital_for_nm,
                aggregate_capital_for_nm,
                left_field=f"detail capital_rub for nm_id {nm_id}",
                right_field=f"aggregate capital_rub for nm_id {nm_id}",
            )
            raw_residual_by_nm[nm_id] = comparison.raw_residual_rub
            if comparison.raw_residual_rub != ZERO:
                raw_capital_mismatches.append(nm_id)
            if not comparison.canonical_equal or not comparison.residual_attributable:
                canonical_capital_mismatches.append(nm_id)
        detail_quantity = sum(item[0] for item in detail_by_nm.values())
        aggregate_quantity = sum(item[0] for item in aggregate_by_nm.values())
        detail_capital = sum((item[1] for item in detail_by_nm.values()), ZERO)
        aggregate_capital = sum((item[1] for item in aggregate_by_nm.values()), ZERO)
        total_comparison = compare_canonical_rub_money(
            detail_capital,
            aggregate_capital,
            left_field="detail total capital_rub",
            right_field="aggregate total capital_rub",
        )
        attributed_residual = sum(raw_residual_by_nm.values(), ZERO)
        raw_residual_conserved = (
            attributed_residual == total_comparison.raw_residual_rub
        )
        total_boundary_failed = (
            not total_comparison.canonical_equal
            or not total_comparison.residual_attributable
            or not raw_residual_conserved
        )
        blocking_nm_ids = set(quantity_mismatches) | set(
            canonical_capital_mismatches
        )
        if total_boundary_failed:
            blocking_nm_ids.update(raw_capital_mismatches or all_nm_ids)
        mismatches = tuple(sorted(blocking_nm_ids))
    status = "mismatch" if mismatches else "pass"
    detail_fingerprint = _current_detail_fingerprint(conn, epoch)
    aggregate_fingerprint = _fingerprint(
        [
            {
                "nm_id": nm_id,
                "quantity": quantity,
                "capital_rub": canonical_decimal_text(capital),
            }
            for nm_id, (quantity, capital) in sorted(aggregate_by_nm.items())
        ]
    )
    return FfPoolParityResult(
        status=status,
        feature_epoch=epoch,
        detail_row_count=len(detail_rows),
        aggregate_row_count=len(aggregate_by_nm),
        detail_quantity=detail_quantity,
        aggregate_quantity=aggregate_quantity,
        detail_capital_rub=detail_capital,
        aggregate_capital_rub=aggregate_capital,
        mismatched_nm_ids=mismatches,
        quantity_mismatched_nm_ids=tuple(quantity_mismatches),
        canonical_capital_mismatched_nm_ids=tuple(
            canonical_capital_mismatches
        ),
        raw_capital_mismatched_nm_ids=tuple(raw_capital_mismatches),
        raw_capital_residuals_by_nm=tuple(
            (nm_id, raw_residual_by_nm[nm_id])
            for nm_id in raw_capital_mismatches
        ),
        detail_canonical_capital_minor_units=total_comparison.left_minor_units,
        aggregate_canonical_capital_minor_units=total_comparison.right_minor_units,
        raw_capital_residual_rub=total_comparison.raw_residual_rub,
        raw_residual_conserved=raw_residual_conserved,
        money_parity_policy=CANONICAL_RUB_MONEY_POLICY,
        detail_fingerprint=detail_fingerprint,
        aggregate_fingerprint=aggregate_fingerprint,
        fail_closed=bool(mismatches),
        reader_allowed=bool(reader_configured and not mismatches),
        aggregate_unchanged=True,
    )


def record_ff_pool_parity_diagnostic(
    conn: sqlite3.Connection,
    *,
    diagnostic_id: str,
    aggregate_revision: str,
    checked_at: str,
    result: FfPoolParityResult,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Append a proven pass/mismatch diagnostic for the current feature epoch."""

    if result.status not in {"pass", "mismatch"} or result.feature_epoch <= 0:
        raise ValueError("only evaluated current-epoch parity may be recorded")
    _require_utc_timestamp(checked_at)
    epoch_row = _latest_feature_epoch(conn)
    if epoch_row is None or int(epoch_row[0]) != result.feature_epoch:
        raise ValueError("parity result feature epoch is no longer current")
    if _current_detail_fingerprint(conn, result.feature_epoch) != result.detail_fingerprint:
        raise ValueError("pool detail changed after parity evaluation")
    conn.execute(
        f"""INSERT INTO {PARITY_TABLE}(
                diagnostic_id,feature_epoch,aggregate_revision,status,
                detail_row_count,aggregate_row_count,detail_quantity,
                aggregate_quantity,detail_capital_rub,aggregate_capital_rub,
                detail_fingerprint,aggregate_fingerprint,
                mismatched_nm_ids_json,checked_at,details_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(diagnostic_id),
            result.feature_epoch,
            str(aggregate_revision),
            result.status,
            result.detail_row_count,
            result.aggregate_row_count,
            result.detail_quantity,
            result.aggregate_quantity,
            canonical_decimal_text(result.detail_capital_rub),
            canonical_decimal_text(result.aggregate_capital_rub),
            result.detail_fingerprint,
            result.aggregate_fingerprint,
            json.dumps(list(result.mismatched_nm_ids), separators=(",", ":")),
            checked_at,
            json.dumps(
                {
                    **dict(details or {}),
                    "money_parity_policy": result.money_parity_policy,
                    "quantity_mismatched_nm_ids": list(
                        result.quantity_mismatched_nm_ids
                    ),
                    "canonical_capital_mismatched_nm_ids": list(
                        result.canonical_capital_mismatched_nm_ids
                    ),
                    "raw_capital_mismatched_nm_ids": list(
                        result.raw_capital_mismatched_nm_ids
                    ),
                    "raw_capital_residuals_by_nm": {
                        str(nm_id): canonical_decimal_text(residual)
                        for nm_id, residual in result.raw_capital_residuals_by_nm
                    },
                    "detail_canonical_capital_minor_units": (
                        result.detail_canonical_capital_minor_units
                    ),
                    "aggregate_canonical_capital_minor_units": (
                        result.aggregate_canonical_capital_minor_units
                    ),
                    "raw_capital_residual_rub": canonical_decimal_text(
                        result.raw_capital_residual_rub
                    ),
                    "raw_residual_conserved": result.raw_residual_conserved,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )


def _latest_feature_epoch(conn: sqlite3.Connection) -> tuple[Any, ...] | None:
    return conn.execute(
        f"""SELECT epoch,writer_enabled,reader_enabled
            FROM {FEATURE_EPOCHS_TABLE} ORDER BY epoch DESC LIMIT 1"""
    ).fetchone()


def _parity_result(*, status: str, feature_epoch: int) -> FfPoolParityResult:
    return FfPoolParityResult(
        status=status,  # type: ignore[arg-type]
        feature_epoch=feature_epoch,
        detail_row_count=0,
        aggregate_row_count=0,
        detail_quantity=0,
        aggregate_quantity=0,
        detail_capital_rub=ZERO,
        aggregate_capital_rub=ZERO,
        mismatched_nm_ids=(),
        quantity_mismatched_nm_ids=(),
        canonical_capital_mismatched_nm_ids=(),
        raw_capital_mismatched_nm_ids=(),
        raw_capital_residuals_by_nm=(),
        detail_canonical_capital_minor_units=0,
        aggregate_canonical_capital_minor_units=0,
        raw_capital_residual_rub=ZERO,
        raw_residual_conserved=True,
        money_parity_policy=CANONICAL_RUB_MONEY_POLICY,
        detail_fingerprint="",
        aggregate_fingerprint="",
        fail_closed=False,
        reader_allowed=False,
        aggregate_unchanged=True,
    )


def _exact_integer(value: Any, *, field_name: str, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an exact integer")
    if isinstance(value, int):
        result = value
    else:
        text = str(value or "").strip()
        if not text or text.lstrip("-").isdigit() is False:
            raise ValueError(f"{field_name} must be an exact integer")
        result = int(text)
    if positive and result <= 0:
        raise ValueError(f"{field_name} must be positive")
    return result


def _decimal(value: Any, *, field_name: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be Decimal-safe") from exc
    if not amount.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return amount


def _require_utc_timestamp(value: str) -> None:
    if not value.endswith("Z"):
        raise ValueError("timestamp must be persisted in UTC with Z suffix")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be valid ISO 8601 UTC") from exc


def _current_detail_fingerprint(conn: sqlite3.Connection, epoch: int) -> str:
    rows = conn.execute(
        f"""SELECT facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                   wac_rub,source_watermark,updated_at
            FROM {BALANCES_TABLE}
            WHERE projection_epoch=?
            ORDER BY facility_id,pool,nm_id""",
        (epoch,),
    ).fetchall()
    return _fingerprint([list(row) for row in rows])


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
