"""Default-off FBW supply to FF facility origin assignments.

This Stage 4 service stores only append-only operator evidence.  It does not
post an FF movement, change a WB supply cache row, materialize a pool balance,
or switch any current warehouse producer/consumer.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from packages.application.ff_pool_foundation import (
    FACILITIES_TABLE,
    FEATURE_EPOCHS_TABLE,
    PARITY_TABLE,
    read_ff_pool_feature_state,
)


CONTRACT_NAME = "ff_wb_supply_origin_assignments_v1"
CONTRACT_VERSION = 1
ASSIGNMENTS_TABLE = "sheet_vitrina_v1_wb_supply_ff_origin_assignments"
WB_SUPPLIES_TABLE = "sheet_vitrina_v1_wb_supplies"
POOL = "FBO"
MAX_PAGE_SIZE = 100
MAX_HISTORY_SIZE = 50
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,119}")
SAFE_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
REQUIRED_TABLES = frozenset(
    {
        ASSIGNMENTS_TABLE,
        WB_SUPPLIES_TABLE,
        FACILITIES_TABLE,
        FEATURE_EPOCHS_TABLE,
        PARITY_TABLE,
    }
)


class FfWbSupplyOriginError(ValueError):
    """Stable machine-readable Stage 4 boundary error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Any = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details
        self.http_status = int(http_status)


