"""Opt-in, bounded Web Vitrina window protocol.

The legacy page composition is deliberately untouched.  A manifest reads
ready headers and input revisions; only a requested chunk builds business
cells.  Jobs run off the single-thread HTTP request loop with a fixed queue.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from hashlib import sha256
import base64
import binascii
import gzip
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import sys
from threading import Event, RLock
import time
import traceback
from types import SimpleNamespace
from typing import Any, Callable, Mapping


def _log_unexpected_window_error(operation: str, exc: Exception) -> None:
    """Log only fixed diagnostic fields; worker exceptions may contain business data."""

    try:
        root = Path(__file__).resolve().parents[2]
        frames = []
        for frame in traceback.extract_tb(exc.__traceback__):
            try:
                relative = Path(frame.filename).resolve().relative_to(root)
            except ValueError:
                continue
            frames.append({
                "file": relative.as_posix(), "function": frame.name,
                "line": frame.lineno,
            })
        print(json.dumps({
            "event": "web_vitrina_window_v3_unexpected_error_v1",
            "operation": operation if operation in {"manifest", "global", "chunk"} else "other",
            "exception_class": type(exc).__name__,
            "frames": frames[-8:],
        }, ensure_ascii=True, separators=(",", ":")), file=sys.stderr, flush=True)
    except Exception:
        # Diagnostics must not change the public error or job lifecycle.
        pass

from packages.business_time import (
    business_date_from_timestamp, current_business_date_iso, default_business_as_of_date,
)
from packages.application.ready_publication import (
    INVENTORY_PREPARATION_TABLES, MATERIAL_TABLES,
)
from packages.application.sheet_vitrina_v1_web_vitrina import (
    SheetVitrinaV1WebVitrinaBlock, _effective_web_vitrina_metrics,
    _apply_funnel_operator_presentation, _include_authenticated_discount_total_row,
    _include_buyout_percent_rows, _include_proxy_v4_unit_margin_rows,
    _normalize_rows, _require_data_sheet,
    _build_schema,
)
from packages.application.sheet_vitrina_v1_authenticated_buyer import (
    AVG_EFFECTIVE_DISCOUNT_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_buyout_percent import (
    BUYOUT_PERCENT_METRIC_KEY, BUYOUT_PERCENT_MATURITY_DAYS,
)
from packages.application.sheet_vitrina_v1_proxy_v4 import (
    PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_inventory_planning import COMBINED_TOTAL_ALIAS_KEY
from packages.application.sheet_vitrina_v1_inventory_planning import extend_rows_with_inventory_planning
from packages.application.web_vitrina_management_history import (
    ensure_current_proxy_rows, has_complete_recovery,
)
from packages.application.web_vitrina_window_read_context import window_read_context
from packages.application.web_vitrina_ready_header_cache import ReadyHeaderCache
from packages.application.web_vitrina_page_composition import (
    _build_metric_option_groups, _build_metric_options, _build_sort_options,
    _build_labeled_options, _count_metric_adapter_rows, _count_rows_from_adapter,
    _merge_metric_catalog, _resolve_default_sort_value,
    WEB_VITRINA_PAGE_STATE_NAMESPACE,
)
from packages.application.web_vitrina_gravity_table_adapter import (
    _build_columns as _adapter_build_columns,
    _build_filters as _adapter_build_filters,
    _build_renderer_registry as _adapter_build_renderers,
    _build_rows as _adapter_build_rows,
    _build_sorts as _adapter_build_sorts,
    _renderer_id,
)
from packages.application.web_vitrina_view_model import (
    _FORMATTER_LIBRARY, _build_columns as _view_build_columns,
    _build_sorts as _view_build_sorts, _build_cell,
    build_web_vitrina_view_model,
)
from packages.contracts.web_vitrina_gravity_table_adapter import WebVitrinaGravityTableRenderer
from packages.application.web_vitrina_compact_table import CELL_DEFAULTS, CELL_FIELDS


SCHEMA_VERSION = 3
WINDOW_FORMAT = "window_v3"
MAX_CHUNK_DATES = 2
MAX_CHUNK_ROWS = 2000
MAX_SCOPED_BUILD_ROWS = MAX_CHUNK_ROWS
MAX_SELECTED_ROWS = 200
MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_SESSIONS = 4
MAX_SESSION_BYTES = 64 * 1024 * 1024
SESSION_TTL_SECONDS = 15 * 60
MAX_JOB_RESULT_BYTES = 16 * 1024 * 1024
JOB_TTL_SECONDS = 60
MAX_GLOBAL_BYTES = 8 * 1024 * 1024
GLOBAL_TTL_SECONDS = 60
MAX_PENDING_JOBS = 3
MAX_QUERY_BYTES = 4096


class WindowV3Error(Exception):
    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status = status

    def payload(self) -> dict[str, Any]:
        return {
            "response_schema_version": SCHEMA_VERSION,
            "window_format": WINDOW_FORMAT,
            "state": "stale" if self.status == 409 else "error",
            "code": self.code,
            "message": str(self),
        }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + sha256(_canonical_bytes(value)).hexdigest()


def _dates_inclusive(start: str, end: str) -> list[str]:
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end)
    except ValueError as exc:
        raise WindowV3Error("window_invalid_period", 422, "Некорректные даты периода.") from exc
    days = (last - first).days + 1
    if days < 1:
        raise WindowV3Error("window_invalid_period", 422, "Конец периода раньше начала.")
    # The manifest materializes date columns, sort descriptions, filter sort
    # options and Python objects per day.  Reserve a conservative 4 KiB/day
    # before constructing any of those arrays; this is a byte budget, not a
    # business maximum duration.
    if days * 4096 > MAX_SESSION_BYTES:
        raise WindowV3Error("window_calendar_resource_limit", 413, "Метаданные дат не помещаются в лимит сессии.")
    return [(first + timedelta(days=offset)).isoformat() for offset in range(days)]


@dataclass(frozen=True)
class _ReadyHeader:
    bundle_version: str
    as_of_date: str
    snapshot_id: str
    activated_at: str
    refreshed_at: str
    date_columns: tuple[str, ...]
    book_bindings_json: str
    revision: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.bundle_version, self.as_of_date)


@dataclass(frozen=True)
class _DateBinding:
    date: str
    coverage: str
    source_as_of_date: str
    source_key: tuple[str, str] | None

    def payload(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "coverage": self.coverage,
            "source_as_of_date": self.source_as_of_date,
        }


@dataclass(frozen=True)
class _HeaderScan:
    rows: list[list[str]]
    canonical_proxy_dates: frozenset[str]
    legacy_wb_history_present: bool


def _ready_headers(conn: sqlite3.Connection) -> list[_ReadyHeader]:
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "sheet_vitrina_v1_ready_revisions" not in tables:
        raise WindowV3Error("window_input_schema_missing", 409, "Ревизии готовых снимков отсутствуют.")
    rows = conn.execute(
        """SELECT s.bundle_version,s.as_of_date,s.snapshot_id,s.activated_at,
                  s.refreshed_at,json_extract(s.plan_json,'$.date_columns') AS dates_json,
                  json_extract(s.plan_json,'$.metadata.fbs_accounting_bindings'),r.revision
           FROM sheet_vitrina_v1_ready_snapshots AS s
           LEFT JOIN sheet_vitrina_v1_ready_revisions AS r
             ON r.bundle_version=s.bundle_version AND r.as_of_date=s.as_of_date
           ORDER BY s.activated_at DESC,s.refreshed_at DESC,s.as_of_date DESC,
                    s.bundle_version DESC"""
    ).fetchall()
    headers: list[_ReadyHeader] = []
    for row in rows:
        if row[7] is None:
            raise WindowV3Error("window_input_revision_missing", 409, "Ревизия ready-снимка отсутствует.")
        try:
            columns = json.loads(row[5] or "[]")
        except json.JSONDecodeError as exc:
            raise WindowV3Error("window_ready_header_invalid", 409, "Колонки ready-снимка повреждены.") from exc
        if not isinstance(columns, list):
            raise WindowV3Error("window_ready_header_invalid", 409, "Колонки ready-снимка повреждены.")
        headers.append(_ReadyHeader(
            bundle_version=str(row[0]), as_of_date=str(row[1]),
            snapshot_id=str(row[2]), activated_at=str(row[3]),
            refreshed_at=str(row[4]), date_columns=tuple(str(item) for item in columns),
            book_bindings_json=str(row[6] or "{}"), revision=int(row[7]),
        ))
    return headers


def _selected_default_header(
    headers: list[_ReadyHeader], current_bundle: str, default_date: str,
) -> _ReadyHeader | None:
    current = [item for item in headers if item.bundle_version == current_bundle]
    return next((item for item in current if item.as_of_date == default_date), None) or (
        max(current, key=lambda item: item.as_of_date) if current else None
    )


def _covering_recovery(conn: sqlite3.Connection, header: _ReadyHeader, day: str) -> bool:
    row = conn.execute(
        """SELECT json_extract(plan_json,'$.metadata.server_cell_presentation')
           FROM sheet_vitrina_v1_ready_snapshots
           WHERE bundle_version=? AND as_of_date=?""",
        header.key,
    ).fetchone()
    if row is None:
        return False
    try:
        presentation = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        return False
    return has_complete_recovery({"server_cell_presentation": presentation}, day)


def _select_bindings(
    conn: sqlite3.Connection, headers: list[_ReadyHeader], dates: list[str],
    current_bundle: str, default_date: str,
) -> tuple[list[_DateBinding], _ReadyHeader | None]:
    exact: dict[str, _ReadyHeader] = {}
    covering: dict[str, _ReadyHeader] = {}
    requested = set(dates)
    for header in headers:
        if header.as_of_date in requested:
            exact.setdefault(header.as_of_date, header)
        for day in header.date_columns:
            if day in requested:
                covering.setdefault(day, header)
    default = _selected_default_header(headers, current_bundle, default_date)
    default_columns = set(default.date_columns) if default else set()
    bindings: list[_DateBinding] = []
    for day in dates:
        selected = exact.get(day)
        if selected is not None:
            bindings.append(_DateBinding(day, "exact", day, selected.key))
        elif default is not None and day in default_columns:
            bindings.append(_DateBinding(day, "covered", default.as_of_date, default.key))
        else:
            selected = covering.get(day)
            if selected is not None and _covering_recovery(conn, selected, day):
                bindings.append(_DateBinding(day, "covered", selected.as_of_date, selected.key))
            else:
                bindings.append(_DateBinding(day, "missing", "", None))
    return bindings, default


def _hash_query(conn: sqlite3.Connection, sql: str, parameters: tuple[Any, ...] = ()) -> str:
    digest = sha256()
    for row in conn.execute(sql, parameters):
        digest.update(_canonical_bytes(list(row)))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}


def _source_revisions(conn: sqlite3.Connection, tables: set[str]) -> dict[str, int | None]:
    revision_table = "sheet_vitrina_v1_ready_input_revisions"
    if revision_table not in tables:
        raise WindowV3Error("window_input_schema_missing", 409, "Ревизии входных источников отсутствуют.")
    revisions = {str(row[0]): int(row[1]) for row in conn.execute(
        f"SELECT source_table,revision FROM {revision_table}"
    )}
    # Registry changes are fingerprinted for the selected bundle below.  A
    # publication of another bundle must not invalidate this visible window.
    selected = tuple(table for table in (
        *MATERIAL_TABLES, *INVENTORY_PREPARATION_TABLES,
    ) if table not in {
        "registry_upload_current_state", "registry_upload_config_v2",
        "registry_upload_metrics_v2", "registry_upload_formulas_v2",
    })
    result: dict[str, int | None] = {}
    for table in selected:
        if table in tables and table not in revisions:
            raise WindowV3Error("window_input_revision_missing", 409, f"Нет ревизии источника {table}.")
        result[table] = revisions.get(table) if table in tables else None
    return result


def _scoped_table_hash(
    conn: sqlite3.Connection, tables: set[str], table: str,
    *, date_column: str | None = None, dates: list[str] | None = None,
) -> str:
    if table not in tables:
        return "missing"
    if date_column and dates is not None:
        if not dates:
            return _digest([])
        placeholders = ",".join("?" for _ in dates)
        return _hash_query(conn,
            f'SELECT * FROM "{table}" WHERE "{date_column}" IN ({placeholders}) ORDER BY "{date_column}",rowid',
            tuple(dates),
        )
    return _hash_query(conn, f'SELECT * FROM "{table}" ORDER BY rowid')


def _temporal_buyout_hash(
    conn: sqlite3.Connection, tables: set[str], dates: list[str],
) -> str:
    if "temporal_source_snapshots" not in tables:
        return "missing"
    if not dates:
        return _digest([])
    from packages.application.sheet_vitrina_v1_buyout_percent import (
        BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY,
        SALES_FUNNEL_HISTORY_SOURCE_KEY,
    )
    placeholders = ",".join("?" for _ in dates)
    return _hash_query(conn,
        "SELECT source_key,snapshot_date,captured_at,payload_json "
        "FROM temporal_source_snapshots WHERE source_key IN (?,?) "
        f"AND snapshot_date IN ({placeholders}) ORDER BY source_key,snapshot_date",
        (BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY, SALES_FUNNEL_HISTORY_SOURCE_KEY, *dates),
    )


def _current_supplier_certification_material(
    conn: sqlite3.Connection, tables: set[str], *, business_date: str, dates: list[str],
) -> Any:
    """Only today's active functional balance can revalidate mutable shipments."""
    if business_date not in dates:
        return ["not_used"]
    required = {
        "sheet_vitrina_v1_warehouse_functional_cutovers",
        "sheet_vitrina_v1_warehouse_functional_active",
        "sheet_vitrina_v1_warehouse_functional_versions",
        "sheet_vitrina_v1_warehouse_functional_balances",
        "sheet_vitrina_v1_warehouse_wb_snapshots",
    }
    if not required <= tables:
        return ["no_functional_boundary"]
    cutover = conn.execute(
        "SELECT cutover_at FROM sheet_vitrina_v1_warehouse_functional_cutovers "
        "WHERE cutover_id='warehouse_functional_cutover_v1' AND status='posted'"
    ).fetchone()
    if cutover is None or business_date < business_date_from_timestamp(str(cutover[0])):
        return ["no_current_revalidation"]
    active = conn.execute(
        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone()
    if active is None:
        return ["no_active_version"]
    candidates = conn.execute(
        "SELECT version.version_id,version.business_effective_date "
        "FROM sheet_vitrina_v1_warehouse_functional_versions version "
        "JOIN sheet_vitrina_v1_warehouse_wb_snapshots snapshot "
        "ON snapshot.version_id=version.version_id "
        "WHERE version.cutover_id='warehouse_functional_cutover_v1' "
        "AND version.status='good' AND snapshot.snapshot_date=? "
        "ORDER BY COALESCE(NULLIF(version.published_at,''),version.created_at) DESC, "
        "version.created_at DESC,version.version_id DESC",
        (business_date,),
    ).fetchall()
    selected = next((str(row[0]) for row in candidates
                     if str(row[1] or business_date) <= business_date), "")
    active_version = str(active[0])
    if selected != active_version:
        return ["not_active_today", selected, active_version]
    from packages.application.warehouse_functional import _shipment_ids_from_provenance
    shipment_ids: set[str] = set()
    for row in conn.execute(
        "SELECT provenance_json FROM sheet_vitrina_v1_warehouse_functional_balances "
        "WHERE version_id=? ORDER BY nm_id,warehouse_key", (active_version,),
    ):
        try:
            provenance = json.loads(row[0] or "{}")
        except json.JSONDecodeError as exc:
            raise WindowV3Error("window_supplier_provenance_invalid", 409,
                                "Происхождение складской себестоимости повреждено.") from exc
        shipment_ids.update(_shipment_ids_from_provenance(provenance))
    if not shipment_ids:
        return ["no_supplier_shipments", active_version]
    source_tables = (
        ("sheet_vitrina_v1_supplier_shipments", "shipment_id"),
        ("sheet_vitrina_v1_supplier_shipment_lines", "shipment_id"),
        ("sheet_vitrina_v1_cny_ledger_operations", "source_order_id"),
        ("sheet_vitrina_v1_supplier_financial_documents", "supplier_order_id"),
        ("sheet_vitrina_v1_supplier_financial_expense_lines", "supplier_order_id"),
        ("sheet_vitrina_v1_cny_documents", "source_order_id"),
        ("sheet_vitrina_v1_supplier_bank_operation_assignments", "supplier_order_id"),
        ("sheet_vitrina_v1_supplier_payment_fee_confirmations", "supplier_order_id"),
    )
    required_sources = {name for name, _ in source_tables[:5]}
    if required_sources - tables:
        raise WindowV3Error(
            "window_supplier_source_missing", 409,
            "Источники подтверждения складской себестоимости недоступны.",
        )
    material: list[Any] = []
    for shipment_id in sorted(shipment_ids):
        selected_rows: list[Any] = []
        for table, column in source_tables:
            selected_rows.append([table, (
                _hash_query(conn, f'SELECT * FROM "{table}" WHERE "{column}"=? ORDER BY rowid',
                            (shipment_id,)) if table in tables else "missing"
            )])
        for table, where, args in (
            ("sheet_vitrina_v1_warehouse_supplier_cost_states",
             "version_id=? AND shipment_id=?", (active_version, shipment_id)),
            ("sheet_vitrina_v1_warehouse_supplier_cost_state_corrections",
             "version_id=? AND shipment_id=?", (active_version, shipment_id)),
            ("sheet_vitrina_v1_warehouse_targeted_recalc_queue",
             "stable_source_id IN (?,?)",
             (f"supplier_costs:{shipment_id}", f"supplier_shipment:{shipment_id}")),
        ):
            selected_rows.append([table, (
                _hash_query(conn, f'SELECT * FROM "{table}" WHERE {where} ORDER BY rowid', args)
                if table in tables else "missing"
            )])
        correction = "sheet_vitrina_v1_warehouse_supplier_cost_state_corrections"
        replay = "sheet_vitrina_v1_warehouse_supplier_cost_state_replays"
        rollback = "sheet_vitrina_v1_warehouse_supplier_cost_state_replay_rollbacks"
        if {correction, replay, rollback} <= tables:
            selected_rows.append(["effective_replay", _hash_query(conn,
                f"SELECT correction.*,replay.*,rollback.* FROM {correction} correction "
                f"JOIN {replay} replay ON replay.replay_id=correction.replay_id "
                f"LEFT JOIN {rollback} rollback ON rollback.replay_id=replay.replay_id "
                "WHERE correction.version_id=? AND correction.shipment_id=? "
                "ORDER BY replay.sequence_no,replay.replay_id",
                (active_version, shipment_id),
            )])
        else:
            selected_rows.append(["effective_replay", "missing"])
        material.append([shipment_id, selected_rows])
    return ["active_supplier_shipments", active_version, _digest(material)]


def _active_breakglass_hash(conn: sqlite3.Connection, tables: set[str]) -> str:
    operation_table = "sheet_vitrina_v1_breakglass_last_good_operations"
    cells_table = "sheet_vitrina_v1_breakglass_last_good_cells"
    revoked_table = "sheet_vitrina_v1_breakglass_last_good_revocations"
    if {operation_table, cells_table, revoked_table} - tables:
        return "missing"
    operation = conn.execute(
        f"SELECT operation.* FROM {operation_table} operation "
        f"LEFT JOIN {revoked_table} revoked ON revoked.operation_id=operation.operation_id "
        "WHERE revoked.operation_id IS NULL "
        "ORDER BY operation.applied_at DESC,operation.operation_id DESC LIMIT 1"
    ).fetchone()
    if operation is None:
        return _digest(["no_active_operation"])
    operation_id = str(operation[0])
    cells = conn.execute(
        f"SELECT * FROM {cells_table} WHERE operation_id=? ORDER BY row_id",
        (operation_id,),
    ).fetchall()
    return _digest([list(operation), [list(row) for row in cells]])


def _bound_book_versions(
    headers: Mapping[tuple[str, str], _ReadyHeader], selected_keys: set[tuple[str, str]],
    bindings: list[_DateBinding],
) -> list[str]:
    dates_by_key: dict[tuple[str, str], set[str]] = {}
    for binding in bindings:
        if binding.source_key is not None:
            dates_by_key.setdefault(binding.source_key, set()).add(binding.date)
    versions: set[str] = set()
    for key in selected_keys:
        header = headers.get(key)
        if header is None:
            raise WindowV3Error("window_version_stale", 409, "Ready-снимок исчез во время чтения привязок.")
        try:
            by_date = json.loads(header.book_bindings_json)
        except json.JSONDecodeError as exc:
            raise WindowV3Error("window_ready_invalid", 409, "Привязки FBS повреждены.") from exc
        if not isinstance(by_date, Mapping):
            raise WindowV3Error("window_ready_invalid", 409, "Привязки FBS повреждены.")
        for day in dates_by_key.get(key, ()):
            bound = by_date.get(day)
            if isinstance(bound, Mapping) and bound.get("book_version"):
                versions.add(str(bound["book_version"]))
    return sorted(versions)


def _book_material(
    context: Any, runtime_dir: Path, versions: list[str], *, include_current: bool,
) -> Any:
    if not include_current and not versions:
        return ["not_used", []]
    book = context.borrow_book(runtime_dir / "fbs-snapshot-accounting.sqlite3")
    if book is None:
        if versions:
            raise WindowV3Error("window_bound_book_missing", 409, "Привязанный FBS-снимок отсутствует.")
        return ["missing", []]
    current_version = ""
    if include_current:
        current = book.execute(
            "SELECT version FROM accounting_current WHERE singleton=1"
        ).fetchone()
        current_version = str(current[0]) if current is not None else ""
    selected = sorted({*versions, current_version} - {""})
    revisions: list[list[str]] = []
    for version in selected:
        row = book.execute(
            "SELECT version,payload FROM accounting_revisions WHERE version=?",
            (version,),
        ).fetchone()
        if row is None:
            raise WindowV3Error("window_bound_book_missing", 409, "Привязанный FBS-снимок отсутствует.")
        revisions.append([str(row[0]), _digest(str(row[1]))])
    return [current_version, revisions]


def _input_material(
    conn: sqlite3.Connection, runtime_dir: Path, dates: list[str],
    *, business_date: str, default_as_of_date: str, context,
    header_cache: ReadyHeaderCache | None = None,
) -> tuple[dict[str, Any], list[_DateBinding], _ReadyHeader | None, list[_ReadyHeader]]:
    tables = _table_names(conn)
    state = conn.execute(
        "SELECT bundle_version,activated_at FROM registry_upload_current_state WHERE slot=1"
    ).fetchone()
    if state is None:
        raise WindowV3Error("window_bundle_missing", 409, "Текущий набор метрик отсутствует.")
    bundle, activated_at = str(state[0]), str(state[1])
    headers = header_cache.load(conn, _ready_headers) if header_cache is not None else _ready_headers(conn)
    bindings, default = _select_bindings(conn, headers, dates, bundle, default_as_of_date)
    by_key = {header.key: header for header in headers}
    selected_keys = {item.source_key for item in bindings if item.source_key is not None}
    if default is not None:
        selected_keys.add(default.key)
    selected_ready = [
        [*key, by_key[key].revision, by_key[key].snapshot_id]
        for key in sorted(selected_keys)
    ]
    source_dates = sorted({item.date for item in bindings if item.source_key is not None})
    scoped: dict[str, Any] = {}
    for table, column in (
        ("sheet_vitrina_v1_warehouse_business_projection_current_rows", "as_of_date"),
        ("sheet_vitrina_v1_inventory_history_finalizations", "business_date"),
        ("sheet_vitrina_v1_inventory_history_captures", "business_date"),
        ("sheet_vitrina_v1_wb_cost_daily_state", "as_of_date"),
    ):
        scoped[table] = _scoped_table_hash(conn, tables, table, date_column=column, dates=source_dates)
    maturity_boundary = (
        date.fromisoformat(business_date) - timedelta(days=BUYOUT_PERCENT_MATURITY_DAYS)
    ).isoformat()
    mature_source_dates = [day for day in source_dates if day <= maturity_boundary]
    scoped["temporal_buyout"] = _temporal_buyout_hash(conn, tables, mature_source_dates)
    scoped["projection_state"] = _scoped_table_hash(
        conn, tables, "sheet_vitrina_v1_warehouse_business_projection_state"
    )
    scoped["active_breakglass"] = _active_breakglass_hash(conn, tables)
    scoped["current_supplier_certification"] = _current_supplier_certification_material(
        conn, tables, business_date=business_date, dates=dates,
    )
    if "sheet_vitrina_v1_ready_publications" in tables:
        scoped["selected_ready_publications"] = _digest([
            [*key, _hash_query(conn,
                "SELECT * FROM sheet_vitrina_v1_ready_publications "
                "WHERE bundle_version=? AND as_of_date=? "
                "ORDER BY operation_id,attempt_id", key)]
            for key in sorted(selected_keys)
        ])
    else:
        scoped["selected_ready_publications"] = "missing"
    bound_versions = _bound_book_versions(by_key, selected_keys, bindings)
    book_material = _book_material(
        context, runtime_dir, bound_versions,
        include_current=business_date in dates,
    )
    for name in (".auto-updates-policy.json", ".web-vitrina-fbs-lifecycle-last-good.json"):
        context.read_file_once(runtime_dir / name)
    from packages.application.inventory_planning_read_model import InventoryPlanningReadModel
    from packages.application.web_vitrina_fbs_lifecycle_last_good import load_owner_paused_fallback
    fallback = load_owner_paused_fallback(runtime_dir)
    planning_options = {"business_date": business_date}
    if fallback is not None:
        planning_options["lifecycle_quality_resolver"] = fallback.resolve
    planning_etag = InventoryPlanningReadModel(db_path=context.operational_db_path).current(
        **planning_options,
    )["etag"]
    material = {
        "operational_generation": context.operational_generation,
        "business_date": business_date,
        "default_as_of_date": default_as_of_date,
        "current_bundle": [bundle, activated_at],
        "registry_config": _hash_query(conn,
            "SELECT * FROM registry_upload_config_v2 WHERE bundle_version=? ORDER BY display_order,nm_id", (bundle,)),
        "registry_metrics": _hash_query(conn,
            "SELECT * FROM registry_upload_metrics_v2 WHERE bundle_version=? ORDER BY metric_key", (bundle,)),
        "registry_formulas": _hash_query(conn,
            "SELECT * FROM registry_upload_formulas_v2 WHERE bundle_version=? ORDER BY formula_id", (bundle,)),
        "bindings": [[item.date,item.coverage,item.source_as_of_date,item.source_key] for item in bindings],
        "selected_ready": selected_ready,
        "source_revisions": _source_revisions(conn, tables),
        "current_planning_etag": planning_etag,
        "scoped": scoped,
        "book": book_material,
        "files": context.file_content_digests(),
    }
    return material, bindings, default, headers


def _row_header_keys(
    bindings: list[_DateBinding], default: _ReadyHeader | None,
) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    if default is not None:
        ordered.append(default.key)
    ordered.extend(
        item.source_key for item in reversed(bindings) if item.source_key is not None
    )
    ordered.extend(
        item.source_key for item in bindings if item.source_key is not None
    )
    seen: set[tuple[str, str]] = set()
    result = []
    for key in ordered:
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def _scan_row_headers(
    db_path: Path, *, keys: list[tuple[str, str]],
    expected_revisions: Mapping[tuple[str, str], int],
    bindings: list[_DateBinding],
    check_step: Callable[[], None],
) -> _HeaderScan:
    """Stream each selected ready plan once, keeping only unique row labels/IDs."""
    rows_by_id: dict[str, list[str]] = {}
    dates_by_key: dict[tuple[str, str], set[str]] = {}
    for binding in bindings:
        if binding.source_key is not None:
            dates_by_key.setdefault(binding.source_key, set()).add(binding.date)
    canonical_dates: set[str] = set()
    legacy_wb_history_present = False
    for key in keys:
        check_step()
        with window_read_context(db_path) as context:
            conn = context.borrow(db_path)
            record = conn.execute(
                """SELECT snapshot.plan_json,revision.revision
                   FROM sheet_vitrina_v1_ready_snapshots AS snapshot
                   JOIN sheet_vitrina_v1_ready_revisions AS revision
                     ON revision.bundle_version=snapshot.bundle_version
                    AND revision.as_of_date=snapshot.as_of_date
                   WHERE snapshot.bundle_version=? AND snapshot.as_of_date=?""",
                key,
            ).fetchone()
            if record is None or int(record[1]) != expected_revisions[key]:
                raise WindowV3Error("window_version_stale", 409, "Ready-снимок изменился во время чтения каталога.")
            try:
                plan = json.loads(record[0])
            except json.JSONDecodeError as exc:
                raise WindowV3Error("window_ready_invalid", 409, "Ready-снимок повреждён.") from exc
            sheet = next(
                (item for item in plan.get("sheets", []) if item.get("sheet_name") == "DATA_VITRINA"),
                None,
            )
            if sheet is None:
                raise WindowV3Error("window_ready_invalid", 409, "DATA_VITRINA отсутствует.")
            date_indexes = {
                day: index + 2
                for index, day in enumerate(plan.get("date_columns") or [])
                if day in dates_by_key.get(key, set())
            }
            presentation = (plan.get("metadata") or {}).get("server_cell_presentation") or {}
            for raw in sheet.get("rows") or []:
                if not isinstance(raw, list) or len(raw) < 2:
                    continue
                row_id = str(raw[1] or "").strip()
                if row_id and row_id not in rows_by_id:
                    rows_by_id[row_id] = [str(raw[0] or ""), row_id]
                if row_id.endswith("|" + COMBINED_TOTAL_ALIAS_KEY) or row_id.endswith(
                    "|total_" + COMBINED_TOTAL_ALIAS_KEY
                ):
                    legacy_wb_history_present |= any(
                        index < len(raw) and raw[index] not in (None, "")
                        for index in date_indexes.values()
                    )
                if row_id.endswith("|" + PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY) or row_id.endswith(
                    "|" + PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY
                ):
                    by_date = presentation.get(row_id) or {}
                    if isinstance(by_date, Mapping):
                        canonical_dates.update(
                            day for day in date_indexes
                            if isinstance(by_date.get(day), Mapping)
                            and by_date[day].get("calculation_contract") == "catalog_economics_v1"
                            and by_date[day].get("source_as_of_date") == day
                        )
    return _HeaderScan(
        rows=list(rows_by_id.values()),
        canonical_proxy_dates=frozenset(canonical_dates),
        legacy_wb_history_present=legacy_wb_history_present,
    )


def _catalog_core_rows(
    scan: _HeaderScan, *, block: SheetVitrinaV1WebVitrinaBlock,
    business_date: str, selected_dates: list[str],
) -> list[Any]:
    """Reuse legacy row-identity rules, with no date values or daily calculation.

    Planning/history and exact-bound current FBS capital rows are added by
    separate metadata selectors.  This base family has unconditional IDs.
    """
    current = block.runtime.load_current_state()
    enabled = [item for item in current.config_v2 if item.enabled]
    metrics = {item.metric_key: item for item in _effective_web_vitrina_metrics(current.metrics_v2)}
    rows = _normalize_rows(
        scan.rows, date_columns=[],
        config_by_nm_id={int(item.nm_id): item for item in current.config_v2},
        metrics_by_key=metrics, row_updated_at_by_id={},
    )
    if set(selected_dates) - scan.canonical_proxy_dates:
        rows = _include_proxy_v4_unit_margin_rows(
            rows, runtime=block.runtime, date_columns=[], enabled_config=enabled,
            sku_metric=metrics[PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY],
            total_metric=metrics[PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY],
            parameters_for_date=block.proxy_v4_parameters_resolver,
        )
    rows = _include_buyout_percent_rows(
        rows, runtime=block.runtime, date_columns=[], source_snapshot_dates=[],
        enabled_config=enabled, metric=metrics[BUYOUT_PERCENT_METRIC_KEY],
        current_business_date=date.fromisoformat(business_date),
    )
    return rows


def _history_catalog(
    db_path: Path, runtime_dir: Path, bindings: list[_DateBinding],
    *, check_step: Callable[[], None], verify_context: Callable[[Any], None],
) -> dict[str, Any]:
    """Select history row identities without a period-wide value matrix.

    Finalized captures use indexed metadata. Accepted preliminary captures are
    admitted only by the existing exact ready/book/receipt proof, run for the
    few dates that actually carry an accounting binding.
    """
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan
    from packages.application.management_inventory_history import read_management_inventory_history
    from packages.application.sheet_vitrina_v1_inventory_history import KNOWN_BAD_CAPTURES
    from packages.application.inventory_quantity import CONTRACT as QUANTITY_CONTRACT

    history_dates: dict[str, dict[str, Any]] = {}
    facilities: dict[str, dict[str, Any]] = {}
    dates_by_key: dict[tuple[str, str], list[str]] = {}
    for binding in bindings:
        if binding.source_key is not None:
            dates_by_key.setdefault(binding.source_key, []).append(binding.date)
    needed = {
        "sheet_vitrina_v1_inventory_history_finalizations",
        "sheet_vitrina_v1_inventory_history_captures",
        "sheet_vitrina_v1_inventory_history_components",
    }
    for binding in bindings:
        check_step()
        day = binding.date
        with window_read_context(db_path, runtime_dir=runtime_dir) as context:
            conn = context.borrow(db_path)
            if needed <= _table_names(conn):
                selected = conn.execute(
                    "SELECT capture.*,finalization.finalization_id,finalization.finalization_digest "
                    "FROM sheet_vitrina_v1_inventory_history_finalizations finalization "
                    "JOIN sheet_vitrina_v1_inventory_history_captures capture "
                    "ON capture.capture_id=finalization.capture_id "
                    "WHERE finalization.business_date=? "
                    "ORDER BY finalization.finalization_sequence DESC LIMIT 1",
                    (day,),
                ).fetchone()
            else:
                selected = None
            if selected is not None:
                try:
                    roster = json.loads(selected["facility_roster_json"])
                    source_manifest = json.loads(selected["source_manifest_json"])
                except json.JSONDecodeError as exc:
                    raise WindowV3Error("window_history_invalid", 409, "Исторический состав повреждён.") from exc
                for facility in roster if isinstance(roster, list) else []:
                    if isinstance(facility, Mapping) and facility.get("facility_id"):
                        facilities[str(facility["facility_id"])] = dict(facility)
                typed = source_manifest.get("contract") == QUANTITY_CONTRACT if isinstance(source_manifest, Mapping) else False
                diagnostic = (
                    (day, str(selected["capture_id"]), str(selected["source_digest"]))
                    in KNOWN_BAD_CAPTURES
                )
                scopes: dict[str, dict[str, Any]] = {}
                for component in conn.execute(
                    "SELECT scope_key,component_kind,provenance_json "
                    "FROM sheet_vitrina_v1_inventory_history_components "
                    "WHERE capture_id=? ORDER BY scope_kind,scope_key,component_kind,component_id",
                    (selected["capture_id"],),
                ):
                    scope = scopes.setdefault(str(component["scope_key"]), {
                        "typed_quantity": typed, "diagnostic": "known_bad" if diagnostic else "",
                        "wb": {"provenance": {"identity": {}}},
                    })
                    if component["component_kind"] == "WB":
                        try:
                            provenance = json.loads(component["provenance_json"] or "{}")
                        except json.JSONDecodeError as exc:
                            raise WindowV3Error("window_history_invalid", 409, "Исторический источник повреждён.") from exc
                        scope["wb"]["provenance"]["identity"] = (
                            provenance.get("identity") or {}
                            if isinstance(provenance, Mapping) else {}
                        )
                history_dates[day] = {"scopes": scopes}
    # Accepted preliminary rows require the shared exact ready/book/receipt
    # proof. A separate short RO transaction is held for each bound date.
    for key, wanted_dates in dates_by_key.items():
        check_step()
        with window_read_context(db_path, runtime_dir=runtime_dir) as context:
            conn = context.borrow(db_path)
            plan_row = conn.execute(
                "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots "
                "WHERE bundle_version=? AND as_of_date=?", key,
            ).fetchone()
            if plan_row is None:
                raise WindowV3Error("window_version_stale", 409, "Ready-снимок исчез при выборе истории.")
            metadata = json.loads(plan_row[0]).get("metadata") or {}
            bound_dates = [
                day for day in wanted_dates
                if day not in history_dates
                and isinstance((metadata.get("fbs_accounting_bindings") or {}).get(day), Mapping)
            ]
            if not bound_dates:
                continue
        for day in bound_dates:
            check_step()
            with window_read_context(db_path, runtime_dir=runtime_dir) as context:
                verify_context(context)
                plan = _deserialize_sheet_vitrina_plan(plan_row[0])
                # The legacy proof reloads/verifies the complete canonical
                # ready plan but materializes only this one date's evidence.
                limited_plan = replace(plan, date_columns=[day])
                proven = read_management_inventory_history(
                    db_path, runtime_dir=runtime_dir, plan=limited_plan,
                    current_date="",
                )
                dated = (proven.get("dates") or {}).get(day)
                if not dated or not dated.get("accepted_publication"):
                    continue
                history_dates[day] = {"scopes": {
                    str(scope_key): {
                        "typed_quantity": bool(scope.get("typed_quantity")),
                        "diagnostic": str(scope.get("diagnostic") or ""),
                        "wb": {"provenance": {"identity": (
                            ((scope.get("wb") or {}).get("provenance") or {}).get("identity") or {}
                        )}},
                    }
                    for scope_key, scope in (dated.get("scopes") or {}).items()
                }}
                for facility in proven.get("facilities") or []:
                    if isinstance(facility, Mapping) and facility.get("facility_id"):
                        facilities[str(facility["facility_id"])] = dict(facility)
    return {
        "dates": history_dates,
        "facilities": sorted(
            facilities.values(),
            key=lambda item: (int(item.get("display_order") or 0), str(item.get("code") or ""),
                              str(item.get("facility_id") or "")),
        ),
    }


def _catalog_planning_rows(
    rows: list[Any], *, block: SheetVitrinaV1WebVitrinaBlock,
    selected_dates: list[str], business_date: str,
    legacy_wb_history_present: bool,
    history_catalog: Mapping[str, Any] | None = None,
) -> list[Any]:
    """Run the shared planning row-order/identity rule on one metadata date.

    The input history catalog contains only union scope identities and
    facilities.  It deliberately has no per-date quantities or cell values.
    """
    if not selected_dates:
        return rows
    from packages.application.inventory_planning_read_model import InventoryPlanningReadModel
    planning = InventoryPlanningReadModel(db_path=block.runtime.db_path).current(
        business_date=business_date,
    )
    planning_date = str((planning.get("wb") or {}).get("snapshot_date") or "")
    catalog_date = planning_date if planning_date in selected_dates else selected_dates[0]
    prepared = rows
    if legacy_wb_history_present:
        # The legacy presence gate checks only whether *any* combined WB cell
        # is nonempty.  The header scan proved that fact; a single sentinel
        # lets the original row-identity function take exactly that branch.
        for index, row in enumerate(rows):
            if row.metric_key in {COMBINED_TOTAL_ALIAS_KEY, f"total_{COMBINED_TOTAL_ALIAS_KEY}"}:
                prepared = list(rows)
                prepared[index] = replace(row, values_by_date={catalog_date: 1})
                break
        else:
            raise WindowV3Error("window_catalog_inconsistent", 409, "Legacy WB row пропал из каталога.")
    current = block.runtime.load_current_state()
    completed = extend_rows_with_inventory_planning(
        prepared, planning=planning, history=dict(history_catalog or {}),
        date_columns=[catalog_date],
        enabled_config=[item for item in current.config_v2 if item.enabled],
    )
    return [replace(row, values_by_date={}, presentation_by_date={}) for row in completed]


def _catalog_finalize_rows(
    rows: list[Any], *, block: SheetVitrinaV1WebVitrinaBlock,
) -> list[Any]:
    """Apply the two final legacy presentation row-order rules unchanged."""
    metrics = {
        item.metric_key: item
        for item in _effective_web_vitrina_metrics(
            block.runtime.load_current_state().metrics_v2
        )
    }
    rows = _apply_funnel_operator_presentation(rows, date_columns=[])
    return _include_authenticated_discount_total_row(
        rows, date_columns=[], metric=metrics[AVG_EFFECTIVE_DISCOUNT_METRIC_KEY],
    )


def _catalog_surface(
    rows: list[Any], *, dates: list[str], metric_catalog: list[Any],
    business_date: str,
) -> dict[str, Any]:
    """Compose legacy static cells and full column metadata without date cells."""
    static_schema = asdict(_build_schema(SimpleNamespace(date_columns=[], temporal_slots=[])))
    payload = {
        "contract_name": "web_vitrina_contract", "contract_version": "v1",
        "meta": {
            "snapshot_id": "", "as_of_date": dates[-1],
            "business_timezone": "Asia/Yekaterinburg",
            "generated_at": business_date + "T00:00:00Z",
        },
        "schema": static_schema,
        "rows": [asdict(row) for row in rows],
    }
    view = build_web_vitrina_view_model(payload)
    view_payload = asdict(view)
    renderers = _adapter_build_renderers(view_payload)
    static_columns = _adapter_build_columns(
        view_payload, renderer_ids={item.renderer_id for item in renderers},
    )
    static_rows = _adapter_build_rows(view_payload, columns=static_columns)
    filters = _adapter_build_filters(view_payload)
    sorts = _adapter_build_sorts(view_payload)

    full_schema = asdict(_build_schema(SimpleNamespace(date_columns=dates, temporal_slots=[])))
    date_view_columns = [
        asdict(item) for item in _view_build_columns({"schema": full_schema})
        if item.id.startswith("date:")
    ]
    date_view_sorts = [
        asdict(item) for item in _view_build_sorts({"schema": full_schema})
        if item.field.startswith("date:")
    ]
    date_columns = _adapter_build_columns(
        {"columns": date_view_columns, "rows": [], "filters": [], "sorts": date_view_sorts},
        renderer_ids={"renderer:text:text_default"},
    )
    date_columns = [
        replace(item, meta=replace(
            item.meta, default_cell_renderer_id="renderer:number:number_default"
        )) for item in date_columns
    ]
    date_sorts = _adapter_build_sorts({"sorts": date_view_sorts})
    # Every dynamically populated date cell names one of these renderers.
    # The per-cell renderer controls missing/quality states after transfer.
    extra_renderers = [
        WebVitrinaGravityTableRenderer(
            renderer_id=f"renderer:{kind}:{formatter}",
            gravity_variant="placeholder" if kind in {"empty", "unknown"} else "text",
            formatter_id=formatter, align="end",
            placeholder_text="—" if kind in {"empty", "unknown"} else None,
        )
        for kind, formatter in (
            ("empty", "empty_default"), ("unknown", "unknown_default"),
            ("number", "number_default"), ("money", "money_rub"),
            ("money", "money_rub_per_unit"), ("percent", "percent_default"),
        )
    ]
    renderers_by_id = {item.renderer_id: asdict(item) for item in [*renderers, *extra_renderers]}
    columns = [asdict(item) for item in [*static_columns, *date_columns]]
    adapter_sorts = [asdict(item) for item in [*sorts, *date_sorts]]
    catalog_rows = []
    for index, row in enumerate(static_rows):
        packed_cells: dict[str, list[Any]] = {}
        for column_id, cell in row.values.items():
            values = [getattr(cell, field) for field in CELL_FIELDS]
            while len(values) > 2 and values[-1] == CELL_DEFAULTS[len(values) - 1]:
                values.pop()
            packed_cells[column_id] = values
        catalog_rows.append({
            "row_id": row.row_id, "row_kind": row.row_kind,
            "section_id": row.section_id, "group_id": row.group_id,
            "depth": row.depth, "parent_id": row.parent_id,
            "search_text": row.search_text, "filter_tokens": row.filter_tokens,
            "row_order": index + 1, "static_cells": packed_cells,
        })
    section_items = [asdict(item) for item in view.sections]
    group_items = [asdict(item) for item in view.groups]
    metrics = _merge_metric_catalog(
        _count_metric_adapter_rows(static_rows),
        metric_catalog=[asdict(item) for item in metric_catalog],
    )
    metric_options = _build_metric_options(metrics, sections=section_items)
    section_counts = _count_rows_from_adapter(static_rows, key="section_id")
    group_counts = _count_rows_from_adapter(static_rows, key="group_id")
    column_labels = {item["id"]: item["header"] for item in columns}
    filter_surface = {
        "state_namespace": WEB_VITRINA_PAGE_STATE_NAMESPACE,
        "browser_state_persistence": "none",
        "controls": [
            {"control_id": "search", "kind": "search", "label": "Поиск", "default_value": "",
             "placeholder": "SKU, metric, group, nmId", "options": []},
            {"control_id": "section", "kind": "select", "label": "Секция", "default_value": "__all__",
             "options": _build_labeled_options(all_label="Все секции", items=section_items,
                                                counts=section_counts, id_key="section_id")},
            {"control_id": "group", "kind": "select", "label": "Группа", "default_value": "__all__",
             "options": _build_labeled_options(all_label="Все группы", items=group_items,
                                                counts=group_counts, id_key="group_id")},
            {"control_id": "metric", "kind": "select", "label": "Метрика", "default_value": "__all__",
             "options": metric_options,
             "option_groups": _build_metric_option_groups(metric_options, sections=section_items)},
        ],
        "sort_options": _build_sort_options({"sorts": adapter_sorts}, column_labels=column_labels),
        "default_sort_value": _resolve_default_sort_value({"sorts": adapter_sorts}),
        "empty_result_message": "Фильтры не вернули ни одной строки.",
    }
    table_surface = {
        "columns": columns, "rows": [],
        "renderers": list(renderers_by_id.values()),
        "formatters": [asdict(item) for item in _FORMATTER_LIBRARY.values()],
        "sorts": adapter_sorts, "filters": [asdict(item) for item in filters],
        "state_surface": {"current_state": "ready", "empty_message": "", "loading_message": "", "error_message": ""},
        "total_row_count": len(catalog_rows), "returned_row_count": 0,
        "table_data_state": "windowed",
    }
    return {
        "columns": columns,
        "rows": catalog_rows,
        "static_value_encoding": {
            "format": "indexed_cells_v2", "fields": list(CELL_FIELDS),
            "defaults": list(CELL_DEFAULTS),
        },
        "metric_catalog": [asdict(item) for item in metric_catalog],
        "filter_surface": filter_surface,
        "table_surface": table_surface,
    }


@dataclass(frozen=True)
class WindowV3Prepared:
    """A heavy response encoded once in the worker, ready for HTTP negotiation."""

    kind: str
    session_id: str
    json_bytes: bytes
    gzip_bytes: bytes
    build_ms: float
    encode_ms: float
    gzip_ms: float

    @property
    def retained_bytes(self) -> int:
        return len(self.json_bytes) + len(self.gzip_bytes)


@dataclass
class _WindowSession:
    session_id: str
    owner_key: tuple[str, str, str, tuple[str, ...]]
    content_token: str
    dates: list[str]
    bindings: list[_DateBinding]
    business_date: str
    default_as_of_date: str
    captured_now: datetime
    row_ids: list[str]
    static_search_texts: list[str]
    row_orders: list[int]
    sort_options: dict[str, str]
    static_sort_values: dict[str, list[tuple[Any, str]]]
    metadata_retained_bytes: int
    manifest: WindowV3Prepared | None
    created_at: float
    touched_at: float
    globals: dict[str, dict[str, Any]]
    cancelled: bool = False


@dataclass
class _WindowJob:
    job_id: str
    owner_key: tuple[str, str, str, tuple[str, ...]]
    operation: str
    session_id: str
    cancel_event: Event
    future: Future | None
    created_at: float
    completed_at: float = 0.0
    cached_result: tuple[int, dict[str, Any] | WindowV3Prepared] | None = None


class WindowV3Service:
    """One bounded heavy worker behind the existing single-thread HTTP loop."""

    def __init__(self, block: SheetVitrinaV1WebVitrinaBlock) -> None:
        self.block = block
        self._header_cache = ReadyHeaderCache()
        self._secret = secrets.token_bytes(32)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="web-vitrina-window")
        self._lock = RLock()
        self._sessions: dict[str, _WindowSession] = {}
        self._jobs: dict[str, _WindowJob] = {}
        self._pending = 0
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for job in self._jobs.values():
                job.cancel_event.set()
            for session in self._sessions.values():
                session.cancelled = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def request(
        self, operation: str, params: Mapping[str, str], *, owner: Mapping[str, Any],
    ) -> tuple[int, dict[str, Any] | WindowV3Prepared]:
        owner_key = self._owner_key(owner)
        arguments = {str(key): str(value) for key, value in params.items()}
        ack_ids = self._parse_ack_job_ids(arguments.pop("ack_job_ids", ""))
        self._check_arguments(operation, arguments)
        with self._lock:
            if self._closed:
                raise WindowV3Error("window_unavailable", 503, "Окно витрины остановлено.")
            self._cleanup_locked()
            self._ack_jobs_locked(ack_ids, owner_key)
        if operation == "manifest":
            dates = _dates_inclusive(arguments["date_from"], arguments["date_to"])
            return self._submit(
                owner_key, operation, "",
                lambda cancelled: self._build_manifest(dates, owner_key, cancelled),
            )
        if operation == "job":
            return self._poll(arguments["job_id"], owner_key)
        if operation == "cancel":
            return self._cancel(arguments, owner_key)
        session = self._session(arguments["session_id"], arguments["content_token"], owner_key)
        if operation == "seek":
            return 200, self._seek(session, arguments, owner_key)
        if operation == "chunk":
            cursor = self._decode_cursor(arguments["cursor"], session, owner_key)
            return self._submit(
                owner_key, operation, session.session_id,
                lambda cancelled: self._build_chunk(session, cursor, cancelled),
            )
        if operation == "global":
            return self._submit(
                owner_key, operation, session.session_id,
                lambda cancelled: self._build_global(session, arguments, cancelled),
            )
        raise WindowV3Error("window_invalid_operation", 422, "Неизвестная операция окна.")

    @staticmethod
    def _owner_key(owner: Mapping[str, Any]) -> tuple[str, str, str, tuple[str, ...]]:
        username = str(owner.get("username") or "").strip().casefold()
        if not username:
            raise WindowV3Error("window_owner_missing", 403, "Доступ к окну не подтверждён.")
        return (
            str(owner.get("user_id") or ""), username,
            str(owner.get("role") or "").strip(),
            tuple(sorted(str(value) for value in (owner.get("allowed_sections") or []))),
        )

    @staticmethod
    def _check_arguments(operation: str, arguments: Mapping[str, str]) -> None:
        shapes = {
            "manifest": ({"date_from", "date_to"}, {"history_mode"}),
            "job": ({"job_id"}, set()),
            "cancel": (set(), {"job_id", "session_id"}),
            "seek": ({"session_id", "content_token", "date_index", "row_start", "row_count"},
                     {"global_handle", "selected_row_indexes"}),
            "chunk": ({"session_id", "content_token", "cursor"}, set()),
            "global": ({"session_id", "content_token"}, {"search", "sort"}),
        }
        if operation not in shapes:
            raise WindowV3Error("window_invalid_operation", 422, "Неизвестная операция окна.")
        required, optional = shapes[operation]
        if set(arguments) - required - optional or required - set(arguments):
            raise WindowV3Error("window_invalid_parameters", 422, "Некорректные параметры окна.")
        if operation == "manifest" and arguments.get("history_mode", "explicit") != "explicit":
            raise WindowV3Error("window_invalid_parameters", 422, "Поддерживается только явный период.")
        if operation == "cancel" and (bool(arguments.get("job_id")) == bool(arguments.get("session_id"))):
            raise WindowV3Error("window_invalid_parameters", 422, "Нужен один job_id или session_id.")

    @staticmethod
    def _parse_ack_job_ids(raw: str) -> tuple[str, ...]:
        if not raw:
            return ()
        if len(raw.encode("utf-8")) > 512:
            raise WindowV3Error("window_invalid_parameters", 422, "Слишком много подтверждений окна.")
        ids = tuple(raw.split(","))
        if len(ids) > 8 or any(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item) is None for item in ids):
            raise WindowV3Error("window_invalid_parameters", 422, "Некорректные подтверждения окна.")
        return ids

    def _ack_jobs_locked(
        self, ids: tuple[str, ...], owner_key: tuple[str, str, str, tuple[str, ...]],
    ) -> None:
        for job_id in ids:
            job = self._jobs.get(job_id)
            # Unknown, foreign and unfinished jobs are indistinguishable here.
            if job is not None and job.owner_key == owner_key and job.cached_result is not None:
                self._jobs.pop(job_id, None)

    def _submit(
        self, owner_key: tuple[str, str, str, tuple[str, ...]], operation: str,
        session_id: str, work: Callable[[Event], WindowV3Prepared],
    ) -> tuple[int, dict[str, Any]]:
        with self._lock:
            if self._pending >= MAX_PENDING_JOBS:
                payload = WindowV3Error("window_busy", 429, "Очередь окна заполнена.").payload()
                payload["retry_after_ms"] = 500
                return 429, payload
            job_id = secrets.token_urlsafe(18)
            cancelled = Event()
            self._pending += 1
            future = self._executor.submit(work, cancelled)
            job = _WindowJob(job_id, owner_key, operation, session_id,
                             cancelled, future, time.monotonic())
            self._jobs[job_id] = job
            future.add_done_callback(
                lambda completed: self._job_done(job_id, operation, completed)
            )
        return 202, self._pending_payload(job_id)

    def _job_done(self, job_id: str, operation: str, future: Future) -> None:
        try:
            prepared = future.result()
            response: tuple[int, dict[str, Any] | WindowV3Prepared] = (200, prepared)
        except WindowV3Error as exc:
            response = (exc.status, exc.payload())
        except Exception as exc:
            _log_unexpected_window_error(operation, exc)
            # Do not retain a Future exception traceback with its worker locals.
            response = (500, WindowV3Error(
                "window_internal_error", 500, "Не удалось построить окно витрины."
            ).payload())
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.completed_at = time.monotonic()
                if job.cancel_event.is_set():
                    if isinstance(response[1], WindowV3Prepared) and response[1].kind == "manifest":
                        self._retire_session_locked(response[1].session_id)
                    response = (409, WindowV3Error(
                        "window_version_stale", 409, "Задача окна отменена."
                    ).payload())
                elif isinstance(response[1], WindowV3Prepared):
                    result = response[1]
                    if result.session_id not in self._sessions:
                        response = (409, WindowV3Error(
                            "window_version_stale", 409, "Сессия окна истекла."
                        ).payload())
                    elif result.kind != "manifest":
                        held = sum(
                            item.cached_result[1].retained_bytes
                            for item in self._jobs.values()
                            if item.cached_result is not None
                            and isinstance(item.cached_result[1], WindowV3Prepared)
                            and item.cached_result[1].kind != "manifest"
                        )
                        if result.retained_bytes > MAX_JOB_RESULT_BYTES:
                            response = (413, WindowV3Error(
                                "window_result_too_large", 413, "Результат окна превышает лимит памяти."
                            ).payload())
                        elif held + result.retained_bytes > MAX_JOB_RESULT_BYTES:
                            response = (503, WindowV3Error(
                                "window_result_cache_full", 503, "Кэш результатов окна заполнен."
                            ).payload())
                job.cached_result = response
                job.future = None
            self._pending = max(0, self._pending - 1)

    @staticmethod
    def _pending_payload(job_id: str, *, elapsed_seconds: float = 0.0) -> dict[str, Any]:
        retry_ms = 50 if elapsed_seconds < 0.2 else 150 if elapsed_seconds < 0.75 else 250
        return {
            "response_schema_version": SCHEMA_VERSION,
            "window_format": WINDOW_FORMAT, "state": "pending",
            "job_id": job_id, "retry_after_ms": retry_ms,
        }

    def _poll(
        self, job_id: str, owner_key: tuple[str, str, str, tuple[str, ...]],
    ) -> tuple[int, dict[str, Any] | WindowV3Prepared]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.owner_key != owner_key:
                raise WindowV3Error("window_job_missing", 404, "Задача окна недоступна.")
            if job.cancel_event.is_set():
                raise WindowV3Error("window_version_stale", 409, "Задача окна отменена.")
            if job.future is not None:
                return 202, self._pending_payload(
                    job_id, elapsed_seconds=time.monotonic() - job.created_at,
                )
            if job.cached_result is not None:
                if isinstance(job.cached_result[1], WindowV3Prepared):
                    result_session = self._sessions.get(job.cached_result[1].session_id)
                    if result_session is None or result_session.cancelled or result_session.owner_key != owner_key:
                        raise WindowV3Error("window_version_stale", 409, "Сессия окна истекла.")
                return job.cached_result
            raise WindowV3Error("window_internal_error", 500, "Результат окна недоступен.")

    def _cancel(
        self, arguments: Mapping[str, str],
        owner_key: tuple[str, str, str, tuple[str, ...]],
    ) -> tuple[int, dict[str, Any]]:
        with self._lock:
            if "job_id" in arguments:
                job = self._jobs.get(arguments["job_id"])
                if job is None or job.owner_key != owner_key:
                    raise WindowV3Error("window_job_missing", 404, "Задача окна недоступна.")
                job.cancel_event.set()
                if job.future is not None:
                    job.future.cancel()
                if job.cached_result is not None and isinstance(job.cached_result[1], WindowV3Prepared):
                    prepared = job.cached_result[1]
                    if prepared.kind == "manifest":
                        self._retire_session_locked(prepared.session_id)
                    else:
                        job.cached_result = (409, WindowV3Error(
                            "window_version_stale", 409, "Задача окна отменена."
                        ).payload())
            else:
                session = self._sessions.get(arguments["session_id"])
                if session is None or session.owner_key != owner_key:
                    raise WindowV3Error("window_session_missing", 404, "Сессия окна недоступна.")
                self._retire_session_locked(session.session_id)
        return 200, {
            "response_schema_version": SCHEMA_VERSION,
            "window_format": WINDOW_FORMAT, "kind": "cancel", "cancelled": True,
        }

    def _cleanup_locked(self) -> None:
        now = time.monotonic()
        for job_id, job in list(self._jobs.items()):
            if job.completed_at and now - job.completed_at > JOB_TTL_SECONDS:
                self._jobs.pop(job_id, None)
        for session_id, session in list(self._sessions.items()):
            if session.cancelled or now - session.touched_at > SESSION_TTL_SECONDS:
                self._retire_session_locked(session_id)
        for session in self._sessions.values():
            for handle, item in list(session.globals.items()):
                if now - float(item["created_at"]) > GLOBAL_TTL_SECONDS:
                    session.globals.pop(handle, None)

    def _retire_session_locked(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session.cancelled = True
        for job in self._jobs.values():
            prepared = job.cached_result[1] if job.cached_result is not None else None
            if job.session_id == session_id or (
                isinstance(prepared, WindowV3Prepared) and prepared.session_id == session_id
            ):
                job.cancel_event.set()
                if job.future is not None:
                    job.future.cancel()
                if job.cached_result is not None:
                    # Keep only a small stale tombstone until the job TTL expires.
                    job.cached_result = (409, WindowV3Error(
                        "window_version_stale", 409, "Сессия окна истекла."
                    ).payload())

    @staticmethod
    def _session_bytes(session: _WindowSession) -> int:
        return (
            (session.manifest.retained_bytes if session.manifest else 0)
            + session.metadata_retained_bytes
            + sum(72 * len(item["matched_set"]) + 256 for item in session.globals.values())
        )

    def _session(
        self, session_id: str, token: str,
        owner_key: tuple[str, str, str, tuple[str, ...]],
    ) -> _WindowSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.cancelled:
                raise WindowV3Error("window_version_stale", 409, "Сессия окна истекла.")
            if session.owner_key != owner_key:
                raise WindowV3Error("window_session_missing", 404, "Сессия окна недоступна.")
            if session.content_token != token:
                raise WindowV3Error("window_version_stale", 409, "Версия окна изменилась.")
            session.touched_at = time.monotonic()
            return session

    def _build_manifest(
        self, dates: list[str], owner_key: tuple[str, str, str, tuple[str, ...]],
        cancelled: Event,
    ) -> WindowV3Prepared:
        started = time.perf_counter()
        captured_now = self.block.now_factory()
        business_date = current_business_date_iso(captured_now)
        default_date = default_business_as_of_date(captured_now)
        db_path = self.block.runtime.db_path
        runtime_dir = self.block.runtime.runtime_dir
        with window_read_context(db_path, runtime_dir=runtime_dir) as context:
            material, bindings, default, headers = _input_material(
                context.borrow(db_path), runtime_dir, dates,
                business_date=business_date, default_as_of_date=default_date,
                context=context, header_cache=self._header_cache,
            )
        token = _digest(material)
        self._check_cancelled(cancelled)
        scan = _scan_row_headers(
            db_path, keys=_row_header_keys(bindings, default),
            expected_revisions={item.key: item.revision for item in headers},
            bindings=bindings, check_step=lambda: self._check_cancelled(cancelled),
        )
        self._check_cancelled(cancelled)
        def verify_history_context(context: Any) -> None:
            current, _, _, _ = _input_material(
                context.borrow(db_path), runtime_dir, dates,
                business_date=business_date, default_as_of_date=default_date,
                context=context, header_cache=self._header_cache,
            )
            if _digest(current) != token:
                raise WindowV3Error("window_version_stale", 409, "Входы истории изменились при выборе каталога.")

        history_catalog = _history_catalog(
            db_path, runtime_dir, bindings,
            check_step=lambda: self._check_cancelled(cancelled),
            verify_context=verify_history_context,
        )
        self._check_cancelled(cancelled)
        with window_read_context(db_path, runtime_dir=runtime_dir) as context:
            current_material, _, _, _ = _input_material(
                context.borrow(db_path), runtime_dir, dates,
                business_date=business_date, default_as_of_date=default_date,
                context=context, header_cache=self._header_cache,
            )
            if _digest(current_material) != token:
                raise WindowV3Error("window_version_stale", 409, "Входы изменились при чтении каталога.")
            frozen_block = self._frozen_block(captured_now)
            core = _catalog_core_rows(
                scan, block=frozen_block, business_date=business_date,
                selected_dates=dates,
            )
            planning = _catalog_planning_rows(
                core, block=frozen_block, selected_dates=dates,
                business_date=business_date,
                legacy_wb_history_present=scan.legacy_wb_history_present,
                history_catalog=history_catalog,
            )
            if business_date in dates:
                from packages.application.fbs_accounting_runtime import load as load_accounting
                accounting_book, _ = load_accounting(runtime_dir)
                if accounting_book is None or frozen_block._fbs_inventory_snapshot is not None:
                    planning = ensure_current_proxy_rows(
                        planning, business_date=business_date, date_present=True,
                    )
            rows = _catalog_finalize_rows(planning, block=frozen_block)
            metrics = _effective_web_vitrina_metrics(
                self.block.runtime.load_current_state().metrics_v2
            )
        self._check_cancelled(cancelled)
        with window_read_context(db_path, runtime_dir=runtime_dir) as context:
            latest_material, _, _, _ = _input_material(
                context.borrow(db_path), runtime_dir, dates,
                business_date=business_date, default_as_of_date=default_date,
                context=context, header_cache=self._header_cache,
            )
        if _digest(latest_material) != token:
            raise WindowV3Error("window_version_stale", 409, "Входы изменились после чтения каталога.")
        if not rows:
            raise WindowV3Error("window_catalog_empty", 409, "Каталог строк отсутствует.")
        surface = _catalog_surface(
            rows, dates=dates, metric_catalog=metrics, business_date=business_date,
        )
        sort_options = {
            str(item["value"]): str(item["column_id"])
            for item in surface["filter_surface"]["sort_options"]
        }
        static_sort_columns = {
            column for column in sort_options.values()
            if not column.startswith("date:")
        }
        static_sort_values = {
            column: [
                (packed[0], str(packed[1])) if packed else (None, "")
                for item in surface["rows"]
                for packed in [item["static_cells"].get(column)]
            ]
            for column in static_sort_columns
        }
        session_id = secrets.token_urlsafe(18)
        initial_cursor = self._mint_cursor(
            session_id=session_id, content_token=token, owner_key=owner_key,
            date_index=0, row_start=0, row_count=min(200, len(rows)),
            selected_row_indexes=[], global_handle="",
        )
        payload = {
            "response_schema_version": SCHEMA_VERSION,
            "window_format": WINDOW_FORMAT, "kind": "manifest", "state": "ready",
            "session_id": session_id, "content_token": token,
            "period": {"date_from": dates[0], "date_to": dates[-1]},
            "business_date": business_date,
            "dates": [item.payload() for item in bindings],
            **surface,
            "total_row_count": len(rows), "initial_cursor": initial_cursor,
            "chunk_shape": {
                "max_dates": MAX_CHUNK_DATES, "max_rows": MAX_CHUNK_ROWS,
                "max_decoded_bytes": MAX_CHUNK_BYTES,
            },
        }
        prepared = self._prepare(payload, build_ms=(time.perf_counter() - started) * 1000)
        if prepared.retained_bytes > MAX_SESSION_BYTES:
            raise WindowV3Error("window_manifest_too_large", 413, "Каталог не помещается в лимит сессии.")
        session = _WindowSession(
            session_id, owner_key, token, dates, bindings, business_date,
            default_date, captured_now, [row["row_id"] for row in surface["rows"]],
            [row["search_text"] for row in surface["rows"]],
            [int(row["row_order"]) for row in surface["rows"]],
            sort_options, static_sort_values,
            (
                sum(72 + len(value.encode("utf-8")) for value in [
                    *[row["row_id"] for row in surface["rows"]],
                    *[row["search_text"] for row in surface["rows"]],
                ])
                + 40 * len(rows)
                + sum(
                    96 + len(_canonical_bytes([value, display]))
                    for values in static_sort_values.values()
                    for value, display in values
                )
                + len(_canonical_bytes(sort_options)) * 2
            ),
            prepared, time.monotonic(), time.monotonic(), {},
        )
        with self._lock:
            self._check_cancelled(cancelled)
            self._admit_session_locked(session)
        return prepared

    def _admit_session_locked(self, session: _WindowSession) -> None:
        if self._session_bytes(session) > MAX_SESSION_BYTES:
            raise WindowV3Error("window_manifest_too_large", 413, "Каталог не помещается в лимит сессии.")
        held = sum(self._session_bytes(item) for item in self._sessions.values())
        while self._sessions and (
            len(self._sessions) >= MAX_SESSIONS
            or held + self._session_bytes(session) > MAX_SESSION_BYTES
        ):
            oldest = min(self._sessions.values(), key=lambda item: item.touched_at)
            held -= self._session_bytes(oldest)
            self._retire_session_locked(oldest.session_id)
        self._sessions[session.session_id] = session

    def _frozen_block(self, captured_now: datetime) -> SheetVitrinaV1WebVitrinaBlock:
        return SheetVitrinaV1WebVitrinaBlock(
            runtime=self.block.runtime,
            now_factory=lambda: captured_now,
            proxy_v4_parameters_resolver=self.block.proxy_v4_parameters_resolver,
            fbs_inventory_snapshot=self.block._fbs_inventory_snapshot,
        )

    @staticmethod
    def _check_cancelled(cancelled: Event) -> None:
        if cancelled.is_set():
            raise WindowV3Error("window_version_stale", 409, "Задача окна отменена.")

    @staticmethod
    def _prepare(payload: dict[str, Any], *, build_ms: float) -> WindowV3Prepared:
        encoded_at = time.perf_counter()
        json_bytes = _canonical_bytes(payload)
        if payload.get("kind") == "chunk":
            marker = b'"decoded_bytes":0'
            if json_bytes.count(marker) != 1:
                raise WindowV3Error("window_encoding_error", 500, "Размер порции не определён.")
            decoded_size = len(json_bytes)
            while True:
                next_size = len(json_bytes) + len(str(decoded_size)) - 1
                if next_size == decoded_size:
                    break
                decoded_size = next_size
            json_bytes = json_bytes.replace(marker, b'"decoded_bytes":' + str(decoded_size).encode("ascii"), 1)
            if len(json_bytes) != decoded_size:
                raise WindowV3Error("window_encoding_error", 500, "Размер порции не согласован.")
        encode_ms = (time.perf_counter() - encoded_at) * 1000
        zipped_at = time.perf_counter()
        gzip_bytes = gzip.compress(json_bytes, compresslevel=4, mtime=0)
        gzip_ms = (time.perf_counter() - zipped_at) * 1000
        return WindowV3Prepared(
            kind=str(payload["kind"]), session_id=str(payload.get("session_id") or ""),
            json_bytes=json_bytes, gzip_bytes=gzip_bytes,
            build_ms=build_ms, encode_ms=encode_ms, gzip_ms=gzip_ms,
        )

    def _mint_cursor(
        self, *, session_id: str, content_token: str,
        owner_key: tuple[str, str, str, tuple[str, ...]], date_index: int,
        row_start: int, row_count: int, selected_row_indexes: list[int],
        global_handle: str,
    ) -> str:
        payload = {
            "s": session_id, "v": content_token, "o": _digest(owner_key),
            "d": date_index, "r": row_start, "n": row_count,
            "i": selected_row_indexes, "g": global_handle,
        }
        raw = _canonical_bytes(payload)
        body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        signature = hmac.new(self._secret, body.encode("ascii"), sha256).digest()
        return body + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")

    def _seek(
        self, session: _WindowSession, arguments: Mapping[str, str],
        owner_key: tuple[str, str, str, tuple[str, ...]],
    ) -> dict[str, Any]:
        try:
            date_index = int(arguments["date_index"])
            row_start = int(arguments["row_start"])
            row_count = int(arguments["row_count"])
            selected = (
                [int(item) for item in arguments["selected_row_indexes"].split(",")]
                if arguments.get("selected_row_indexes") else []
            )
        except ValueError as exc:
            raise WindowV3Error("window_invalid_cursor", 422, "Некорректная область окна.") from exc
        if not (0 <= date_index < len(session.dates)) or row_start < 0 or not (1 <= row_count <= MAX_CHUNK_ROWS):
            raise WindowV3Error("window_invalid_cursor", 422, "Область окна вне периода.")
        if selected:
            if len(selected) != row_count or len(selected) > MAX_SELECTED_ROWS or len(set(selected)) != len(selected):
                raise WindowV3Error("window_invalid_cursor", 422, "Некорректный список строк.")
            if any(index < 0 or index >= len(session.row_ids) for index in selected):
                raise WindowV3Error("window_invalid_cursor", 422, "Строка вне каталога.")
        elif row_start + row_count > len(session.row_ids):
            raise WindowV3Error("window_invalid_cursor", 422, "Строка вне каталога.")
        handle = arguments.get("global_handle", "")
        if handle:
            with self._lock:
                global_result = session.globals.get(handle)
                if global_result is None or time.monotonic() - global_result["created_at"] > GLOBAL_TTL_SECONDS:
                    raise WindowV3Error("window_version_stale", 409, "Глобальный поиск истёк.")
                matched = global_result["matched_set"]
            if not selected or any(index not in matched for index in selected):
                raise WindowV3Error("window_invalid_cursor", 422, "Строки не принадлежат глобальному результату.")
        elif selected:
            raise WindowV3Error("window_invalid_cursor", 422, "Выбранным строкам нужен глобальный результат.")
        cursor = self._mint_cursor(
            session_id=session.session_id, content_token=session.content_token,
            owner_key=owner_key, date_index=date_index, row_start=row_start,
            row_count=row_count, selected_row_indexes=selected, global_handle=handle,
        )
        return {
            "response_schema_version": SCHEMA_VERSION, "window_format": WINDOW_FORMAT,
            "kind": "seek", "session_id": session.session_id,
            "content_token": session.content_token, "cursor": cursor,
        }

    def _decode_cursor(
        self, cursor: str, session: _WindowSession,
        owner_key: tuple[str, str, str, tuple[str, ...]],
    ) -> dict[str, Any]:
        if len(cursor) > MAX_QUERY_BYTES:
            raise WindowV3Error("window_invalid_cursor", 422, "Курсор слишком длинный.")
        try:
            body, signed = cursor.split(".", 1)
            actual = base64.urlsafe_b64decode(signed + "=" * (-len(signed) % 4))
            expected = hmac.new(self._secret, body.encode("ascii"), sha256).digest()
            if not hmac.compare_digest(actual, expected):
                raise ValueError("signature")
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            payload = json.loads(raw)
        except (ValueError, UnicodeError, binascii.Error, json.JSONDecodeError) as exc:
            raise WindowV3Error("window_invalid_cursor", 422, "Курсор окна повреждён.") from exc
        if (payload.get("s") != session.session_id or payload.get("v") != session.content_token
                or payload.get("o") != _digest(owner_key)):
            raise WindowV3Error("window_version_stale", 409, "Курсор относится к другой версии окна.")
        seek_args = {
            "date_index": str(payload.get("d")), "row_start": str(payload.get("r")),
            "row_count": str(payload.get("n")),
        }
        if payload.get("i"):
            seek_args["selected_row_indexes"] = ",".join(str(item) for item in payload["i"])
        if payload.get("g"):
            seek_args["global_handle"] = str(payload["g"])
        self._seek(session, seek_args, owner_key)
        return payload

    def _check_input_version(self, session: _WindowSession) -> None:
        with window_read_context(self.block.runtime.db_path, runtime_dir=self.block.runtime.runtime_dir) as context:
            material, _, _, _ = _input_material(
                context.borrow(self.block.runtime.db_path), self.block.runtime.runtime_dir,
                session.dates, business_date=session.business_date,
                default_as_of_date=session.default_as_of_date, context=context,
                header_cache=self._header_cache,
            )
        if _digest(material) != session.content_token:
            raise WindowV3Error("window_version_stale", 409, "Входы витрины изменились.")

    def _slice_rows(
        self, session: _WindowSession, date_index: int, cancelled: Event,
        row_indexes: list[int],
    ) -> tuple[list[str], dict[str, Any]]:
        self._check_cancelled(cancelled)
        if session.cancelled:
            raise WindowV3Error("window_version_stale", 409, "Сессия окна отменена.")
        days = session.dates[date_index:date_index + MAX_CHUNK_DATES]
        with window_read_context(self.block.runtime.db_path, runtime_dir=self.block.runtime.runtime_dir) as context:
            material, _, _, _ = _input_material(
                context.borrow(self.block.runtime.db_path), self.block.runtime.runtime_dir,
                session.dates, business_date=session.business_date,
                default_as_of_date=session.default_as_of_date, context=context,
                header_cache=self._header_cache,
            )
            if _digest(material) != session.content_token:
                raise WindowV3Error("window_version_stale", 409, "Входы витрины изменились.")
            if any(item.source_key is not None for item in session.bindings[date_index:date_index + len(days)]):
                contract = self._frozen_block(session.captured_now).build(
                    page_route="/web-vitrina", read_route="/api/web-vitrina",
                    date_from=days[0], date_to=days[-1],
                    output_row_ids=frozenset(session.row_ids[index] for index in row_indexes),
                )
                by_id = {row.row_id: row for row in contract.rows}
            else:
                by_id = {}
        self._check_cancelled(cancelled)
        self._check_input_version(session)
        return days, by_id

    @staticmethod
    def _date_source(row: Any | None) -> Mapping[str, Any]:
        if row is None:
            return {}
        # _build_cell reads only these three fields for date columns. Repeating
        # dataclass serialization for every date duplicates the whole row map.
        return {
            "values_by_date": row.values_by_date,
            "presentation_by_date": row.presentation_by_date,
            "format": row.format,
        }

    @staticmethod
    def _date_cell(column: Any, row: Any | None) -> list[Any]:
        source = row if isinstance(row, Mapping) else WindowV3Service._date_source(row)
        cell = _build_cell(column, source)
        renderer_id = _renderer_id(
            cell_kind=cell.cell_kind, formatter_id=cell.formatter_id,
        )
        values = [renderer_id if field == "renderer_id" else getattr(cell, field)
                  for field in CELL_FIELDS]
        while len(values) > 2 and values[-1] == CELL_DEFAULTS[len(values) - 1]:
            values.pop()
        return values

    @staticmethod
    def _date_view_columns(days: list[str]) -> list[Any]:
        schema = asdict(_build_schema(SimpleNamespace(date_columns=days, temporal_slots=[])))
        return [item for item in _view_build_columns({"schema": schema}) if item.id.startswith("date:")]

    def _build_chunk(
        self, session: _WindowSession, cursor: dict[str, Any], cancelled: Event,
    ) -> WindowV3Prepared:
        started = time.perf_counter()
        date_index = int(cursor["d"])
        requested = list(cursor["i"]) if cursor["i"] else list(range(int(cursor["r"]), int(cursor["r"]) + int(cursor["n"])))
        scoped = requested[:MAX_SCOPED_BUILD_ROWS]
        days, by_id = self._slice_rows(session, date_index, cancelled, scoped)
        columns = self._date_view_columns(days)
        rows = []
        for index in scoped:
            self._check_cancelled(cancelled)
            source = by_id.get(session.row_ids[index])
            source_payload = self._date_source(source)
            entries = [[position, *self._date_cell(column, source_payload)]
                       for position, column in enumerate(columns)]
            rows.append({"row_index": index, "values": entries})
        payload = {
            "response_schema_version": SCHEMA_VERSION, "window_format": WINDOW_FORMAT,
            "kind": "chunk", "state": "ready", "session_id": session.session_id,
            "content_token": session.content_token, "dates": days,
            "date_index": date_index, "row_start": int(cursor["r"]),
            "requested_row_count": len(requested), "row_count": len(rows),
            "rows": rows, "has_more_rows": len(rows) < len(requested), "next_cursor": "",
            "decoded_bytes": 0,
            "value_encoding": {"format": "indexed_cells_v2", "fields": list(CELL_FIELDS),
                               "defaults": list(CELL_DEFAULTS)},
        }
        if len(rows) < len(requested):
            payload["next_cursor"] = self._mint_cursor(
                session_id=session.session_id, content_token=session.content_token,
                owner_key=session.owner_key, date_index=date_index,
                row_start=int(cursor["r"]) + len(rows), row_count=len(requested) - len(rows),
                selected_row_indexes=(requested[len(rows):] if cursor["i"] else []),
                global_handle=str(cursor["g"]),
            )
        prepared = self._prepare(payload, build_ms=(time.perf_counter() - started) * 1000)
        if len(prepared.json_bytes) > MAX_CHUNK_BYTES:
            # Only the exceptional over-cap path encodes candidate prefixes.
            # No truncated response is ever sent as a successful chunk.
            low, high, accepted = 1, len(rows) - 1, None
            while low <= high:
                middle = (low + high) // 2
                next_cursor = self._mint_cursor(
                    session_id=session.session_id, content_token=session.content_token,
                    owner_key=session.owner_key, date_index=date_index,
                    row_start=int(cursor["r"]) + middle,
                    row_count=len(requested) - middle,
                    selected_row_indexes=(requested[middle:] if cursor["i"] else []),
                    global_handle=str(cursor["g"]),
                )
                payload.update(rows=rows[:middle], row_count=middle,
                               has_more_rows=True, next_cursor=next_cursor)
                candidate = self._prepare(payload, build_ms=(time.perf_counter() - started) * 1000)
                if len(candidate.json_bytes) <= MAX_CHUNK_BYTES:
                    accepted = candidate
                    low = middle + 1
                else:
                    high = middle - 1
            if accepted is None:
                raise WindowV3Error("window_chunk_too_large", 413, "Одна строка порции превышает лимит 8 МиБ.")
            prepared = accepted
        return prepared

    def _build_global(
        self, session: _WindowSession, arguments: Mapping[str, str], cancelled: Event,
    ) -> WindowV3Prepared:
        started = time.perf_counter()
        search = arguments.get("search", "").strip().lower()
        sort_value = arguments.get("sort", "")
        if sort_value and sort_value not in session.sort_options:
            raise WindowV3Error("window_invalid_sort", 422, "Неизвестная сортировка окна.")
        sort_column = session.sort_options.get(sort_value, "")
        matched = [not search or search in text.lower() for text in session.static_search_texts]
        sort_inputs: list[dict[str, Any]] = []
        if sort_column and not sort_column.startswith("date:"):
            static_values = session.static_sort_values[sort_column]
            sort_inputs = [
                {"row_index": index, "value": value, "display_text": display,
                 "row_order": session.row_orders[index]}
                for index, (value, display) in enumerate(static_values)
            ]
        elif sort_column:
            sort_inputs = [
                {"row_index": index, "value": None, "display_text": "",
                 "row_order": session.row_orders[index]}
                for index in range(len(session.row_ids))
            ]
        sort_date = sort_column.split(":", 1)[1] if sort_column.startswith("date:") else ""
        if search or sort_date:
            scan_indexes = (
                range(0, len(session.dates), MAX_CHUNK_DATES)
                if search else
                [session.dates.index(sort_date) // MAX_CHUNK_DATES * MAX_CHUNK_DATES]
            )
            for date_index in scan_indexes:
                self._check_cancelled(cancelled)
                for start in range(0, len(session.row_ids), MAX_SCOPED_BUILD_ROWS):
                    band = list(range(start, min(start + MAX_SCOPED_BUILD_ROWS, len(session.row_ids))))
                    if not sort_date and all(matched[index] for index in band):
                        continue
                    days, by_id = self._slice_rows(session, date_index, cancelled, band)
                    columns = self._date_view_columns(days)
                    for index in band:
                        if matched[index] and not sort_date:
                            continue
                        source = by_id.get(session.row_ids[index])
                        source_payload = self._date_source(source)
                        for column in columns:
                            cell = _build_cell(column, source_payload)
                            if search and cell.display_text not in {"", "—"} and search in cell.display_text.lower():
                                matched[index] = True
                            if column.id == sort_column:
                                sort_inputs[index]["value"] = cell.value
                                sort_inputs[index]["display_text"] = cell.display_text
        else:
            self._check_input_version(session)
        self._check_cancelled(cancelled)
        indexes = [index for index, included in enumerate(matched) if included]
        if sort_inputs:
            sort_inputs = [sort_inputs[index] for index in indexes]
        handle = secrets.token_urlsafe(12)
        payload = {
            "response_schema_version": SCHEMA_VERSION, "window_format": WINDOW_FORMAT,
            "kind": "global", "state": "ready", "session_id": session.session_id,
            "content_token": session.content_token, "global_handle": handle,
            "matched_row_indexes": indexes, "matched_row_count": len(indexes),
            "search": arguments.get("search", "").strip(), "sort": sort_value,
            "sort_inputs": sort_inputs,
        }
        prepared = self._prepare(payload, build_ms=(time.perf_counter() - started) * 1000)
        if len(prepared.json_bytes) > MAX_GLOBAL_BYTES:
            raise WindowV3Error("window_global_too_large", 413, "Глобальный результат превышает лимит памяти.")
        with self._lock:
            if session.cancelled or session.session_id not in self._sessions:
                raise WindowV3Error("window_version_stale", 409, "Сессия окна отменена.")
            held_without_current = sum(
                self._session_bytes(item) for item in self._sessions.values()
                if item.session_id != session.session_id
            )
            candidate_bytes = (
                (session.manifest.retained_bytes if session.manifest else 0)
                + session.metadata_retained_bytes + 72 * len(indexes) + 256
            )
            if held_without_current + candidate_bytes > MAX_SESSION_BYTES:
                raise WindowV3Error("window_global_too_large", 413, "Метаданные поиска превышают лимит памяти.")
            session.globals.clear()
            session.globals[handle] = {
                "created_at": time.monotonic(), "matched_set": set(indexes),
            }
        return prepared
