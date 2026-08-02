"""Business-time warehouse/product-capital projection for Web Vitrina.

The functional warehouse remains the calculator.  This module only publishes a
bounded, immutable revision of its owned product-capital metrics and exposes a
read-time overlay for persisted Web Vitrina snapshots.  Source rows and
unrelated Vitrina metrics are never written here.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import sqlite3
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (
    OWN_AVG_COST_RUB_METRIC_KEY,
    OWN_AVG_COST_RUB_TOTAL_METRIC_KEY,
    OWN_PRODUCT_CAPITAL_METRIC_KEYS,
    OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS,
    OWN_PRODUCT_CAPITAL_STAGES,
    OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS,
    OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
    OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY,
    OWN_TOTAL_QTY_METRIC_KEY,
    OWN_TOTAL_QTY_TOTAL_METRIC_KEY,
    own_stage_metric_key,
    own_stage_total_metric_key,
)
from packages.business_time import business_date_from_timestamp
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaWriteTarget,
)


CONTRACT_NAME = "warehouse_business_projection"
CONTRACT_VERSION = 1
REVISION_TABLE = "sheet_vitrina_v1_warehouse_business_projection_revisions"
ROW_TABLE = "sheet_vitrina_v1_warehouse_business_projection_rows"
CURRENT_ROW_TABLE = "sheet_vitrina_v1_warehouse_business_projection_current_rows"
STATE_TABLE = "sheet_vitrina_v1_warehouse_business_projection_state"
OUTBOX_TABLE = "sheet_vitrina_v1_warehouse_business_projection_outbox"
DATA_SHEET_NAME = "DATA_VITRINA"
MAX_TARGET_DAYS = 366
ZERO = Decimal("0")

_FUNCTIONAL_TO_PUBLIC_STAGE = {
    "production": "PRODUCTION",
    "china_to_ff": "PRODUCTION_TO_FF",
    "ff": "FF",
    "ff_to_wb": "FF_TO_WB",
    "wb": "WB",
    "wb_acceptance_discrepancy": "WB_ACCEPTANCE_DISCREPANCY",
}
_SUPPLIER_STAGES = ("production", "china_to_ff")


class WarehouseBusinessProjectionError(RuntimeError):
    """A bounded projection revision could not be proven safe."""


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return deepcopy(value)
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return deepcopy(fallback)


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value))


def _number(value: Decimal) -> float:
    return float(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_date(value: Any, *, field_name: str) -> str:
    normalized = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise WarehouseBusinessProjectionError(
            f"{field_name} must be an exact YYYY-MM-DD business date"
        ) from exc


def _date_range(start: str, end: str) -> list[str]:
    left = date.fromisoformat(start)
    right = date.fromisoformat(end)
    count = (right - left).days + 1
    if count < 1:
        raise WarehouseBusinessProjectionError(
            "business projection end date is before its start date"
        )
    if count > MAX_TARGET_DAYS:
        raise WarehouseBusinessProjectionError(
            "bounded business projection exceeds "
            f"{MAX_TARGET_DAYS} dates: start={start}, end={end}"
        )
    return [(left + timedelta(days=offset)).isoformat() for offset in range(count)]


def _is_cost_only_outbox_request(request: Mapping[str, Any]) -> bool:
    source_kind = str(request.get("source_kind") or "").lower()
    stable_source_id = str(request.get("stable_source_id") or "").lower()
    return (
        "cost" in source_kind
        or "fee" in source_kind
        or "certification" in source_kind
        or source_kind == "supplier_cost_payment"
        or stable_source_id.startswith("functional_queue:wb_transit_cost:")
    )


def _bounded_outbox_target_dates(
    conn: sqlite3.Connection,
    *,
    business_effective_date: str,
    publication_business_date: str,
    cost_only: bool,
) -> tuple[list[str], dict[str, Any]]:
    """Keep late cost publication inside the active bounded projection surface."""

    requested_start = _iso_date(
        business_effective_date,
        field_name="business_effective_date",
    )
    target_end = max(
        requested_start,
        _iso_date(
            publication_business_date,
            field_name="publication_business_date",
        ),
    )
    requested_count = (
        date.fromisoformat(target_end) - date.fromisoformat(requested_start)
    ).days + 1
    if requested_count <= MAX_TARGET_DAYS or not cost_only:
        target_dates = _date_range(requested_start, target_end)
        return target_dates, {
            "requested_business_effective_date": requested_start,
            "applied_business_effective_date": requested_start,
            "requested_date_count": requested_count,
            "omitted_historical_date_count": 0,
            "scope_truncated": False,
            "scope_truncation_reason": None,
            "lower_bound_sources": {},
        }

    lower_bounds = {
        "maximum_bounded_window": (
            date.fromisoformat(target_end) - timedelta(days=MAX_TARGET_DAYS - 1)
        ).isoformat(),
    }
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "sheet_vitrina_v1_warehouse_functional_cutovers" in tables:
        cutover = conn.execute(
            """
            SELECT cutover_at
            FROM sheet_vitrina_v1_warehouse_functional_cutovers
            WHERE cutover_id='warehouse_functional_cutover_v1'
            """
        ).fetchone()
        if cutover is not None and str(cutover[0] or "")[:10]:
            cutover_date = _iso_date(
                str(cutover[0])[:10],
                field_name="functional_cutover",
            )
            if cutover_date <= target_end:
                lower_bounds["functional_cutover"] = cutover_date
    current_lower_bound = conn.execute(
        f"SELECT MIN(as_of_date) FROM {CURRENT_ROW_TABLE}"
    ).fetchone()
    if (
        current_lower_bound is not None
        and str(current_lower_bound[0] or "")[:10]
    ):
        active_surface_date = _iso_date(
            str(current_lower_bound[0])[:10],
            field_name="active_projection_surface",
        )
        if active_surface_date <= target_end:
            lower_bounds["active_projection_surface"] = active_surface_date
    applied_start = max(requested_start, *lower_bounds.values())
    target_dates = _date_range(applied_start, target_end)
    omitted_count = (
        date.fromisoformat(applied_start) - date.fromisoformat(requested_start)
    ).days
    return target_dates, {
        "requested_business_effective_date": requested_start,
        "applied_business_effective_date": applied_start,
        "requested_date_count": requested_count,
        "omitted_historical_date_count": omitted_count,
        "scope_truncated": True,
        "scope_truncation_reason": (
            "late_cost_outside_active_bounded_business_projection"
        ),
        "lower_bound_sources": lower_bounds,
    }


def ensure_warehouse_business_projection_schema(conn: sqlite3.Connection) -> None:
    existing_tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if {
        REVISION_TABLE,
        ROW_TABLE,
        CURRENT_ROW_TABLE,
        STATE_TABLE,
        OUTBOX_TABLE,
    }.issubset(existing_tables):
        return
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {REVISION_TABLE}(
            revision_id TEXT PRIMARY KEY,
            stable_source_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            business_effective_date TEXT NOT NULL,
            published_at TEXT NOT NULL,
            status TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL UNIQUE,
            base_version_id TEXT NOT NULL,
            published_version_id TEXT NOT NULL,
            affected_nm_ids_json TEXT NOT NULL,
            affected_dates_json TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            changed_row_count INTEGER NOT NULL,
            changed_cell_count INTEGER NOT NULL,
            diagnostics_json TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS warehouse_business_projection_revision_status
        ON {REVISION_TABLE}(status,published_at,revision_id);

        CREATE TABLE IF NOT EXISTS {ROW_TABLE}(
            revision_id TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            nm_id INTEGER NOT NULL,
            metrics_json TEXT NOT NULL,
            presentation_json TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            row_fingerprint TEXT NOT NULL,
            PRIMARY KEY(revision_id,as_of_date,nm_id)
        );
        CREATE INDEX IF NOT EXISTS warehouse_business_projection_rows_date
        ON {ROW_TABLE}(as_of_date,nm_id,revision_id);

        CREATE TABLE IF NOT EXISTS {CURRENT_ROW_TABLE}(
            as_of_date TEXT NOT NULL,
            nm_id INTEGER NOT NULL,
            revision_id TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            presentation_json TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            row_fingerprint TEXT NOT NULL,
            published_at TEXT NOT NULL,
            PRIMARY KEY(as_of_date,nm_id)
        );

        CREATE TABLE IF NOT EXISTS {STATE_TABLE}(
            slot INTEGER PRIMARY KEY CHECK(slot=1),
            revision_no INTEGER NOT NULL,
            revision_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            business_effective_date TEXT NOT NULL,
            published_at TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS {OUTBOX_TABLE}(
            request_id TEXT PRIMARY KEY,
            stable_source_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            business_effective_date TEXT NOT NULL,
            affected_nm_ids_json TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            error TEXT,
            UNIQUE(stable_source_id,source_revision)
        );
        CREATE INDEX IF NOT EXISTS warehouse_business_projection_outbox_status
        ON {OUTBOX_TABLE}(status,business_effective_date,requested_at);
        """
    )