def ensure_ff_wb_supply_origin_schema(conn: sqlite3.Connection) -> None:
    """Create only the empty append-only Stage 4 evidence table."""

    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {ASSIGNMENTS_TABLE}(
            assignment_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id TEXT NOT NULL UNIQUE,
            request_id TEXT NOT NULL UNIQUE,
            request_fingerprint TEXT NOT NULL,
            wb_supply_cache_key TEXT NOT NULL,
            wb_supply_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            feature_epoch INTEGER NOT NULL REFERENCES {FEATURE_EPOCHS_TABLE}(epoch),
            facility_id TEXT NOT NULL REFERENCES {FACILITIES_TABLE}(facility_id),
            pool TEXT NOT NULL DEFAULT 'FBO' CHECK(pool='FBO'),
            supersedes_assignment_id TEXT UNIQUE
                REFERENCES {ASSIGNMENTS_TABLE}(assignment_id),
            actor TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            assigned_at TEXT NOT NULL
                CHECK(substr(assigned_at,-1,1)='Z' AND julianday(assigned_at) IS NOT NULL),
            CHECK(length(trim(assignment_id)) BETWEEN 8 AND 120),
            CHECK(length(trim(request_id)) BETWEEN 8 AND 120),
            CHECK(length(trim(request_fingerprint)) BETWEEN 8 AND 120),
            CHECK(length(trim(wb_supply_cache_key)) BETWEEN 1 AND 160),
            CHECK(length(trim(wb_supply_id)) BETWEEN 1 AND 80),
            CHECK(length(trim(source_revision)) BETWEEN 8 AND 120),
            CHECK(length(trim(facility_id)) BETWEEN 1 AND 80),
            CHECK(length(trim(actor)) BETWEEN 1 AND 160),
            CHECK(length(reason) <= 500)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS wb_supply_ff_origin_initial
        ON {ASSIGNMENTS_TABLE}(wb_supply_cache_key)
        WHERE supersedes_assignment_id IS NULL;
        CREATE INDEX IF NOT EXISTS wb_supply_ff_origin_by_supply
        ON {ASSIGNMENTS_TABLE}(wb_supply_cache_key,assignment_sequence DESC);
        CREATE INDEX IF NOT EXISTS wb_supply_ff_origin_by_facility
        ON {ASSIGNMENTS_TABLE}(facility_id,assignment_sequence DESC);

        CREATE TRIGGER IF NOT EXISTS wb_supply_ff_origin_supersedes_same_supply
        BEFORE INSERT ON {ASSIGNMENTS_TABLE}
        WHEN NEW.supersedes_assignment_id IS NOT NULL
        BEGIN
            SELECT CASE WHEN NOT EXISTS(
                SELECT 1 FROM {ASSIGNMENTS_TABLE} AS prior
                WHERE prior.assignment_id=NEW.supersedes_assignment_id
                  AND prior.wb_supply_cache_key=NEW.wb_supply_cache_key
            ) THEN RAISE(ABORT,'WB supply origin correction must supersede the same supply') END;
        END;
        CREATE TRIGGER IF NOT EXISTS wb_supply_ff_origin_no_update
        BEFORE UPDATE ON {ASSIGNMENTS_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'WB supply FF origin assignment is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS wb_supply_ff_origin_no_delete
        BEFORE DELETE ON {ASSIGNMENTS_TABLE}
        BEGIN
            SELECT RAISE(ABORT,'WB supply FF origin assignment is append-only');
        END;
        """
    )


class FfWbSupplyOriginAssignments:
    """Bounded query-only reads and guarded append-only assignments."""

    def __init__(
        self,
        *,
        db_path: Path,
        timestamp_factory: Any,
    ) -> None:
        self.db_path = Path(db_path)
        self.timestamp_factory = timestamp_factory

    def assignment_detail(
        self,
        supply_ref: str,
        *,
        aggregate_revision: str = "",
        history_limit: int = 20,
    ) -> dict[str, Any]:
        reference = _identifier(supply_ref, "supply_ref", maximum=160)
        limit = _bounded_int(history_limit, "history_limit", minimum=1, maximum=MAX_HISTORY_SIZE)
        with _connect_readonly(self.db_path) as conn:
            schema = _schema(conn)
            if not schema["available"]:
                return _with_etag(
                    {
                        "contract_name": CONTRACT_NAME,
                        "contract_version": CONTRACT_VERSION,
                        "status": "schema_absent",
                        "feature": _off_feature("schema_absent_default_off"),
                        "supply": None,
                        "current_assignment": None,
                        "history": [],
                        "facilities": [],
                        "assignment_allowed": False,
                        "reason": "schema_absent",
                        "missing_tables": schema["missing_tables"],
                    }
                )
            supply = _resolve_supply(conn, reference)
            if supply is None:
                raise FfWbSupplyOriginError(
                    "wb_supply_not_found", "WB supply was not found in the server cache", http_status=404
                )
            feature = asdict(
                read_ff_pool_feature_state(conn, aggregate_revision=str(aggregate_revision or ""))
            )
            current = _current_assignment(conn, supply["cache_key"])
            history = [
                _assignment_row(row)
                for row in conn.execute(
                    f"""SELECT assignment.*,facility.code AS facility_code,
                               facility.name AS facility_name,facility.active AS facility_active
                        FROM {ASSIGNMENTS_TABLE} AS assignment
                        JOIN {FACILITIES_TABLE} AS facility
                          ON facility.facility_id=assignment.facility_id
                        WHERE assignment.wb_supply_cache_key=?
                        ORDER BY assignment.assignment_sequence DESC LIMIT ?""",
                    (supply["cache_key"], limit),
                ).fetchall()
            ]
            facilities_total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {FACILITIES_TABLE} WHERE active=1"
                ).fetchone()[0]
            )
            facilities = [
                _facility_row(row)
                for row in conn.execute(
                    f"""SELECT facility_id,code,name,active,display_timezone,updated_at
                        FROM {FACILITIES_TABLE}
                        WHERE active=1 ORDER BY code,facility_id LIMIT ?""",
                    (MAX_PAGE_SIZE,),
                ).fetchall()
            ]
        eligible = _is_real_fbw_supply(supply)
        facilities_truncated = facilities_total > MAX_PAGE_SIZE
        allowed = bool(
            feature["writer_effective"]
            and eligible
            and facilities
            and not facilities_truncated
        )
        reason = (
            "assignment_available"
            if allowed
            else "facility_pool_feature_off"
            if not feature["writer_effective"]
            else "wb_supply_has_no_real_id"
            if not eligible
            else "facility_options_truncated"
            if facilities_truncated
            else "no_active_facilities"
        )
        return _with_etag(
            {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "status": "ready",
                "feature": feature,
                "policy": {
                    "pool": POOL,
                    "append_only": True,
                    "correction_requires_current_assignment": True,
                    "creates_pool_movement": False,
                    "mutates_wb": False,
                },
                "supply": supply,
                "eligible": eligible,
                "current_assignment": current,
                "history": history,
                "facilities": facilities,
                "facility_options": {
                    "total": facilities_total,
                    "limit": MAX_PAGE_SIZE,
                    "truncated": facilities_truncated,
                },
                "assignment_allowed": allowed,
                "reason": reason,
            }
        )

    def assignments_page(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        facility_id: str = "",
        search: str = "",
        current_only: bool = True,
        aggregate_revision: str = "",
    ) -> dict[str, Any]:
        page_number = _bounded_int(page, "page", minimum=1, maximum=1_000_000)
        page_size = _bounded_int(limit, "limit", minimum=1, maximum=MAX_PAGE_SIZE)
        facility_filter = _optional_identifier(facility_id, "facility_id", maximum=80)
        search_value = _safe_text(search, "search", maximum=160)
        with _connect_readonly(self.db_path) as conn:
            schema = _schema(conn)
            if not schema["available"]:
                return _with_etag(
                    {
                        "contract_name": CONTRACT_NAME,
                        "contract_version": CONTRACT_VERSION,
                        "status": "schema_absent",
                        "feature": _off_feature("schema_absent_default_off"),
                        "assignments": [],
                        "page": _page(page_number, page_size, 0),
                        "missing_tables": schema["missing_tables"],
                    }
                )
            feature = asdict(
                read_ff_pool_feature_state(conn, aggregate_revision=str(aggregate_revision or ""))
            )
            where: list[str] = []
            args: list[Any] = []
            if current_only:
                where.append(
                    f"NOT EXISTS(SELECT 1 FROM {ASSIGNMENTS_TABLE} AS child "
                    "WHERE child.supersedes_assignment_id=assignment.assignment_id)"
                )
            if facility_filter:
                where.append("assignment.facility_id=?")
                args.append(facility_filter)
            if search_value:
                where.append(
                    "(assignment.wb_supply_id LIKE ? ESCAPE '\\' "
                    "OR assignment.wb_supply_cache_key LIKE ? ESCAPE '\\' "
                    "OR facility.code LIKE ? ESCAPE '\\' OR facility.name LIKE ? ESCAPE '\\')"
                )
                pattern = "%" + _like(search_value) + "%"
                args.extend([pattern, pattern, pattern, pattern])
            where_sql = " WHERE " + " AND ".join(where) if where else ""
            join_sql = (
                f" FROM {ASSIGNMENTS_TABLE} AS assignment "
                f"JOIN {FACILITIES_TABLE} AS facility ON facility.facility_id=assignment.facility_id"
            )
            total = int(conn.execute("SELECT COUNT(*)" + join_sql + where_sql, args).fetchone()[0])
            rows = conn.execute(
                "SELECT assignment.*,facility.code AS facility_code,facility.name AS facility_name,"
                "facility.active AS facility_active"
                + join_sql
                + where_sql
                + " ORDER BY assignment.assignment_sequence DESC LIMIT ? OFFSET ?",
                [*args, page_size, (page_number - 1) * page_size],
            ).fetchall()
        return _with_etag(
            {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "status": "ready",
                "feature": feature,
                "assignments": [_assignment_row(row) for row in rows],
                "page": _page(page_number, page_size, total),
                "filters": {
                    "facility_id": facility_filter,
                    "search": search_value,
                    "current_only": bool(current_only),
                },
            }
        )

    def assign_origin(
        self,
        supply_ref: str,
        payload: Mapping[str, Any],
        *,
        actor: str,
    ) -> dict[str, Any]:
        reference = _identifier(supply_ref, "supply_ref", maximum=160)
        request_id = _request_id(payload.get("request_id"))
        facility_id = _identifier(payload.get("facility_id"), "facility_id", maximum=80)
        if "expected_assignment_id" not in payload:
            raise FfWbSupplyOriginError(
                "expected_assignment_required", "expected_assignment_id is required for optimistic concurrency"
            )
        expected_assignment_id = _optional_identifier(
            payload.get("expected_assignment_id"), "expected_assignment_id", maximum=120
        )
        reason = _safe_text(payload.get("reason"), "reason", maximum=500)
        if payload.get("pool") not in (None, "", POOL):
            raise FfWbSupplyOriginError("invalid_pool", "FBW supply origin pool is fixed to FBO")
        assigned_at = _timestamp(self.timestamp_factory())
        if not self.db_path.is_file():
            raise FfWbSupplyOriginError(
                "runtime_store_missing", "Operational runtime store is missing", http_status=503
            )
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            schema = _schema(connection)
            if not schema["available"]:
                raise FfWbSupplyOriginError(
                    "schema_absent",
                    "WB supply FF-origin schema is not available",
                    details={"missing_tables": schema["missing_tables"]},
                    http_status=503,
                )
            supply = _resolve_supply(connection, reference)
            if supply is None:
                raise FfWbSupplyOriginError(
                    "wb_supply_not_found", "WB supply was not found in the server cache", http_status=404
                )
            if not _is_real_fbw_supply(supply):
                raise FfWbSupplyOriginError(
                    "wb_supply_has_no_real_id",
                    "A preorder without a real WB supply id cannot receive an FF origin",
                    http_status=409,
                )
            feature = asdict(read_ff_pool_feature_state(connection, aggregate_revision=""))
            if not feature["writer_effective"]:
                raise FfWbSupplyOriginError(
                    "facility_pool_feature_off",
                    "FF facility/pool writer is disabled",
                    details={"reason": feature["reason"]},
                    http_status=409,
                )
            facility = connection.execute(
                f"""SELECT facility_id,code,name,active,display_timezone,updated_at
                    FROM {FACILITIES_TABLE} WHERE facility_id=?""",
                (facility_id,),
            ).fetchone()
            if facility is None:
                raise FfWbSupplyOriginError("facility_not_found", "FF facility was not found", http_status=404)
            if not bool(facility["active"]):
                raise FfWbSupplyOriginError(
                    "facility_inactive", "WB supply origin requires an active FF facility", http_status=409
                )
            source_revision = _supply_revision(supply)
            fingerprint = _fingerprint(
                {
                    "contract": CONTRACT_NAME,
                    "wb_supply_cache_key": supply["cache_key"],
                    "wb_supply_id": supply["wb_supply_id"],
                    "facility_id": facility_id,
                    "expected_assignment_id": expected_assignment_id,
                    "pool": POOL,
                    "reason": reason,
                }
            )
            repeated = connection.execute(
                f"""SELECT assignment.*,facility.code AS facility_code,
                           facility.name AS facility_name,facility.active AS facility_active
                    FROM {ASSIGNMENTS_TABLE} AS assignment
                    JOIN {FACILITIES_TABLE} AS facility
                      ON facility.facility_id=assignment.facility_id
                    WHERE assignment.request_id=?""",
                (request_id,),
            ).fetchone()
            if repeated is not None:
                if str(repeated["request_fingerprint"]) != fingerprint:
                    raise FfWbSupplyOriginError(
                        "request_id_conflict", "request_id was already used for a different assignment", http_status=409
                    )
                connection.rollback()
                return {
                    "contract_name": CONTRACT_NAME,
                    "status": "ready",
                    "idempotent": True,
                    "assignment": _assignment_row(repeated),
                    "creates_pool_movement": False,
                }
            current = _current_assignment(connection, supply["cache_key"])
            current_id = str((current or {}).get("assignment_id") or "")
            if current_id != expected_assignment_id:
                raise FfWbSupplyOriginError(
                    "stale_origin_assignment",
                    "WB supply FF origin changed; reload current assignment",
                    details={"expected_assignment_id": expected_assignment_id, "current_assignment": current},
                    http_status=409,
                )
            if current is not None and str(current["facility_id"]) == facility_id:
                raise FfWbSupplyOriginError(
                    "origin_assignment_unchanged", "Selected FF origin is already current", http_status=409
                )
            assignment_id = "wbfo_" + _fingerprint(
                {"request_id": request_id, "cache_key": supply["cache_key"]}
            ).removeprefix("sha256:")[:28]
            connection.execute(
                f"""INSERT INTO {ASSIGNMENTS_TABLE}(
                       assignment_id,request_id,request_fingerprint,
                       wb_supply_cache_key,wb_supply_id,source_revision,feature_epoch,
                       facility_id,pool,supersedes_assignment_id,actor,reason,assigned_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    assignment_id,
                    request_id,
                    fingerprint,
                    supply["cache_key"],
                    supply["wb_supply_id"],
                    source_revision,
                    int(feature["epoch"]),
                    facility_id,
                    POOL,
                    current_id or None,
                    _actor(actor),
                    reason,
                    assigned_at,
                ),
            )
            row = connection.execute(
                f"""SELECT assignment.*,facility.code AS facility_code,
                           facility.name AS facility_name,facility.active AS facility_active
                    FROM {ASSIGNMENTS_TABLE} AS assignment
                    JOIN {FACILITIES_TABLE} AS facility
                      ON facility.facility_id=assignment.facility_id
                    WHERE assignment.assignment_id=?""",
                (assignment_id,),
            ).fetchone()
            connection.commit()
            return {
                "contract_name": CONTRACT_NAME,
                "status": "ready",
                "idempotent": False,
                "assignment": _assignment_row(row),
                "creates_pool_movement": False,
            }
        except FfWbSupplyOriginError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise FfWbSupplyOriginError(
                "origin_assignment_conflict", f"WB supply FF origin conflict: {exc}", http_status=409
            ) from exc
        finally:
            connection.close()


def _schema(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    missing = sorted(REQUIRED_TABLES - tables)
    return {"available": not missing, "missing_tables": missing}


def _resolve_supply(conn: sqlite3.Connection, supply_ref: str) -> dict[str, Any] | None:
    rows = conn.execute(
        f"""SELECT supply_id,cache_key,wb_supply_id,preorder_id,normalized_row_json,
                   raw_list_hash,raw_detail_hash,raw_goods_hash,raw_package_hash,
                   warehouse_id,status_id,source_created_at,supply_date,fact_date,updated_date,synced_at
            FROM {WB_SUPPLIES_TABLE}
            WHERE supply_id=? OR cache_key=? OR wb_supply_id=?
            ORDER BY CASE WHEN wb_supply_id=? THEN 0 WHEN cache_key=? THEN 1 ELSE 2 END,supply_id
            LIMIT 3""",
        (supply_ref, supply_ref, supply_ref, supply_ref, supply_ref),
    ).fetchall()
    unique = {str(row["supply_id"]): row for row in rows}
    if not unique:
        return None
    if len(unique) > 1:
        raise FfWbSupplyOriginError(
            "wb_supply_identity_ambiguous", "WB supply reference matches multiple cached rows", http_status=409
        )
    row = next(iter(unique.values()))
    normalized = _json_object(row["normalized_row_json"])
    return {
        "supply_id": str(row["supply_id"] or ""),
        "cache_key": str(row["cache_key"] or normalized.get("cache_key") or row["supply_id"] or ""),
        "wb_supply_id": str(row["wb_supply_id"] or normalized.get("wb_supply_id") or ""),
        "preorder_id": str(row["preorder_id"] or normalized.get("preorder_id") or ""),
        "number_label": str(normalized.get("number_label") or normalized.get("visible_number") or row["wb_supply_id"] or row["supply_id"] or ""),
        "type_label": str(normalized.get("type_label") or ""),
        "status_id": row["status_id"],
        "warehouse_id": str(row["warehouse_id"] or ""),
        "source_created_at": str(row["source_created_at"] or ""),
        "supply_date": str(row["supply_date"] or ""),
        "fact_date": str(row["fact_date"] or ""),
        "updated_date": str(row["updated_date"] or ""),
        "synced_at": str(row["synced_at"] or ""),
        "raw_list_hash": str(row["raw_list_hash"] or ""),
        "raw_detail_hash": str(row["raw_detail_hash"] or ""),
        "raw_goods_hash": str(row["raw_goods_hash"] or ""),
        "raw_package_hash": str(row["raw_package_hash"] or ""),
    }


def _is_real_fbw_supply(supply: Mapping[str, Any]) -> bool:
    value = str(supply.get("wb_supply_id") or "").strip()
    return value.isdigit() and int(value) > 0


def _supply_revision(supply: Mapping[str, Any]) -> str:
    return _fingerprint(
        {
            "cache_key": supply.get("cache_key"),
            "wb_supply_id": supply.get("wb_supply_id"),
            "raw_list_hash": supply.get("raw_list_hash"),
            "raw_detail_hash": supply.get("raw_detail_hash"),
            "raw_goods_hash": supply.get("raw_goods_hash"),
            "raw_package_hash": supply.get("raw_package_hash"),
        }
    )


def _current_assignment(conn: sqlite3.Connection, cache_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"""SELECT assignment.*,facility.code AS facility_code,
                   facility.name AS facility_name,facility.active AS facility_active
            FROM {ASSIGNMENTS_TABLE} AS assignment
            JOIN {FACILITIES_TABLE} AS facility
              ON facility.facility_id=assignment.facility_id
            WHERE assignment.wb_supply_cache_key=?
              AND NOT EXISTS(
                  SELECT 1 FROM {ASSIGNMENTS_TABLE} AS child
                  WHERE child.supersedes_assignment_id=assignment.assignment_id
              )
            ORDER BY assignment.assignment_sequence DESC LIMIT 1""",
        (cache_key,),
    ).fetchone()
    return _assignment_row(row) if row is not None else None


def _assignment_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "assignment_id": str(row["assignment_id"]),
        "request_id": str(row["request_id"]),
        "wb_supply_cache_key": str(row["wb_supply_cache_key"]),
        "wb_supply_id": str(row["wb_supply_id"]),
        "source_revision": str(row["source_revision"]),
        "feature_epoch": int(row["feature_epoch"]),
        "facility_id": str(row["facility_id"]),
        "facility_code": str(row["facility_code"]),
        "facility_name": str(row["facility_name"]),
        "facility_active": bool(row["facility_active"]),
        "pool": str(row["pool"]),
        "supersedes_assignment_id": str(row["supersedes_assignment_id"] or ""),
        "actor": str(row["actor"]),
        "reason": str(row["reason"] or ""),
        "assigned_at": str(row["assigned_at"]),
    }


def _facility_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "facility_id": str(row["facility_id"]),
        "code": str(row["code"]),
        "name": str(row["name"]),
        "active": bool(row["active"]),
        "display_timezone": str(row["display_timezone"]),
        "updated_at": str(row["updated_at"]),
    }


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    resolved = Path(db_path).resolve()
    if not resolved.is_file():
        raise FfWbSupplyOriginError("runtime_store_missing", "Operational runtime store is missing", http_status=503)
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _page(page: int, limit: int, total: int) -> dict[str, Any]:
    offset = (page - 1) * limit
    return {
        "number": page,
        "limit": limit,
        "total": total,
        "has_previous": page > 1,
        "has_next": offset + limit < total,
    }