def ensure_warehouse_projection_source_outbox(
    conn: sqlite3.Connection,
) -> None:
    """Attach durable outbox triggers to canonical capital evidence tables."""

    ensure_warehouse_business_projection_schema(conn)
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "sheet_vitrina_v1_own_capital_events" in tables:
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS warehouse_projection_own_capital_event
            AFTER INSERT ON sheet_vitrina_v1_own_capital_events
            BEGIN
              INSERT INTO {OUTBOX_TABLE}(
                request_id,stable_source_id,source_revision,
                business_effective_date,affected_nm_ids_json,source_kind,
                status,requested_at,started_at,finished_at,error
              ) VALUES(
                'whbpo_event_' || NEW.event_id,
                'own_capital_event:' || NEW.event_id,
                NEW.evidence_hash,
                NEW.effective_date,
                '[' || CAST(NEW.nm_id AS TEXT) || ']',
                NEW.event_type,
                'queued',
                NEW.created_at,
                NULL,NULL,NULL
              )
              ON CONFLICT(stable_source_id,source_revision) DO UPDATE SET
                business_effective_date=MIN(
                  business_effective_date,
                  excluded.business_effective_date
                ),
                affected_nm_ids_json=excluded.affected_nm_ids_json,
                status=CASE
                  WHEN status='complete' THEN status ELSE 'queued'
                END,
                requested_at=excluded.requested_at,
                error=NULL;
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS warehouse_projection_own_capital_event_delete
            AFTER DELETE ON sheet_vitrina_v1_own_capital_events
            BEGIN
              INSERT INTO {OUTBOX_TABLE}(
                request_id,stable_source_id,source_revision,
                business_effective_date,affected_nm_ids_json,source_kind,
                status,requested_at,started_at,finished_at,error
              ) VALUES(
                'whbpo_event_delete_' || OLD.event_id || '_' || OLD.evidence_hash,
                'own_capital_event:' || OLD.event_id,
                'deleted:' || OLD.evidence_hash,
                OLD.effective_date,
                '[' || CAST(OLD.nm_id AS TEXT) || ']',
                OLD.event_type || ':deleted',
                'queued',
                strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                NULL,NULL,NULL
              )
              ON CONFLICT(stable_source_id,source_revision) DO UPDATE SET
                business_effective_date=MIN(
                  business_effective_date,
                  excluded.business_effective_date
                ),
                affected_nm_ids_json=excluded.affected_nm_ids_json,
                status=CASE
                  WHEN status='complete' THEN status ELSE 'queued'
                END,
                requested_at=excluded.requested_at,
                error=NULL;
            END
            """
        )
    if "sheet_vitrina_v1_own_capital_expense_certifications" in tables:
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS warehouse_projection_expense_cert_insert
            AFTER INSERT ON sheet_vitrina_v1_own_capital_expense_certifications
            BEGIN
              INSERT INTO {OUTBOX_TABLE}(
                request_id,stable_source_id,source_revision,
                business_effective_date,affected_nm_ids_json,source_kind,
                status,requested_at,started_at,finished_at,error
              ) VALUES(
                'whbpo_cert_' || NEW.shipment_id || '_' || NEW.certified_at
                  || '_' || CAST(NEW.expenses_complete AS TEXT),
                'supplier_certification:' || NEW.shipment_id,
                NEW.shipment_id || ':' || NEW.certified_at || ':'
                  || CAST(NEW.expenses_complete AS TEXT),
                substr(NEW.certified_at,1,10),
                '[]','supplier_expense_certification','queued',
                NEW.certified_at,NULL,NULL,NULL
              )
              ON CONFLICT(stable_source_id,source_revision) DO UPDATE SET
                business_effective_date=MIN(
                  business_effective_date,
                  excluded.business_effective_date
                ),
                affected_nm_ids_json=excluded.affected_nm_ids_json,
                status=CASE
                  WHEN status='complete' THEN status ELSE 'queued'
                END,
                requested_at=excluded.requested_at,
                error=NULL;
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS warehouse_projection_expense_cert_update
            AFTER UPDATE OF expenses_complete,certified_at
            ON sheet_vitrina_v1_own_capital_expense_certifications
            BEGIN
              INSERT INTO {OUTBOX_TABLE}(
                request_id,stable_source_id,source_revision,
                business_effective_date,affected_nm_ids_json,source_kind,
                status,requested_at,started_at,finished_at,error
              ) VALUES(
                'whbpo_cert_' || NEW.shipment_id || '_' || NEW.certified_at
                  || '_' || CAST(NEW.expenses_complete AS TEXT),
                'supplier_certification:' || NEW.shipment_id,
                NEW.shipment_id || ':' || NEW.certified_at || ':'
                  || CAST(NEW.expenses_complete AS TEXT),
                substr(NEW.certified_at,1,10),
                '[]','supplier_expense_certification','queued',
                NEW.certified_at,NULL,NULL,NULL
              )
              ON CONFLICT(stable_source_id,source_revision) DO UPDATE SET
                business_effective_date=MIN(
                  business_effective_date,
                  excluded.business_effective_date
                ),
                affected_nm_ids_json=excluded.affected_nm_ids_json,
                status=CASE
                  WHEN status='complete' THEN status ELSE 'queued'
                END,
                requested_at=excluded.requested_at,
                error=NULL;
            END
            """
        )
    if "sheet_vitrina_v1_wb_cost_daily_state" in tables:
        for action in ("INSERT", "UPDATE"):
            trigger_name = (
                "warehouse_projection_wb_cost_insert"
                if action == "INSERT"
                else "warehouse_projection_wb_cost_update"
            )
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {trigger_name}
                AFTER {action} ON sheet_vitrina_v1_wb_cost_daily_state
                BEGIN
                  INSERT INTO {OUTBOX_TABLE}(
                    request_id,stable_source_id,source_revision,
                    business_effective_date,affected_nm_ids_json,source_kind,
                    status,requested_at,started_at,finished_at,error
                  ) VALUES(
                    'whbpo_wb_' || NEW.as_of_date || '_' || CAST(NEW.nm_id AS TEXT)
                      || '_' || NEW.inputs_hash,
                    'official_wb_cost:' || NEW.as_of_date || ':'
                      || CAST(NEW.nm_id AS TEXT),
                    NEW.inputs_hash,
                    NEW.as_of_date,
                    '[' || CAST(NEW.nm_id AS TEXT) || ']',
                    'official_wb_snapshot_wac','queued',
                    NEW.calculated_at,NULL,NULL,NULL
                  )
                  ON CONFLICT(stable_source_id,source_revision) DO UPDATE SET
                    business_effective_date=MIN(
                      business_effective_date,
                      excluded.business_effective_date
                    ),
                    affected_nm_ids_json=excluded.affected_nm_ids_json,
                    status=CASE
                      WHEN status='complete' THEN status ELSE 'queued'
                    END,
                    requested_at=excluded.requested_at,
                    error=NULL;
                END
                """
            )
    if "sheet_vitrina_v1_warehouse_targeted_recalc_queue" in tables:
        for action in ("INSERT", "UPDATE"):
            trigger_name = (
                "warehouse_projection_functional_queue_insert"
                if action == "INSERT"
                else "warehouse_projection_functional_queue_update"
            )
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {trigger_name}
                AFTER {action} ON sheet_vitrina_v1_warehouse_targeted_recalc_queue
                WHEN NEW.status='queued'
                BEGIN
                  INSERT INTO {OUTBOX_TABLE}(
                    request_id,stable_source_id,source_revision,
                    business_effective_date,affected_nm_ids_json,source_kind,
                    status,requested_at,started_at,finished_at,error
                  ) VALUES(
                    'whbpo_functional_' || NEW.queue_id || '_'
                      || substr(NEW.source_revision,1,32),
                    'functional_queue:' || NEW.stable_source_id,
                    NEW.source_revision,
                    NEW.effective_date,
                    NEW.affected_nm_ids_json,
                    CASE
                      WHEN NEW.stable_source_id LIKE 'supplier_costs:%'
                        OR NEW.stable_source_id LIKE 'cny_document:%'
                        OR NEW.stable_source_id LIKE 'fulfillment_upload:%'
                      THEN 'functional_cost_revision'
                      WHEN NEW.stable_source_id LIKE 'wb_transit_cost:%'
                      THEN 'functional_transit_cost_revision'
                      WHEN NEW.stable_source_id LIKE 'wb_supply:%'
                      THEN 'functional_physical_revision'
                      ELSE 'functional_source_revision'
                    END,
                    'queued',NEW.requested_at,NULL,NULL,NULL
                  )
                  ON CONFLICT(stable_source_id,source_revision) DO UPDATE SET
                    business_effective_date=MIN(
                      business_effective_date,
                      excluded.business_effective_date
                    ),
                    affected_nm_ids_json=excluded.affected_nm_ids_json,
                    status=CASE
                      WHEN status='complete' THEN status ELSE 'queued'
                    END,
                    requested_at=excluded.requested_at,
                    error=NULL;
                END
                """
            )
    if {
        "sheet_vitrina_v1_ff_stock_operations",
        "sheet_vitrina_v1_ff_stock_operation_lines",
    }.issubset(tables):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS warehouse_projection_ff_operation_line
            AFTER INSERT ON sheet_vitrina_v1_ff_stock_operation_lines
            BEGIN
              INSERT INTO {OUTBOX_TABLE}(
                request_id,stable_source_id,source_revision,
                business_effective_date,affected_nm_ids_json,source_kind,
                status,requested_at,started_at,finished_at,error
              )
              SELECT
                'whbpo_ff_' || NEW.operation_id || '_'
                  || CAST(NEW.line_no AS TEXT),
                'ff_operation:' || NEW.operation_id || ':'
                  || CAST(NEW.nm_id AS TEXT),
                NEW.operation_id || ':' || CAST(NEW.line_no AS TEXT) || ':'
                  || CAST(NEW.quantity_delta AS TEXT),
                COALESCE(
                  NULLIF(operation.business_effective_date,''),
                  date(operation.created_at,'+5 hours')
                ),
                '[' || CAST(NEW.nm_id AS TEXT) || ']',
                'ff_stock_physical_movement',
                'queued',operation.created_at,NULL,NULL,NULL
              FROM sheet_vitrina_v1_ff_stock_operations AS operation
              WHERE operation.operation_id=NEW.operation_id
              ON CONFLICT(stable_source_id,source_revision) DO UPDATE SET
                business_effective_date=MIN(
                  business_effective_date,
                  excluded.business_effective_date
                ),
                affected_nm_ids_json=excluded.affected_nm_ids_json,
                status=CASE
                  WHEN status='complete' THEN status ELSE 'queued'
                END,
                requested_at=excluded.requested_at,
                error=NULL;
            END
            """
        )


def ensure_functional_version_business_time_schema(
    conn: sqlite3.Connection,
) -> None:
    """Add nullable business/audit columns without rewriting historical rows."""

    columns = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in conn.execute(
            "PRAGMA table_info(sheet_vitrina_v1_warehouse_functional_versions)"
        ).fetchall()
    }
    if "business_effective_date" not in columns:
        conn.execute(
            "ALTER TABLE sheet_vitrina_v1_warehouse_functional_versions "
            "ADD COLUMN business_effective_date TEXT"
        )
    if "published_at" not in columns:
        conn.execute(
            "ALTER TABLE sheet_vitrina_v1_warehouse_functional_versions "
            "ADD COLUMN published_at TEXT"
        )


def _business_effective_date(plan: Mapping[str, Any]) -> str:
    explicit = str(
        plan.get("business_effective_date")
        or plan.get("earliest_business_date")
        or ""
    ).strip()
    if explicit:
        return _iso_date(explicit, field_name="business_effective_date")
    header_before = dict(plan.get("before_header") or {})
    header_after = dict(plan.get("after_header") or {})
    candidates = [
        str(value or "")[:10]
        for value in (
            header_before.get("actual_shipment_date"),
            header_after.get("actual_shipment_date"),
        )
        if str(value or "").strip()
    ]
    if not candidates:
        raise WarehouseBusinessProjectionError(
            "targeted source revision has no business-effective date"
        )
    return min(_iso_date(value, field_name="business_effective_date") for value in candidates)


def _exact_base_version(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
    published_version_id: str,
) -> sqlite3.Row | None:
    """Select by functional business date; technical timestamps only order ties."""

    rows = conn.execute(
        """
        SELECT version.version_id,version.version_kind,
               version.business_effective_date,version.published_at,
               version.created_at,snapshot.snapshot_date
        FROM sheet_vitrina_v1_warehouse_functional_versions AS version
        JOIN sheet_vitrina_v1_warehouse_wb_snapshots AS snapshot
          ON snapshot.version_id=version.version_id
        WHERE version.cutover_id='warehouse_functional_cutover_v1'
          AND version.status='good'
          AND snapshot.snapshot_date=?
          AND COALESCE(
                NULLIF(version.business_effective_date,''),
                snapshot.snapshot_date
              )<=?
        ORDER BY
          CASE WHEN version.version_id=? THEN 1 ELSE 0 END DESC,
          COALESCE(NULLIF(version.published_at,''),version.created_at) DESC,
          version.created_at DESC,
          version.version_id DESC
        """,
        (as_of_date, as_of_date, published_version_id),
    ).fetchall()
    return rows[0] if rows else None


def _version_balances(
    conn: sqlite3.Connection,
    *,
    version_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM sheet_vitrina_v1_warehouse_functional_balances
        WHERE version_id=?
        ORDER BY warehouse_key,nm_id
        """,
        (version_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        row["provenance"] = _loads(row.pop("provenance_json", "{}"), {})
        result.append(row)
    return result


def _target_source_records(
    plan: Mapping[str, Any],
    *,
    shipment_id: str,
) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = {}
    for raw in plan.get("target_rows_after") or []:
        nm_id = int(raw.get("nm_id") or 0)
        if nm_id <= 0:
            continue
        sources = [
            dict(item)
            for item in dict(raw.get("provenance") or {}).get("source_records") or []
            if str(item.get("shipment_id") or "") == shipment_id
        ]
        if sources:
            result.setdefault(nm_id, []).extend(sources)
    return result


def _supplier_stage_for_date(
    *,
    as_of_date: str,
    after_header: Mapping[str, Any],
) -> str | None:
    shipment_date = str(after_header.get("actual_shipment_date") or "").strip()[:10]
    acceptance_date = str(
        after_header.get("actual_ff_acceptance_date") or ""
    ).strip()[:10]
    if not shipment_date or as_of_date < shipment_date:
        return "production"
    if acceptance_date and as_of_date >= acceptance_date:
        return None
    return "china_to_ff"


def _reproject_supplier_source(
    balances: Sequence[Mapping[str, Any]],
    *,
    plan: Mapping[str, Any],
    as_of_date: str,
) -> list[dict[str, Any]]:
    shipment_id = str(plan.get("shipment_id") or "").strip()
    if not shipment_id:
        raise WarehouseBusinessProjectionError(
            "targeted supplier projection is missing shipment_id"
        )
    affected = {
        int(value)
        for value in plan.get("affected_nm_ids") or []
        if int(value) > 0
    }
    target_sources = _target_source_records(plan, shipment_id=shipment_id)
    destination = _supplier_stage_for_date(
        as_of_date=as_of_date,
        after_header=dict(plan.get("after_header") or {}),
    )
    by_key = {
        (str(row.get("warehouse_key") or ""), int(row.get("nm_id") or 0)): deepcopy(dict(row))
        for row in balances
    }
    before_quantity = {
        nm_id: sum(
            (
                _decimal(by_key.get((stage, nm_id), {}).get("quantity"))
                for stage in _SUPPLIER_STAGES
            ),
            ZERO,
        )
        for nm_id in affected
    }
    for nm_id in affected:
        for stage in _SUPPLIER_STAGES:
            row = by_key.get((stage, nm_id))
            if row is None:
                row = {
                    "warehouse_key": stage,
                    "nm_id": nm_id,
                    "quantity": "0",
                    "wac_rub": None,
                    "capital_rub": "0",
                    "cost_covered_quantity": "0",
                    "quality": "empty",
                    "certified": 0,
                    "wb_quantity": "0",
                    "wb_in_way_to_client": "0",
                    "wb_in_way_from_client": "0",
                    "provenance": {"source_records": []},
                }
            sources = [
                dict(item)
                for item in dict(row.get("provenance") or {}).get("source_records") or []
                if str(item.get("shipment_id") or "") != shipment_id
            ]
            if stage == destination:
                sources.extend(deepcopy(target_sources.get(nm_id) or []))
            quantity = sum((_decimal(item.get("flow_quantity")) for item in sources), ZERO)
            capital = sum((_decimal(item.get("flow_capital_rub")) for item in sources), ZERO)
            covered = sum(
                (
                    _decimal(item.get("flow_quantity"))
                    for item in sources
                    if str(item.get("cost_freshness") or "") != "unavailable"
                    and _decimal(item.get("flow_capital_rub")) > ZERO
                ),
                ZERO,
            )
            if quantity <= ZERO:
                by_key.pop((stage, nm_id), None)
                continue
            fully_covered = covered >= quantity and capital > ZERO
            qualities = sorted(
                {
                    str(item.get("quality") or "cost_unavailable")
                    for item in sources
                }
            )
            by_key[(stage, nm_id)] = {
                **row,
                "warehouse_key": stage,
                "nm_id": nm_id,
                "quantity": str(quantity),
                "wac_rub": str(capital / quantity) if fully_covered else None,
                "capital_rub": str(capital),
                "cost_covered_quantity": str(min(covered, quantity)),
                "quality": (
                    qualities[0]
                    if len(qualities) == 1
                    else "mixed:" + ",".join(qualities)
                ),
                "certified": int(
                    fully_covered
                    and all(
                        bool(item.get("expenses_complete_certification"))
                        for item in sources
                    )
                ),
                "provenance": {
                    **dict(row.get("provenance") or {}),
                    "source_records": sources,
                    "business_projection_revision": True,
                    "business_effective_date": _business_effective_date(plan),
                },
            }
    after_quantity = {
        nm_id: sum(
            (
                _decimal(by_key.get((stage, nm_id), {}).get("quantity"))
                for stage in _SUPPLIER_STAGES
            ),
            ZERO,
        )
        for nm_id in affected
    }
    expected_target_quantity = {
        nm_id: sum(
            (
                _decimal(item.get("flow_quantity"))
                for item in target_sources.get(nm_id) or []
            ),
            ZERO,
        )
        for nm_id in affected
    }
    before_target_quantity: dict[int, Decimal] = {}
    for row in balances:
        nm_id = int(row.get("nm_id") or 0)
        if nm_id not in affected or str(row.get("warehouse_key") or "") not in _SUPPLIER_STAGES:
            continue
        before_target_quantity[nm_id] = before_target_quantity.get(nm_id, ZERO) + sum(
            (
                _decimal(item.get("flow_quantity"))
                for item in dict(row.get("provenance") or {}).get("source_records") or []
                if str(item.get("shipment_id") or "") == shipment_id
            ),
            ZERO,
        )
    for nm_id in affected:
        expected_delta = expected_target_quantity[nm_id] - before_target_quantity.get(
            nm_id, ZERO
        )
        if after_quantity[nm_id] - before_quantity[nm_id] != expected_delta:
            raise WarehouseBusinessProjectionError(
                "supplier physical projection violates quantity conservation: "
                f"date={as_of_date}, nm_id={nm_id}"
            )
    return [
        row
        for _, row in sorted(
            by_key.items(),
            key=lambda item: (item[0][0], item[0][1]),
        )
    ]


def _metric_rows(
    balances: Sequence[Mapping[str, Any]],
    *,
    affected_nm_ids: Iterable[int],
    unavailable_reason: str = "",
) -> dict[int, dict[str, Any]]:
    affected = sorted({int(value) for value in affected_nm_ids if int(value) > 0})
    if unavailable_reason:
        empty_metrics = {
            key: None for key in OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS
        }
        empty_presentation = {
            key: {
                "state": "unavailable",
                "tone": "neutral",
                "reason": unavailable_reason,
                "source": "WebCore business-time projection",
            }
            for key in OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS
        }
        result = {
            nm_id: {
                "metrics": deepcopy(empty_metrics),
                "presentation": deepcopy(empty_presentation),
            }
            for nm_id in affected
        }
        result[0] = {
            "metrics": {
                key: None for key in OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS
            },
            "presentation": {
                key: {
                    "state": "unavailable",
                    "tone": "neutral",
                    "reason": unavailable_reason,
                    "source": "WebCore business-time projection",
                }
                for key in OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS
            },
        }
        return result

    by_nm: dict[int, dict[str, Any]] = {}
    all_nm_ids = sorted(
        {
            int(row.get("nm_id") or 0)
            for row in balances
            if int(row.get("nm_id") or 0) > 0
        }
    )
    for nm_id in all_nm_ids:
        metrics: dict[str, float | None] = {}
        presentation: dict[str, dict[str, str]] = {}
        for stage in OWN_PRODUCT_CAPITAL_STAGES:
            metrics[own_stage_metric_key(stage, "qty")] = 0.0
            metrics[own_stage_metric_key(stage, "capital_rub")] = 0.0
            metrics[own_stage_metric_key(stage, "unit_cost_rub")] = None
        by_nm[nm_id] = {"metrics": metrics, "presentation": presentation}

    for raw in balances:
        row = dict(raw)
        public_stage = _FUNCTIONAL_TO_PUBLIC_STAGE.get(
            str(row.get("warehouse_key") or "")
        )
        nm_id = int(row.get("nm_id") or 0)
        if public_stage is None or nm_id <= 0:
            continue
        target = by_nm.setdefault(nm_id, {"metrics": {}, "presentation": {}})
        quantity = _decimal(row.get("quantity"))
        capital = _decimal(row.get("capital_rub"))
        metric_values = {
            own_stage_metric_key(public_stage, "qty"): _number(quantity),
            own_stage_metric_key(public_stage, "capital_rub"): _number(capital),
            own_stage_metric_key(public_stage, "unit_cost_rub"): (
                _number(capital / quantity) if quantity > ZERO else None
            ),
        }
        target["metrics"].update(metric_values)
        if quantity > ZERO and not bool(row.get("certified")):
            reason = str(row.get("quality") or "неполное подтверждение источника")
            for metric_key in metric_values:
                target["presentation"][metric_key] = {
                    "state": "unconfirmed",
                    "tone": "warning",
                    "reason": reason,
                    "source": "WebCore business-time projection",
                }

    for item in by_nm.values():
        metrics = item["metrics"]
        quantity = sum(
            (
                _decimal(metrics.get(own_stage_metric_key(stage, "qty")))
                for stage in OWN_PRODUCT_CAPITAL_STAGES
            ),
            ZERO,
        )
        capital = sum(
            (
                _decimal(metrics.get(own_stage_metric_key(stage, "capital_rub")))
                for stage in OWN_PRODUCT_CAPITAL_STAGES
            ),
            ZERO,
        )
        metrics[OWN_TOTAL_QTY_METRIC_KEY] = _number(quantity)
        metrics[OWN_TOTAL_CAPITAL_RUB_METRIC_KEY] = _number(capital)
        metrics[OWN_AVG_COST_RUB_METRIC_KEY] = (
            _number(capital / quantity) if quantity > ZERO else None
        )

    total_metrics: dict[str, float | None] = {}
    total_presentation: dict[str, dict[str, str]] = {}
    for stage in OWN_PRODUCT_CAPITAL_STAGES:
        quantity = sum(
            (
                _decimal(item["metrics"].get(own_stage_metric_key(stage, "qty")))
                for item in by_nm.values()
            ),
            ZERO,
        )
        capital = sum(
            (
                _decimal(
                    item["metrics"].get(own_stage_metric_key(stage, "capital_rub"))
                )
                for item in by_nm.values()
            ),
            ZERO,
        )
        total_metrics[own_stage_total_metric_key(stage, "qty")] = _number(quantity)
        total_metrics[own_stage_total_metric_key(stage, "capital_rub")] = _number(
            capital
        )
        total_metrics[own_stage_total_metric_key(stage, "unit_cost_rub")] = (
            _number(capital / quantity) if quantity > ZERO else None
        )
        stage_reasons = sorted(
            {
                str(presentation.get("reason") or "")
                for item in by_nm.values()
                for metric_key, presentation in item["presentation"].items()
                if metric_key.startswith(f"own_capital_{stage}_")
                and str(presentation.get("reason") or "")
            }
        )
        if stage_reasons:
            for field in ("qty", "capital_rub", "unit_cost_rub"):
                total_presentation[own_stage_total_metric_key(stage, field)] = {
                    "state": "unconfirmed",
                    "tone": "warning",
                    "reason": "; ".join(stage_reasons),
                    "source": "WebCore business-time projection",
                }
    total_quantity = sum(
        (_decimal(item["metrics"].get(OWN_TOTAL_QTY_METRIC_KEY)) for item in by_nm.values()),
        ZERO,
    )
    total_capital = sum(
        (
            _decimal(item["metrics"].get(OWN_TOTAL_CAPITAL_RUB_METRIC_KEY))
            for item in by_nm.values()
        ),
        ZERO,
    )
    total_metrics[OWN_TOTAL_QTY_TOTAL_METRIC_KEY] = _number(total_quantity)
    total_metrics[OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY] = _number(total_capital)
    total_metrics[OWN_AVG_COST_RUB_TOTAL_METRIC_KEY] = (
        _number(total_capital / total_quantity) if total_quantity > ZERO else None
    )
    result = {
        nm_id: by_nm[nm_id]
        for nm_id in affected
        if nm_id in by_nm
    }
    for nm_id in affected:
        result.setdefault(
            nm_id,
            _metric_rows(
                [],
                affected_nm_ids=[nm_id],
                unavailable_reason=(
                    "Точная functional projection не содержит SKU для этой "
                    "business date; нулевое значение не предполагается."
                ),
            )[nm_id],
        )
    result[0] = {
        "metrics": total_metrics,
        "presentation": total_presentation,
    }
    return result


def _preserve_cost_only_quantities(
    metrics: dict[str, Any],
    old_metrics: Mapping[str, Any],
) -> None:
    """Keep physical quantities immutable while refreshing their cost layer."""

    quantity_cost_groups = [
        (
            own_stage_metric_key(stage, "qty"),
            own_stage_metric_key(stage, "capital_rub"),
            own_stage_metric_key(stage, "unit_cost_rub"),
        )
        for stage in OWN_PRODUCT_CAPITAL_STAGES
    ]
    quantity_cost_groups.extend(
        (
            own_stage_total_metric_key(stage, "qty"),
            own_stage_total_metric_key(stage, "capital_rub"),
            own_stage_total_metric_key(stage, "unit_cost_rub"),
        )
        for stage in OWN_PRODUCT_CAPITAL_STAGES
    )
    quantity_cost_groups.extend(
        [
            (
                OWN_TOTAL_QTY_METRIC_KEY,
                OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
                OWN_AVG_COST_RUB_METRIC_KEY,
            ),
            (
                OWN_TOTAL_QTY_TOTAL_METRIC_KEY,
                OWN_TOTAL_CAPITAL_RUB_TOTAL_METRIC_KEY,
                OWN_AVG_COST_RUB_TOTAL_METRIC_KEY,
            ),
        ]
    )
    for quantity_key, capital_key, unit_cost_key in quantity_cost_groups:
        if quantity_key not in metrics or quantity_key not in old_metrics:
            continue
        metrics[quantity_key] = old_metrics[quantity_key]
        if capital_key not in metrics or unit_cost_key not in metrics:
            continue
        capital = metrics[capital_key]
        quantity = _decimal(metrics[quantity_key])
        metrics[unit_cost_key] = (
            _number(_decimal(capital) / quantity)
            if capital not in (None, "") and quantity > ZERO
            else None
        )


def _candidate_rows(
    conn: sqlite3.Connection,
    *,
    plan: Mapping[str, Any],
    published_version_id: str,
    published_at: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    business_effective_date = _business_effective_date(plan)
    publication_business_date = business_date_from_timestamp(published_at)
    target_dates = _date_range(business_effective_date, publication_business_date)
    affected_nm_ids = sorted(
        {
            int(value)
            for value in plan.get("affected_nm_ids") or []
            if int(value) > 0
        }
    )
    if not affected_nm_ids:
        raise WarehouseBusinessProjectionError(
            "business projection has no affected SKU closure"
        )
    rows: list[dict[str, Any]] = []
    missing_dates: list[str] = []
    projected_balance_rows = 0
    started = time.perf_counter()
    for as_of_date in target_dates:
        version = _exact_base_version(
            conn,
            as_of_date=as_of_date,
            published_version_id=published_version_id,
        )
        if version is None:
            missing_dates.append(as_of_date)
            metrics_by_nm = _metric_rows(
                [],
                affected_nm_ids=affected_nm_ids,
                unavailable_reason=(
                    "Точная складская functional projection за business date "
                    f"{as_of_date} отсутствует. Другие источники Витрины не "
                    "подменяются и вчерашние значения не переносятся."
                ),
            )
            base_version_id = ""
        else:
            base_version_id = str(version["version_id"])
            balances = _version_balances(conn, version_id=base_version_id)
            projected = _reproject_supplier_source(
                balances,
                plan=plan,
                as_of_date=as_of_date,
            )
            projected_balance_rows += len(projected)
            metrics_by_nm = _metric_rows(
                projected,
                affected_nm_ids=affected_nm_ids,
            )
        for nm_id, item in sorted(metrics_by_nm.items()):
            metrics = {
                key: value
                for key, value in dict(item.get("metrics") or {}).items()
                if key in set(OWN_PRODUCT_CAPITAL_METRIC_KEYS)
            }
            presentation = {
                key: value
                for key, value in dict(item.get("presentation") or {}).items()
                if key in set(OWN_PRODUCT_CAPITAL_METRIC_KEYS)
            }
            provenance = {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "stable_source_id": str(
                    plan.get("stable_source_id")
                    or f"supplier_shipment:{plan.get('shipment_id')}"
                ),
                "source_revision": str(plan.get("source_revision") or ""),
                "business_effective_date": business_effective_date,
                "published_at": published_at,
                "as_of_date": as_of_date,
                "base_version_id": base_version_id,
                "published_version_id": published_version_id,
                "owned_metric_keys": sorted(metrics),
                "missing_exact_functional_date": not bool(base_version_id),
            }
            row_fingerprint = _fingerprint(
                {
                    "as_of_date": as_of_date,
                    "nm_id": nm_id,
                    "metrics": metrics,
                    "presentation": presentation,
                    "provenance": provenance,
                }
            )
            rows.append(
                {
                    "as_of_date": as_of_date,
                    "nm_id": nm_id,
                    "metrics": metrics,
                    "presentation": presentation,
                    "provenance": provenance,
                    "row_fingerprint": row_fingerprint,
                }
            )
    diagnostics = {
        "affected_dates": target_dates,
        "missing_exact_functional_dates": missing_dates,
        "affected_date_count": len(target_dates),
        "affected_sku_count": len(affected_nm_ids),
        "candidate_row_count": len(rows),
        "functional_balance_rows_read": projected_balance_rows,
        "external_source_refresh_count": 0,
        "full_vitrina_refresh_count": 0,
        "all_history_rebuild": False,
        "complexity": "O(affected dates × functional SKU rows)",
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    return rows, diagnostics


def publish_targeted_supplier_business_projection(
    conn: sqlite3.Connection,
    *,
    plan: Mapping[str, Any],
    published_version_id: str,
    published_at: str,
    inject_failure: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Publish one coalesced target revision in the caller's business transaction."""

    ensure_warehouse_business_projection_schema(conn)
    ensure_functional_version_business_time_schema(conn)
    business_effective_date = _business_effective_date(plan)
    stable_source_id = str(
        plan.get("stable_source_id")
        or f"supplier_shipment:{plan.get('shipment_id')}"
    )
    source_revision = str(plan.get("source_revision") or "").strip()
    if not stable_source_id or not source_revision:
        raise WarehouseBusinessProjectionError(
            "stable source identity and source revision are required"
        )
    plan_fingerprint = _fingerprint(
        {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "stable_source_id": stable_source_id,
            "source_revision": source_revision,
            "business_effective_date": business_effective_date,
            "published_version_id": published_version_id,
            "target_after_digest": str(plan.get("target_after_digest") or ""),
            "affected_nm_ids": sorted(
                int(value) for value in plan.get("affected_nm_ids") or []
            ),
        }
    )
    existing = conn.execute(
        f"SELECT * FROM {REVISION_TABLE} WHERE plan_fingerprint=?",
        (plan_fingerprint,),
    ).fetchone()
    if existing is not None and str(existing["status"]) == "active":
        return {
            "status": "success",
            "idempotent": True,
            "revision_id": str(existing["revision_id"]),
            "source_revision": source_revision,
            "business_effective_date": business_effective_date,
            "published_at": str(existing["published_at"]),
            "changed_rows": int(existing["changed_row_count"]),
            "changed_cells": int(existing["changed_cell_count"]),
            "diagnostics": _loads(existing["diagnostics_json"], {}),
        }
    active_source_revision = conn.execute(
        f"""
        SELECT revision_id,plan_fingerprint
        FROM {REVISION_TABLE}
        WHERE stable_source_id=? AND source_revision=? AND status='active'
        ORDER BY completed_at DESC,revision_id DESC
        LIMIT 1
        """,
        (stable_source_id, source_revision),
    ).fetchone()
    if active_source_revision is not None:
        raise WarehouseBusinessProjectionError(
            "stable source revision already has a different active projection: "
            f"revision_id={active_source_revision['revision_id']}"
        )
    rows, diagnostics = _candidate_rows(
        conn,
        plan=plan,
        published_version_id=published_version_id,
        published_at=published_at,
    )
    revision_id = "whbpr_" + plan_fingerprint.removeprefix("sha256:")[:24]
    before = {
        (str(row["as_of_date"]), int(row["nm_id"])): str(
            row["row_fingerprint"]
        )
        for row in conn.execute(
            f"""
            SELECT as_of_date,nm_id,row_fingerprint
            FROM {CURRENT_ROW_TABLE}
            WHERE as_of_date BETWEEN ? AND ?
              AND (nm_id=0 OR nm_id IN (
                    {",".join("?" for _ in plan.get("affected_nm_ids") or [])}
                  ))
            """,
            (
                business_effective_date,
                business_date_from_timestamp(published_at),
                *[int(value) for value in plan.get("affected_nm_ids") or []],
            ),
        ).fetchall()
    }
    changed_rows = sum(
        before.get((str(item["as_of_date"]), int(item["nm_id"])))
        != str(item["row_fingerprint"])
        for item in rows
    )
    changed_cells = 0
    for item in rows:
        current = conn.execute(
            f"""
            SELECT metrics_json
            FROM {CURRENT_ROW_TABLE}
            WHERE as_of_date=? AND nm_id=?
            """,
            (item["as_of_date"], int(item["nm_id"])),
        ).fetchone()
        old_metrics = _loads(current["metrics_json"], {}) if current is not None else {}
        changed_cells += sum(
            old_metrics.get(key) != value
            for key, value in item["metrics"].items()
        )
    if inject_failure is not None:
        inject_failure("business_projection_candidate_ready")
    conn.execute(
        f"""
        INSERT INTO {REVISION_TABLE}(
            revision_id,stable_source_id,source_revision,business_effective_date,
            published_at,status,plan_fingerprint,base_version_id,
            published_version_id,affected_nm_ids_json,affected_dates_json,
            source_kind,changed_row_count,changed_cell_count,diagnostics_json,
            error,created_at,completed_at
        ) VALUES(?,?,?,?,?,'candidate',?,?,?,?,?,'targeted_supplier',?,?,?,?,?,NULL)
        """,
        (
            revision_id,
            stable_source_id,
            source_revision,
            business_effective_date,
            published_at,
            plan_fingerprint,
            str(plan.get("base_version_id") or ""),
            published_version_id,
            _json(sorted(int(value) for value in plan.get("affected_nm_ids") or [])),
            _json(diagnostics["affected_dates"]),
            changed_rows,
            changed_cells,
            _json(diagnostics),
            None,
            published_at,
        ),
    )
    for item in rows:
        values = (
            revision_id,
            str(item["as_of_date"]),
            int(item["nm_id"]),
            _json(item["metrics"]),
            _json(item["presentation"]),
            _json(item["provenance"]),
            str(item["row_fingerprint"]),
        )
        conn.execute(
            f"""
            INSERT INTO {ROW_TABLE}(
                revision_id,as_of_date,nm_id,metrics_json,presentation_json,
                provenance_json,row_fingerprint
            ) VALUES(?,?,?,?,?,?,?)
            """,
            values,
        )
        conn.execute(
            f"""
            INSERT INTO {CURRENT_ROW_TABLE}(
                as_of_date,nm_id,revision_id,metrics_json,presentation_json,
                provenance_json,row_fingerprint,published_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(as_of_date,nm_id) DO UPDATE SET
                revision_id=excluded.revision_id,
                metrics_json=excluded.metrics_json,
                presentation_json=excluded.presentation_json,
                provenance_json=excluded.provenance_json,
                row_fingerprint=excluded.row_fingerprint,
                published_at=excluded.published_at
            """,
            (
                values[1],
                values[2],
                revision_id,
                values[3],
                values[4],
                values[5],
                values[6],
                published_at,
            ),
        )
    if inject_failure is not None:
        inject_failure("business_projection_before_switch")
    state = conn.execute(
        f"SELECT revision_no FROM {STATE_TABLE} WHERE slot=1"
    ).fetchone()
    revision_no = int(state["revision_no"] if state is not None else 0) + 1
    conn.execute(
        f"""
        INSERT INTO {STATE_TABLE}(
            slot,revision_no,revision_id,source_revision,business_effective_date,
            published_at,status,updated_at
        ) VALUES(1,?,?,?,?,?,'ready',?)
        ON CONFLICT(slot) DO UPDATE SET
            revision_no=excluded.revision_no,
            revision_id=excluded.revision_id,
            source_revision=excluded.source_revision,
            business_effective_date=excluded.business_effective_date,
            published_at=excluded.published_at,
            status=excluded.status,
            updated_at=excluded.updated_at
        """,
        (
            revision_no,
            revision_id,
            source_revision,
            business_effective_date,
            published_at,
            published_at,
        ),
    )
    conn.execute(
        f"""
        UPDATE {REVISION_TABLE}
        SET status='active',completed_at=?
        WHERE revision_id=? AND status='candidate'
        """,
        (published_at, revision_id),
    )
    conn.execute(
        f"""
        UPDATE {OUTBOX_TABLE}
        SET status='complete',finished_at=?,error=NULL
        WHERE stable_source_id=?
          AND source_revision=?
          AND status IN ('queued','running','error')
        """,
        (
            published_at,
            "functional_queue:" + stable_source_id,
            source_revision,
        ),
    )
    return {
        "status": "success",
        "idempotent": False,
        "revision_no": revision_no,
        "revision_id": revision_id,
        "source_revision": source_revision,
        "business_effective_date": business_effective_date,
        "published_at": published_at,
        "changed_rows": changed_rows,
        "changed_cells": changed_cells,
        "affected_nm_ids": sorted(
            int(value) for value in plan.get("affected_nm_ids") or []
        ),
        "affected_dates": diagnostics["affected_dates"],
        "diagnostics": diagnostics,
    }


def record_failed_business_projection(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    stable_source_id: str,
    source_revision: str,
    business_effective_date: str,
    published_at: str,
    error: str,
) -> dict[str, Any]:
    """Record a failed job without changing current projection rows/state."""

    material = {
        "stable_source_id": str(stable_source_id),
        "source_revision": str(source_revision),
        "business_effective_date": _iso_date(
            business_effective_date,
            field_name="business_effective_date",
        ),
        "published_at": str(published_at),
        "error": str(error).replace("\n", " ")[:1000],
    }
    plan_fingerprint = _fingerprint(
        {"contract_name": CONTRACT_NAME, "failed": material}
    )
    revision_id = "whbpr_failed_" + plan_fingerprint.removeprefix("sha256:")[:16]
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_warehouse_business_projection_schema(conn)
        conn.execute(
            f"""
            INSERT INTO {REVISION_TABLE}(
                revision_id,stable_source_id,source_revision,business_effective_date,
                published_at,status,plan_fingerprint,base_version_id,
                published_version_id,affected_nm_ids_json,affected_dates_json,
                source_kind,changed_row_count,changed_cell_count,diagnostics_json,
                error,created_at,completed_at
            ) VALUES(?,?,?,?,?,'failed',?,'','', '[]','[]',
                     'targeted_supplier',0,0,'{{}}',?,?,?)
            ON CONFLICT(plan_fingerprint) DO UPDATE SET
                status='failed',error=excluded.error,completed_at=excluded.completed_at
            """,
            (
                revision_id,
                material["stable_source_id"],
                material["source_revision"],
                material["business_effective_date"],
                material["published_at"],
                plan_fingerprint,
                material["error"],
                material["published_at"],
                material["published_at"],
            ),
        )
        conn.commit()
    return {
        "status": "error",
        "revision_id": revision_id,
        **material,
    }


def _own_daily_balances(
    conn: sqlite3.Connection,
    *,
    as_of_date: str,
) -> list[dict[str, Any]]:
    table = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table'
          AND name='sheet_vitrina_v1_own_capital_daily_state'
        """
    ).fetchone()
    if table is None:
        return []
    reverse_stage = {
        public: functional for functional, public in _FUNCTIONAL_TO_PUBLIC_STAGE.items()
    }
    rows = conn.execute(
        """
        SELECT *
        FROM sheet_vitrina_v1_own_capital_daily_state
        WHERE as_of_date=?
        ORDER BY nm_id,stage
        """,
        (as_of_date,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        public_stage = str(row.get("stage") or "")
        warehouse_key = reverse_stage.get(public_stage)
        if warehouse_key is None:
            continue
        quantity = _decimal(row.get("quantity"))
        capital = _decimal(row.get("capital_rub"))
        confirmed = _decimal(row.get("confirmed_quantity"))
        reasons = [
            str(value)
            for value in dict(_loads(row.get("diagnostics_json"), {})).get(
                "reasons"
            )
            or []
            if str(value)
        ]
        result.append(
            {
                "warehouse_key": warehouse_key,
                "nm_id": int(row["nm_id"]),
                "quantity": str(quantity),
                "wac_rub": str(capital / quantity) if quantity > ZERO else None,
                "capital_rub": str(capital),
                "cost_covered_quantity": str(quantity if capital > ZERO else ZERO),
                "quality": "primary_documents" if not reasons else "; ".join(reasons),
                "certified": int(quantity <= ZERO or confirmed >= quantity),
                "provenance": {
                    "source": "sheet_vitrina_v1_own_capital_daily_state",
                    "input_fingerprint": str(row.get("input_fingerprint") or ""),
                    "calculated_at": str(row.get("calculated_at") or ""),
                    "reasons": reasons,
                },
            }
        )
    return result


def _resolve_outbox_scope(
    conn: sqlite3.Connection,
    requests: Sequence[Mapping[str, Any]],
) -> tuple[str, list[int]]:
    earliest = min(
        _iso_date(
            request.get("business_effective_date"),
            field_name="business_effective_date",
        )
        for request in requests
    )
    affected = {
        int(value)
        for request in requests
        for value in _loads(request.get("affected_nm_ids_json"), [])
        if int(value) > 0
    }
    certification_shipments = [
        str(request.get("stable_source_id") or "").split(":", 1)[-1]
        for request in requests
        if str(request.get("source_kind") or "")
        == "supplier_expense_certification"
    ]
    if certification_shipments:
        placeholders = ",".join("?" for _ in certification_shipments)
        rows = conn.execute(
            f"""
            SELECT shipment_id,nm_id,MIN(effective_date) AS first_date
            FROM sheet_vitrina_v1_own_capital_events
            WHERE shipment_id IN ({placeholders})
            GROUP BY shipment_id,nm_id
            """,
            certification_shipments,
        ).fetchall()
        for row in rows:
            affected.add(int(row["nm_id"]))
            if str(row["first_date"] or ""):
                earliest = min(earliest, str(row["first_date"])[:10])
    return earliest, sorted(affected)


def _persist_projection_revision(
    conn: sqlite3.Connection,
    *,
    revision_id: str,
    stable_source_id: str,
    source_revision: str,
    business_effective_date: str,
    published_at: str,
    plan_fingerprint: str,
    base_version_id: str,
    published_version_id: str,
    affected_nm_ids: Sequence[int],
    source_kind: str,
    rows: Sequence[Mapping[str, Any]],
    diagnostics: Mapping[str, Any],
) -> dict[str, Any]:
    existing = conn.execute(
        f"SELECT * FROM {REVISION_TABLE} WHERE plan_fingerprint=?",
        (plan_fingerprint,),
    ).fetchone()
    if existing is not None and str(existing["status"]) == "active":
        return {
            "status": "success",
            "idempotent": True,
            "revision_id": str(existing["revision_id"]),
            "revision_no": int(
                (
                    conn.execute(
                        f"SELECT revision_no FROM {STATE_TABLE} WHERE slot=1"
                    ).fetchone()
                    or {"revision_no": 0}
                )["revision_no"]
            ),
            "changed_rows": int(existing["changed_row_count"]),
            "changed_cells": int(existing["changed_cell_count"]),
        }
    changed_rows = 0
    changed_cells = 0
    for item in rows:
        current = conn.execute(
            f"""
            SELECT row_fingerprint,metrics_json
            FROM {CURRENT_ROW_TABLE}
            WHERE as_of_date=? AND nm_id=?
            """,
            (str(item["as_of_date"]), int(item["nm_id"])),
        ).fetchone()
        if (
            current is None
            or str(current["row_fingerprint"]) != str(item["row_fingerprint"])
        ):
            changed_rows += 1
        old_metrics = _loads(current["metrics_json"], {}) if current is not None else {}
        changed_cells += sum(
            old_metrics.get(key) != value
            for key, value in dict(item["metrics"]).items()
        )
    conn.execute(
        f"""
        INSERT INTO {REVISION_TABLE}(
            revision_id,stable_source_id,source_revision,business_effective_date,
            published_at,status,plan_fingerprint,base_version_id,
            published_version_id,affected_nm_ids_json,affected_dates_json,
            source_kind,changed_row_count,changed_cell_count,diagnostics_json,
            error,created_at,completed_at
        ) VALUES(?,?,?,?,?,'candidate',?,?,?,?,?,?,?,?,?,NULL,?,NULL)
        """,
        (
            revision_id,
            stable_source_id,
            source_revision,
            business_effective_date,
            published_at,
            plan_fingerprint,
            base_version_id,
            published_version_id,
            _json(list(affected_nm_ids)),
            _json(list(diagnostics.get("affected_dates") or [])),
            source_kind,
            changed_rows,
            changed_cells,
            _json(dict(diagnostics)),
            published_at,
        ),
    )
    for item in rows:
        values = (
            revision_id,
            str(item["as_of_date"]),
            int(item["nm_id"]),
            _json(item["metrics"]),
            _json(item["presentation"]),
            _json(item["provenance"]),
            str(item["row_fingerprint"]),
        )
        conn.execute(
            f"""
            INSERT INTO {ROW_TABLE}(
                revision_id,as_of_date,nm_id,metrics_json,presentation_json,
                provenance_json,row_fingerprint
            ) VALUES(?,?,?,?,?,?,?)
            """,
            values,
        )
        conn.execute(
            f"""
            INSERT INTO {CURRENT_ROW_TABLE}(
                as_of_date,nm_id,revision_id,metrics_json,presentation_json,
                provenance_json,row_fingerprint,published_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(as_of_date,nm_id) DO UPDATE SET
                revision_id=excluded.revision_id,
                metrics_json=excluded.metrics_json,
                presentation_json=excluded.presentation_json,
                provenance_json=excluded.provenance_json,
                row_fingerprint=excluded.row_fingerprint,
                published_at=excluded.published_at
            """,
            (
                values[1],
                values[2],
                revision_id,
                values[3],
                values[4],
                values[5],
                values[6],
                published_at,
            ),
        )
    state = conn.execute(
        f"SELECT revision_no FROM {STATE_TABLE} WHERE slot=1"
    ).fetchone()
    revision_no = int(state["revision_no"] if state is not None else 0) + 1
    conn.execute(
        f"""
        INSERT INTO {STATE_TABLE}(
            slot,revision_no,revision_id,source_revision,business_effective_date,
            published_at,status,updated_at
        ) VALUES(1,?,?,?,?,?,'ready',?)
        ON CONFLICT(slot) DO UPDATE SET
            revision_no=excluded.revision_no,
            revision_id=excluded.revision_id,
            source_revision=excluded.source_revision,
            business_effective_date=excluded.business_effective_date,
            published_at=excluded.published_at,
            status=excluded.status,
            updated_at=excluded.updated_at
        """,
        (
            revision_no,
            revision_id,
            source_revision,
            business_effective_date,
            published_at,
            published_at,
        ),
    )
    conn.execute(
        f"""
        UPDATE {REVISION_TABLE}
        SET status='active',completed_at=?
        WHERE revision_id=? AND status='candidate'
        """,
        (published_at, revision_id),
    )
    return {
        "status": "success",
        "idempotent": False,
        "revision_no": revision_no,
        "revision_id": revision_id,
        "changed_rows": changed_rows,
        "changed_cells": changed_cells,
    }


def publish_functional_version_business_projection(
    conn: sqlite3.Connection,
    *,
    published_version_id: str,
    business_effective_date: str,
    published_at: str,
    source_revision: str,
) -> dict[str, Any]:
    """Publish one exact functional business-date version into Web Vitrina.

    A normal functional sync already owns the complete six-stage physical and
    cost calculation.  Re-reading event-only daily state here can mix a fresh
    functional version with an older partial capital projection.  This seam is
    therefore deliberately version-bound and runs in the caller's transaction.
    """

    ensure_warehouse_business_projection_schema(conn)
    ensure_functional_version_business_time_schema(conn)
    selected_date = _iso_date(
        business_effective_date,
        field_name="business_effective_date",
    )
    version = conn.execute(
        """
        SELECT version.version_id,version.status,version.business_effective_date,
               version.published_at,version.created_at,snapshot.snapshot_date
        FROM sheet_vitrina_v1_warehouse_functional_versions AS version
        JOIN sheet_vitrina_v1_warehouse_wb_snapshots AS snapshot
          ON snapshot.version_id=version.version_id
        WHERE version.version_id=?
        """,
        (str(published_version_id),),
    ).fetchone()
    if version is None or str(version["status"]) != "good":
        raise WarehouseBusinessProjectionError(
            "functional business projection requires one exact good version"
        )
    if (
        str(version["business_effective_date"] or "") != selected_date
        or str(version["snapshot_date"] or "") != selected_date
    ):
        raise WarehouseBusinessProjectionError(
            "functional version business date and official snapshot date differ"
        )
    balances = _version_balances(conn, version_id=str(published_version_id))
    affected_nm_ids = sorted(
        {
            int(item.get("nm_id") or 0)
            for item in balances
            if int(item.get("nm_id") or 0) > 0
        }
    )
    if not balances or not affected_nm_ids:
        raise WarehouseBusinessProjectionError(
            "functional business projection has no balance rows"
        )
    metrics_by_nm = _metric_rows(
        balances,
        affected_nm_ids=affected_nm_ids,
    )
    rows: list[dict[str, Any]] = []
    for nm_id, item in sorted(metrics_by_nm.items()):
        provenance = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "source": "canonical_functional_warehouse_version",
            "business_effective_date": selected_date,
            "as_of_date": selected_date,
            "base_version_id": str(published_version_id),
            "published_version_id": str(published_version_id),
            "published_at": str(published_at),
            "missing_exact_projection_date": False,
        }
        material = {
            "as_of_date": selected_date,
            "nm_id": int(nm_id),
            "metrics": dict(item["metrics"]),
            "presentation": dict(item["presentation"]),
            "provenance": provenance,
        }
        rows.append(
            {
                **material,
                "row_fingerprint": _fingerprint(material),
            }
        )
    stable_source_id = f"functional_version:{published_version_id}"
    plan_fingerprint = _fingerprint(
        {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "stable_source_id": stable_source_id,
            "source_revision": str(source_revision),
            "business_effective_date": selected_date,
            "published_version_id": str(published_version_id),
            "candidate_fingerprints": [
                str(item["row_fingerprint"]) for item in rows
            ],
        }
    )
    revision_id = "whbpr_functional_" + plan_fingerprint.removeprefix(
        "sha256:"
    )[:20]
    result = _persist_projection_revision(
        conn,
        revision_id=revision_id,
        stable_source_id=stable_source_id,
        source_revision=str(source_revision),
        business_effective_date=selected_date,
        published_at=str(published_at),
        plan_fingerprint=plan_fingerprint,
        base_version_id=str(published_version_id),
        published_version_id=str(published_version_id),
        affected_nm_ids=affected_nm_ids,
        source_kind="exact_functional_version",
        rows=rows,
        diagnostics={
            "affected_dates": [selected_date],
            "affected_sku_count": len(affected_nm_ids),
            "candidate_row_count": len(rows),
            "functional_balance_rows_read": len(balances),
            "external_source_refresh_count": 0,
            "full_vitrina_refresh_count": 0,
            "all_history_rebuild": False,
            "exact_functional_version": True,
        },
    )
    return {
        **result,
        "plan_fingerprint": plan_fingerprint,
        "business_effective_date": selected_date,
        "published_version_id": str(published_version_id),
        "affected_nm_ids": affected_nm_ids,
    }


def drain_warehouse_business_projection_outbox(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    published_at: str | None = None,
    inject_failure: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Coalesce and publish canonical capital events without external fetches."""

    timestamp = str(published_at or _now())
    started = time.perf_counter()
    request_rows: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(runtime.db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            ensure_warehouse_projection_source_outbox(conn)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"""
                UPDATE {OUTBOX_TABLE}
                SET source_kind='functional_transit_cost_revision'
                WHERE stable_source_id LIKE 'functional_queue:wb_transit_cost:%'
                  AND source_kind<>'functional_transit_cost_revision'
                  AND status IN ('queued','error')
                """
            )
            request_rows = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT *
                    FROM {OUTBOX_TABLE}
                    WHERE status IN ('queued','error')
                    ORDER BY business_effective_date,requested_at,request_id
                    """
                ).fetchall()
            ]
            if not request_rows:
                conn.rollback()
                return {
                    "status": "no_op",
                    "request_count": 0,
                    "external_source_refresh_count": 0,
                    "full_vitrina_refresh_count": 0,
                }
            request_ids = [str(item["request_id"]) for item in request_rows]
            placeholders = ",".join("?" for _ in request_ids)
            conn.execute(
                f"""
                UPDATE {OUTBOX_TABLE}
                SET status='running',started_at=?,finished_at=NULL,error=NULL
                WHERE request_id IN ({placeholders})
                  AND status IN ('queued','error')
                """,
                (timestamp, *request_ids),
            )
            projection_request_rows = [
                item
                for item in request_rows
                if not str(item["source_kind"]).startswith(
                    ("functional_", "ff_stock_")
                )
            ]
            scope_rows = projection_request_rows or request_rows
            business_effective_date, affected_nm_ids = _resolve_outbox_scope(
                conn, scope_rows
            )
            if not affected_nm_ids:
                raise WarehouseBusinessProjectionError(
                    "coalesced source outbox has no affected SKU closure"
                )
            cost_only = all(
                _is_cost_only_outbox_request(item) for item in scope_rows
            )
            target_dates, date_scope = _bounded_outbox_target_dates(
                conn,
                business_effective_date=business_effective_date,
                publication_business_date=business_date_from_timestamp(timestamp),
                cost_only=cost_only,
            )
            has_canonical_event_proof = bool(projection_request_rows)
            if not has_canonical_event_proof:
                # These requests only prove that the full functional
                # calculator must replay.  Publishing an event-only fallback
                # here would replace last-good rows with a stale/partial mix.
                conn.execute(
                    f"""
                    UPDATE {OUTBOX_TABLE}
                    SET status='complete',finished_at=?,error=NULL
                    WHERE request_id IN ({placeholders}) AND status='running'
                    """,
                    (timestamp, *request_ids),
                )
                conn.commit()
                return {
                    "status": "success",
                    "idempotent": True,
                    "request_count": len(request_rows),
                    "business_effective_date": business_effective_date,
                    "affected_nm_ids": affected_nm_ids,
                    "affected_dates": target_dates,
                    "changed_rows": 0,
                    "changed_cells": 0,
                    "diagnostics": {
                        "last_good_preserved": True,
                        "awaiting_exact_functional_replay": True,
                        "replay_signal_request_count": len(request_rows),
                        "external_source_refresh_count": 0,
                        "full_vitrina_refresh_count": 0,
                        "all_history_rebuild": False,
                        "cost_only": cost_only,
                        **date_scope,
                        "elapsed_ms": round(
                            (time.perf_counter() - started) * 1000,
                            3,
                        ),
                    },
                }
            candidate_rows: list[dict[str, Any]] = []
            missing_dates: list[str] = []
            daily_rows_read = 0
            for as_of_date in target_dates:
                balances = _own_daily_balances(conn, as_of_date=as_of_date)
                daily_rows_read += len(balances)
                if not balances or not has_canonical_event_proof:
                    missing_dates.append(as_of_date)
                    metrics_by_nm = _metric_rows(
                        [],
                        affected_nm_ids=affected_nm_ids,
                        unavailable_reason=(
                            (
                                "Canonical source revision поставлена в bounded "
                                "functional replay, но exact event projection "
                                "ещё не доказана; "
                                if balances and not has_canonical_event_proof
                                else (
                                    "Точная event-based product-capital "
                                    f"projection за {as_of_date} отсутствует; "
                                )
                            )
                            + "другие источники Витрины и вчерашние значения "
                            "не подменяются."
                        ),
                    )
                else:
                    metrics_by_nm = _metric_rows(
                        balances,
                        affected_nm_ids=affected_nm_ids,
                    )
                for nm_id, item in sorted(metrics_by_nm.items()):
                    metrics = {
                        key: value
                        for key, value in dict(item["metrics"]).items()
                        if key in set(OWN_PRODUCT_CAPITAL_METRIC_KEYS)
                    }
                    presentation = {
                        key: value
                        for key, value in dict(item["presentation"]).items()
                        if key in set(OWN_PRODUCT_CAPITAL_METRIC_KEYS)
                    }
                    if cost_only or not has_canonical_event_proof:
                        current = conn.execute(
                            f"""
                            SELECT metrics_json
                            FROM {CURRENT_ROW_TABLE}
                            WHERE as_of_date=? AND nm_id=?
                            """,
                            (as_of_date, int(nm_id)),
                        ).fetchone()
                        old_metrics = (
                            _loads(current["metrics_json"], {})
                            if current is not None
                            else {}
                        )
                        if not has_canonical_event_proof:
                            for metric_key in list(metrics):
                                is_quantity = (
                                    metric_key.endswith("_qty")
                                    or metric_key.endswith("_qty_total")
                                )
                                if (
                                    (not cost_only or is_quantity)
                                    and metric_key in old_metrics
                                ):
                                    metrics[metric_key] = old_metrics[metric_key]
                                    presentation[metric_key] = {
                                        "state": "unconfirmed",
                                        "tone": "warning",
                                        "reason": (
                                            "Source revision сохранена, но exact "
                                            "event projection ещё не доказана; "
                                            "показано последнее согласованное "
                                            "значение."
                                        ),
                                        "source": (
                                            "WebCore business-time projection"
                                        ),
                                    }
                        if cost_only:
                            _preserve_cost_only_quantities(metrics, old_metrics)
                    provenance = {
                        "contract_name": CONTRACT_NAME,
                        "contract_version": CONTRACT_VERSION,
                        "source": "canonical_own_capital_events",
                        "source_request_ids": [
                            str(request["request_id"])
                            for request in projection_request_rows
                        ],
                        "business_effective_date": business_effective_date,
                        "projection_effective_date": target_dates[0],
                        "as_of_date": as_of_date,
                        "published_at": timestamp,
                        "missing_exact_projection_date": (
                            not bool(balances)
                            or not has_canonical_event_proof
                        ),
                    }
                    candidate_rows.append(
                        {
                            "as_of_date": as_of_date,
                            "nm_id": nm_id,
                            "metrics": metrics,
                            "presentation": presentation,
                            "provenance": provenance,
                            "row_fingerprint": _fingerprint(
                                {
                                    "as_of_date": as_of_date,
                                    "nm_id": nm_id,
                                    "metrics": metrics,
                                    "presentation": presentation,
                                    "provenance": provenance,
                                }
                            ),
                        }
                    )
            if cost_only:
                for item in candidate_rows:
                    current = conn.execute(
                        f"""
                        SELECT metrics_json
                        FROM {CURRENT_ROW_TABLE}
                        WHERE as_of_date=? AND nm_id=?
                        """,
                        (item["as_of_date"], int(item["nm_id"])),
                    ).fetchone()
                    if current is None:
                        continue
                    old = _loads(current["metrics_json"], {})
                    for metric_key, value in item["metrics"].items():
                        if (
                            metric_key.endswith("_qty")
                            or metric_key == OWN_TOTAL_QTY_METRIC_KEY
                        ) and metric_key in old and old.get(metric_key) != value:
                            raise WarehouseBusinessProjectionError(
                                "cost-only projection changed physical quantity: "
                                f"date={item['as_of_date']}, nm_id={item['nm_id']}, "
                                f"metric={metric_key}"
                            )
            source_material = [
                {
                    "request_id": item["request_id"],
                    "stable_source_id": item["stable_source_id"],
                    "source_revision": item["source_revision"],
                    "business_effective_date": item["business_effective_date"],
                    "affected_nm_ids_json": item["affected_nm_ids_json"],
                    "source_kind": item["source_kind"],
                }
                for item in projection_request_rows
            ]
            source_revision = _fingerprint(source_material)
            plan_fingerprint = _fingerprint(
                {
                    "contract_name": CONTRACT_NAME,
                    "contract_version": CONTRACT_VERSION,
                    "source_requests": source_material,
                    "business_effective_date": business_effective_date,
                    "affected_nm_ids": affected_nm_ids,
                    "candidate_fingerprints": [
                        item["row_fingerprint"] for item in candidate_rows
                    ],
                }
            )
            revision_id = (
                "whbpr_events_" + plan_fingerprint.removeprefix("sha256:")[:20]
            )
            diagnostics = {
                "affected_dates": target_dates,
                "missing_exact_projection_dates": missing_dates,
                "affected_date_count": len(target_dates),
                "affected_sku_count": len(affected_nm_ids),
                "source_request_count": len(projection_request_rows),
                "replay_signal_request_count": (
                    len(request_rows) - len(projection_request_rows)
                ),
                "daily_rows_read": daily_rows_read,
                "external_source_refresh_count": 0,
                "full_vitrina_refresh_count": 0,
                "all_history_rebuild": False,
                "cost_only": cost_only,
                **date_scope,
                "complexity": "O(affected dates × canonical daily capital rows)",
            }
            if inject_failure is not None:
                inject_failure("business_projection_candidate_ready")
            publication = _persist_projection_revision(
                conn,
                revision_id=revision_id,
                stable_source_id="coalesced:" + source_revision,
                source_revision=source_revision,
                business_effective_date=business_effective_date,
                published_at=timestamp,
                plan_fingerprint=plan_fingerprint,
                base_version_id="",
                published_version_id="canonical_own_capital_events",
                affected_nm_ids=affected_nm_ids,
                source_kind="coalesced_capital_events",
                rows=candidate_rows,
                diagnostics=diagnostics,
            )
            if inject_failure is not None:
                inject_failure("business_projection_before_switch")
            conn.execute(
                f"""
                UPDATE {OUTBOX_TABLE}
                SET status='complete',finished_at=?,error=NULL
                WHERE request_id IN ({placeholders}) AND status='running'
                """,
                (timestamp, *request_ids),
            )
            conn.commit()
        return {
            **publication,
            "request_count": len(request_rows),
            "source_revision": source_revision,
            "business_effective_date": business_effective_date,
            "affected_nm_ids": affected_nm_ids,
            "affected_dates": target_dates,
            "diagnostics": {
                **diagnostics,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        }
    except Exception as exc:
        if request_rows:
            with sqlite3.connect(runtime.db_path, timeout=30) as failed_conn:
                failed_conn.row_factory = sqlite3.Row
                ensure_warehouse_projection_source_outbox(failed_conn)
                request_ids = [str(item["request_id"]) for item in request_rows]
                placeholders = ",".join("?" for _ in request_ids)
                failed_conn.execute(
                    f"""
                    UPDATE {OUTBOX_TABLE}
                    SET status='error',finished_at=?,error=?
                    WHERE request_id IN ({placeholders})
                      AND status IN ('queued','running','error')
                    """,
                    (
                        timestamp,
                        str(exc).replace("\n", " ")[:1000],
                        *request_ids,
                    ),
                )
                failed_conn.commit()
        raise


def load_warehouse_business_projection_status(
    runtime: RegistryUploadDbBackedRuntime,
) -> dict[str, Any]:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_warehouse_business_projection_schema(conn)
        state = conn.execute(
            f"SELECT * FROM {STATE_TABLE} WHERE slot=1"
        ).fetchone()
        latest_failure = conn.execute(
            f"""
            SELECT revision_id,stable_source_id,source_revision,
                   business_effective_date,published_at,error
            FROM {REVISION_TABLE}
            WHERE status='failed'
            ORDER BY published_at DESC,revision_id DESC LIMIT 1
            """
        ).fetchone()
        latest_outbox_failure = conn.execute(
            f"""
            SELECT request_id,stable_source_id,source_revision,
                   business_effective_date,
                   COALESCE(finished_at,requested_at) AS published_at,error
            FROM {OUTBOX_TABLE}
            WHERE status='error'
            ORDER BY COALESCE(finished_at,requested_at) DESC,request_id DESC
            LIMIT 1
            """
        ).fetchone()
        queue_counts: dict[str, int] = {}
        outbox_counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                f"""
                SELECT status,COUNT(*) AS count
                FROM {OUTBOX_TABLE}
                GROUP BY status
                """
            ).fetchall()
        }
        queue_table = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table'
              AND name='sheet_vitrina_v1_warehouse_targeted_recalc_queue'
            """
        ).fetchone()
        if queue_table is not None:
            queue_counts = {
                str(row["status"]): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT status,COUNT(*) AS count
                    FROM sheet_vitrina_v1_warehouse_targeted_recalc_queue
                    GROUP BY status
                    """
                ).fetchall()
            }
        if (
            latest_outbox_failure is not None
            and (
                latest_failure is None
                or str(latest_outbox_failure["published_at"] or "")
                > str(latest_failure["published_at"] or "")
            )
        ):
            latest_failure = {
                "revision_id": str(latest_outbox_failure["request_id"]),
                "stable_source_id": str(
                    latest_outbox_failure["stable_source_id"]
                ),
                "source_revision": str(
                    latest_outbox_failure["source_revision"]
                ),
                "business_effective_date": str(
                    latest_outbox_failure["business_effective_date"]
                ),
                "published_at": str(latest_outbox_failure["published_at"]),
                "error": str(latest_outbox_failure["error"] or ""),
            }
    if state is None:
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "not_materialized",
            "revision_no": 0,
            "revision_id": "",
            "queue_counts": queue_counts,
            "outbox_counts": outbox_counts,
            "updating": bool(
                outbox_counts.get("queued")
                or outbox_counts.get("running")
            ),
            "latest_failure": dict(latest_failure) if latest_failure else None,
        }
    return {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        **dict(state),
        "queue_counts": queue_counts,
        "outbox_counts": outbox_counts,
        "updating": bool(
            outbox_counts.get("queued")
            or outbox_counts.get("running")
        ),
        "latest_failure": dict(latest_failure) if latest_failure else None,
    }


def load_current_business_projection_metrics(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    as_of_date: str,
    requested_nm_ids: Iterable[int] | None = None,
) -> dict[int, dict[str, Any]]:
    """Return the exact owned SKU metrics for one business date."""

    selected_date = _iso_date(as_of_date, field_name="as_of_date")
    selected_ids = sorted(
        {
            int(value)
            for value in requested_nm_ids or []
            if int(value) > 0
        }
    )
    conditions = ["as_of_date=?", "nm_id>0"]
    parameters: list[Any] = [selected_date]
    if selected_ids:
        conditions.append(
            "nm_id IN (" + ",".join("?" for _ in selected_ids) + ")"
        )
        parameters.extend(selected_ids)
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_warehouse_business_projection_schema(conn)
        rows = conn.execute(
            f"""
            SELECT nm_id,metrics_json,presentation_json,revision_id,published_at
            FROM {CURRENT_ROW_TABLE}
            WHERE {" AND ".join(conditions)}
            ORDER BY nm_id
            """,
            tuple(parameters),
        ).fetchall()
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        metrics = {
            key: value
            for key, value in _loads(row["metrics_json"], {}).items()
            if key in set(OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS)
        }
        presentation = _loads(row["presentation_json"], {})
        reasons = sorted(
            {
                str(item.get("reason") or "")
                for item in presentation.values()
                if isinstance(item, Mapping) and str(item.get("reason") or "")
            }
        )
        state = (
            "unavailable"
            if any(
                str(item.get("state") or "") == "unavailable"
                for item in presentation.values()
                if isinstance(item, Mapping)
            )
            else "unconfirmed"
            if presentation
            else "confirmed"
        )
        result[int(row["nm_id"])] = {
            **metrics,
            "presentation_state": state,
            "presentation_reason": "; ".join(reasons),
            "presentation_reasons": reasons,
            "stage_presentation": {},
            "_warehouse_business_projection_revision_id": str(
                row["revision_id"]
            ),
            "_warehouse_business_projection_published_at": str(
                row["published_at"]
            ),
        }
    return result


def _data_sheet(snapshot: SheetVitrinaV1Envelope) -> SheetVitrinaWriteTarget:
    sheet = next(
        (item for item in snapshot.sheets if item.sheet_name == DATA_SHEET_NAME),
        None,
    )
    if sheet is None:
        raise WarehouseBusinessProjectionError(
            f"ready snapshot does not contain {DATA_SHEET_NAME}"
        )
    return sheet


def apply_warehouse_business_projection_overlay(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    snapshot: SheetVitrinaV1Envelope,
) -> SheetVitrinaV1Envelope:
    """Merge only owned stable metric keys into an in-memory Vitrina read."""

    sheet = _data_sheet(snapshot)
    target_dates = [str(value) for value in snapshot.date_columns]
    if not target_dates:
        return snapshot
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_warehouse_business_projection_schema(conn)
        placeholders = ",".join("?" for _ in target_dates)
        stored_rows = conn.execute(
            f"""
            SELECT *
            FROM {CURRENT_ROW_TABLE}
            WHERE as_of_date IN ({placeholders})
            ORDER BY as_of_date,nm_id
            """,
            target_dates,
        ).fetchall()
        state = conn.execute(
            f"SELECT * FROM {STATE_TABLE} WHERE slot=1"
        ).fetchone()
    if not stored_rows:
        return snapshot
    overlay = {
        (str(row["as_of_date"]), int(row["nm_id"])): {
            "metrics": _loads(row["metrics_json"], {}),
            "presentation": _loads(row["presentation_json"], {}),
            "revision_id": str(row["revision_id"]),
            "published_at": str(row["published_at"]),
        }
        for row in stored_rows
    }
    rows = [list(row) for row in sheet.rows]
    date_index = {
        value: sheet.header.index(value)
        for value in target_dates
        if value in sheet.header
    }
    changed_cells: list[dict[str, Any]] = []
    for row in rows:
        row_id = str(row[1] or "") if len(row) > 1 else ""
        if "|" not in row_id:
            continue
        scope, metric_key = row_id.split("|", 1)
        if metric_key not in set(OWN_PRODUCT_CAPITAL_METRIC_KEYS):
            continue
        nm_id = 0
        if scope.startswith("SKU:"):
            try:
                nm_id = int(scope.split(":", 1)[1])
            except ValueError:
                continue
        elif scope != "TOTAL":
            continue
        for as_of_date, column_index in date_index.items():
            projected = overlay.get((as_of_date, nm_id))
            if projected is None or metric_key not in projected["metrics"]:
                continue
            value = projected["metrics"].get(metric_key)
            normalized = "" if value is None else value
            before = row[column_index] if len(row) > column_index else ""
            if len(row) <= column_index:
                row.extend([""] * (column_index + 1 - len(row)))
            row[column_index] = normalized
            if before != normalized:
                changed_cells.append(
                    {
                        "row_id": row_id,
                        "as_of_date": as_of_date,
                        "revision_id": projected["revision_id"],
                    }
                )
    metadata = deepcopy(dict(snapshot.metadata or {}))
    presentation = metadata.setdefault("server_cell_presentation", {})
    if not isinstance(presentation, dict):
        presentation = {}
        metadata["server_cell_presentation"] = presentation
    for (as_of_date, nm_id), projected in overlay.items():
        scope = "TOTAL" if nm_id == 0 else f"SKU:{nm_id}"
        for metric_key in set(OWN_PRODUCT_CAPITAL_METRIC_KEYS):
            row_id = f"{scope}|{metric_key}"
            by_date = presentation.setdefault(row_id, {})
            if not isinstance(by_date, dict):
                by_date = {}
                presentation[row_id] = by_date
            cell_presentation = projected["presentation"].get(metric_key)
            if cell_presentation:
                by_date[as_of_date] = deepcopy(cell_presentation)
            else:
                by_date.pop(as_of_date, None)
                if not by_date:
                    presentation.pop(row_id, None)
    metadata["warehouse_business_projection"] = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "status": str(state["status"]) if state is not None else "not_materialized",
        "revision_no": int(state["revision_no"]) if state is not None else 0,
        "revision_id": str(state["revision_id"]) if state is not None else "",
        "source_revision": str(state["source_revision"]) if state is not None else "",
        "business_effective_date": (
            str(state["business_effective_date"]) if state is not None else ""
        ),
        "published_at": str(state["published_at"]) if state is not None else "",
        "owned_metric_keys": sorted(OWN_PRODUCT_CAPITAL_METRIC_KEYS),
        "changed_cells": changed_cells,
        "read_merge_only": True,
        "ready_snapshot_mutated": False,
    }
    replacement = replace(
        sheet,
        rows=rows,
        row_count=len(rows),
        column_count=len(sheet.header),
    )
    return replace(
        snapshot,
        sheets=[
            replacement if item.sheet_name == DATA_SHEET_NAME else item
            for item in snapshot.sheets
        ],
        metadata=metadata,
    )