def _off_feature(reason: str) -> dict[str, Any]:
    return {
        "epoch": 0,
        "writer_configured": False,
        "reader_configured": False,
        "writer_effective": False,
        "reader_effective": False,
        "parity_status": "not_evaluated",
        "reason": reason,
    }


def _with_etag(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["etag"] = '"' + _fingerprint(result) + '"'
    return result


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _request_id(value: Any) -> str:
    result = str(value or "").strip()
    if not REQUEST_ID_RE.fullmatch(result):
        raise FfWbSupplyOriginError("invalid_request_id", "request_id must contain 8-120 safe characters")
    return result


def _identifier(value: Any, field: str, *, maximum: int) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum or SAFE_TEXT_RE.search(result) or "/" in result or "\\" in result:
        raise FfWbSupplyOriginError(f"invalid_{field}", f"{field} is invalid")
    return result


def _optional_identifier(value: Any, field: str, *, maximum: int) -> str:
    result = str(value or "").strip()
    return _identifier(result, field, maximum=maximum) if result else ""


def _safe_text(value: Any, field: str, *, maximum: int) -> str:
    result = str(value or "").strip()
    if len(result) > maximum or SAFE_TEXT_RE.search(result):
        raise FfWbSupplyOriginError(f"invalid_{field}", f"{field} is invalid")
    return result


def _actor(value: Any) -> str:
    result = _safe_text(value, "actor", maximum=160)
    return result or "unknown"


def _timestamp(value: Any) -> str:
    result = str(value or "").strip()
    if not result.endswith("Z"):
        raise FfWbSupplyOriginError("invalid_timestamp", "assigned_at must be UTC with Z suffix")
    return result


def _bounded_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise FfWbSupplyOriginError(f"invalid_{field}", f"{field} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise FfWbSupplyOriginError(f"invalid_{field}", f"{field} is outside the allowed range")
    return result


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
