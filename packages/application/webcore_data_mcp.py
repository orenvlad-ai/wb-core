"""Read-only business-data gateway for the WebCore Data MCP."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from packages.application.webcore_ops_diagnostics import (
    OPS_TOOL_NAMES,
    WebCoreOpsDiagnostics,
    WebCoreOpsDiagnosticsError,
)
from packages.application.supplier_shipment_status import apply_derived_supplier_status
from packages.business_time import current_business_date_iso

DB_FILENAME = "registry_upload_runtime.sqlite3"
DEFAULT_MAX_LIMIT = 50
MAX_LIMIT = 100
MAX_DATE_RANGE_DAYS = 62
AUDIT_SCHEMA_VERSION = "webcore_data_mcp_audit_v2"
SCOPE_ANALYTICS_READ = "webcore.analytics.read"
SCOPE_SUPPLY_READ = "webcore.supply.read"
SCOPE_FINANCE_READ = "webcore.finance.read"
SCOPE_OPS_READ = "webcore.ops.read"

LEGACY_BUSINESS_TOOL_NAMES = (
    "get_webcore_data_map",
    "resolve_webcore_data_request",
    "resolve_webcore_data_intent",
    "list_webcore_business_tables",
    "get_webcore_business_table_schema",
    "get_webcore_business_table_rows",
    "get_supplier_shipments_registry",
    "get_supplier_shipment_full_details",
    "get_wb_supplies_registry",
    "get_wb_supply_full_details",
    "list_supply_artifacts",
    "get_supply_artifact",
    "get_data_freshness_status",
    "search_business_objects",
    "explain_metric_source",
    "get_wb_supplies_summary",
    "get_wb_supply_details",
    "rank_supplier_shipments_by_unit_cost",
    "get_supplier_shipment_details",
    "get_latest_factory_order_calculation",
    "list_metrics",
    "get_metric_values",
    "get_snapshot_metrics",
    "get_available_metric_dates",
    "get_stock_report",
    "get_sku_snapshot",
    "get_revenue_by_date",
    "get_revenue_range",
)

MODEL_TOOL_ALIASES = {
    "freshness": "get_data_freshness_status",
    "metric_catalog": "list_metrics",
    "metric_values": "get_metric_values",
    "sku_search": "search_business_objects",
    "sku_snapshot": "get_sku_snapshot",
    "supplier_shipments": "get_supplier_shipments_registry",
    "supplier_shipment": "get_supplier_shipment_full_details",
    "wb_supplies": "get_wb_supplies_registry",
    "wb_supply": "get_wb_supply_full_details",
    "supply_artifacts": "list_supply_artifacts",
    "supply_artifact": "get_supply_artifact",
    "factory_order": "get_latest_factory_order_calculation",
    "stock_report": "get_stock_report",
    "runtime_health": "get_runtime_health_summary",
    "refresh_diagnostics": "get_refresh_diagnostics",
    "deploy_state": "get_deploy_state",
}
MODEL_VISIBLE_TOOL_NAMES = tuple(MODEL_TOOL_ALIASES)
BUSINESS_TOOL_NAMES = tuple(
    dict.fromkeys((*MODEL_VISIBLE_TOOL_NAMES, *LEGACY_BUSINESS_TOOL_NAMES))
)
APPROVED_TOOL_NAMES = tuple(dict.fromkeys((*BUSINESS_TOOL_NAMES, *OPS_TOOL_NAMES)))

FORBIDDEN_OUTPUT_KEY_MARKERS = (
    "password",
    "secret",
    "token",
    "cookie",
    "authorization",
    "storage_state",
    "file_path",
    "stored_file_path",
    "source_file_path",
    "path",
    "sha256",
    "hash",
    "raw_parse_json",
    "raw_json",
    "raw_list_json",
    "raw_detail_json",
    "raw_goods_json",
    "raw_package_json",
    "workbook_blob",
)

SECRET_TEXT_MARKERS = (
    "storage_state",
    "authorization:",
    "bearer ",
    "password=",
    "token=",
    "cookie=",
    "client_secret",
    "oauth_signing_secret",
    "private key",
)

REVENUE_CANDIDATE_MARKERS = (
    "revenue",
    "выруч",
    "order",
    "orders",
    "buyout",
    "fin_buyout",
    "sales",
)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    scope: str = SCOPE_ANALYTICS_READ


class WebCoreDataMcpError(RuntimeError):
    def __init__(self, message: str, *, code: str = "webcore_data_mcp_error") -> None:
        super().__init__(message)
        self.code = code


class WebCoreDataMcpGateway:
    """Allowlisted read-only projections over the WebCore runtime DB."""

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        db_path: Path | None = None,
        audit_log_path: Path | None = None,
        max_limit: int = DEFAULT_MAX_LIMIT,
        ops_diagnostics: WebCoreOpsDiagnostics | None = None,
        ops_command_runner: Any | None = None,
        emit_audit_to_stdout: bool = False,
    ) -> None:
        resolved_runtime_dir: Path | None = runtime_dir
        if db_path is None:
            resolved_runtime_dir = runtime_dir or Path(
                os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", ".runtime/registry_upload")
            )
            db_path = resolved_runtime_dir / DB_FILENAME
        elif resolved_runtime_dir is None:
            resolved_runtime_dir = Path(db_path).expanduser().parent
        self.runtime_dir = Path(resolved_runtime_dir).expanduser() if resolved_runtime_dir else Path(db_path).expanduser().parent
        self.db_path = Path(db_path).expanduser()
        self.audit_log_path = Path(audit_log_path).expanduser() if audit_log_path else None
        self.max_limit = max(1, min(int(max_limit or DEFAULT_MAX_LIMIT), MAX_LIMIT))
        self.emit_audit_to_stdout = bool(emit_audit_to_stdout)
        self._audit_lock = threading.Lock()
        self._call_context = threading.local()
        self.ops_diagnostics = ops_diagnostics or WebCoreOpsDiagnostics(
            runtime_dir=self.runtime_dir,
            db_path=self.db_path,
            command_runner=ops_command_runner,
            mcp_audit_log_path=self.audit_log_path,
        )

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for definition in _tool_definitions():
            security_schemes = [{"type": "oauth2", "scopes": [definition.scope]}]
            tools.append(
                {
                    "name": definition.name,
                    "title": definition.title,
                    "description": definition.description,
                    "inputSchema": definition.input_schema,
                    "outputSchema": definition.output_schema or _object_schema(),
                    "annotations": {
                        "readOnlyHint": True,
                        "destructiveHint": False,
                        "idempotentHint": True,
                        "openWorldHint": False,
                    },
                    "securitySchemes": security_schemes,
                    "_meta": {"securitySchemes": security_schemes},
                }
            )
        return tools

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        identity: str = "",
        correlation_id: str = "",
        deadline_at: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        args = dict(arguments or {})
        started = time.monotonic()
        request_id = correlation_id or f"local-{int(time.time() * 1000)}-{threading.get_ident()}"
        identity_hash = _hash_text(identity) if identity else ""
        row_count = 0
        self._call_context.deadline_at = deadline_at
        self._call_context.cancel_event = cancel_event
        self.record_call_event(
            request_id=request_id,
            tool=name,
            identity_hash=identity_hash,
            event="start",
            status="started",
            duration_ms=0,
            result_bytes=0,
        )
        try:
            if name not in APPROVED_TOOL_NAMES:
                raise WebCoreDataMcpError(f"tool is not allowlisted: {name}", code="tool_not_allowlisted")
            result = self._call_tool(name, args)
            redacted = _redact(result)
            row_count = _estimate_row_count(redacted)
            result_bytes = len(json.dumps(redacted, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            late = bool(cancel_event and cancel_event.is_set())
            self.record_call_event(
                request_id=request_id,
                tool=name,
                identity_hash=identity_hash,
                event="finish",
                status="late_after_timeout" if late else "ok",
                duration_ms=int((time.monotonic() - started) * 1000),
                result_bytes=result_bytes,
                row_count=row_count,
            )
            return redacted
        except sqlite3.OperationalError as exc:
            deadline_reached = deadline_at is not None and time.monotonic() >= deadline_at
            cancelled = bool(cancel_event and cancel_event.is_set())
            if deadline_reached or cancelled or "interrupted" in str(exc).lower():
                error = WebCoreDataMcpError("SQLite query deadline exceeded", code="sqlite_timeout")
            else:
                error = exc
            self.record_call_event(
                request_id=request_id,
                tool=name,
                identity_hash=identity_hash,
                event="controlled_error",
                status="timeout" if isinstance(error, WebCoreDataMcpError) else "error",
                duration_ms=int((time.monotonic() - started) * 1000),
                result_bytes=0,
                error_code=error.code if isinstance(error, WebCoreDataMcpError) else error.__class__.__name__,
            )
            raise error
        except Exception as exc:
            self.record_call_event(
                request_id=request_id,
                tool=name,
                identity_hash=identity_hash,
                event="controlled_error",
                status="error",
                duration_ms=int((time.monotonic() - started) * 1000),
                result_bytes=0,
                error_code=exc.code if isinstance(exc, WebCoreDataMcpError) else exc.__class__.__name__,
            )
            raise
        finally:
            self._call_context.deadline_at = None
            self._call_context.cancel_event = None

    def record_call_event(
        self,
        *,
        request_id: str,
        tool: str,
        identity_hash: str,
        event: str,
        status: str,
        duration_ms: int,
        result_bytes: int,
        row_count: int = 0,
        error_code: str = "",
    ) -> None:
        self._audit(
            {
                "schema_version": AUDIT_SCHEMA_VERSION,
                "at": _utc_now(),
                "request_id": request_id,
                "correlation_id": request_id,
                "event": event,
                "tool": tool,
                "identity_hash": identity_hash,
                "status": status,
                "duration_ms": max(0, int(duration_ms)),
                "result_bytes": max(0, int(result_bytes)),
                "row_count": max(0, int(row_count)),
                "error_code": str(error_code or "")[:80],
            }
        )

    def verify_read_only_connection(self) -> dict[str, Any]:
        with self._connect() as conn:
            write_blocked = False
            error_text = ""
            try:
                conn.execute("CREATE TABLE webcore_data_mcp_write_probe(value TEXT)")
            except sqlite3.DatabaseError as exc:
                write_blocked = True
                error_text = _safe_error_text(str(exc))
            return {
                "status": "ok" if write_blocked else "failed",
                "read_only_mode": "sqlite_uri_mode_ro_query_only",
                "write_probe_blocked": write_blocked,
                "write_probe_error": error_text,
            }

    def _call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        requested_name = name
        name = MODEL_TOOL_ALIASES.get(name, name)
        if name in OPS_TOOL_NAMES:
            try:
                return self.ops_diagnostics.call_tool(
                    name,
                    args,
                    deadline_at=getattr(self._call_context, "deadline_at", None),
                    cancel_event=getattr(self._call_context, "cancel_event", None),
                )
            except WebCoreOpsDiagnosticsError as exc:
                raise WebCoreDataMcpError(str(exc), code="invalid_ops_diagnostics_request") from exc
        if name == "get_webcore_data_map":
            return self.get_webcore_data_map(
                domain=_optional_str(args.get("domain"), max_length=40) or "all",
                include_examples=_optional_bool(args.get("include_examples"), default=False),
                include_limitations=_optional_bool(args.get("include_limitations"), default=False),
            )
        if name in {"resolve_webcore_data_request", "resolve_webcore_data_intent"}:
            return self.resolve_webcore_data_request(
                intent=_required_str(args, "intent", max_length=500),
                domain=_optional_str(args.get("domain"), max_length=40) or "auto",
                object_id=_optional_str(args.get("object_id"), max_length=160),
                shipment_id=_optional_str(args.get("shipment_id"), max_length=160),
                supply_id=_optional_str(args.get("supply_id"), max_length=160),
                invoice_no=_optional_str(args.get("invoice_no"), max_length=160),
                supplier_name=_optional_str(args.get("supplier_name"), max_length=160),
                sku_or_nm_id=_optional_str(args.get("sku_or_nm_id"), max_length=120),
                metric_key_or_label=_optional_str(args.get("metric_key_or_label"), max_length=180),
                date_value=_optional_date(args.get("date")),
                date_from=_optional_date(args.get("date_from")),
                date_to=_optional_date(args.get("date_to")),
                artifact_kind=_optional_str(args.get("artifact_kind"), max_length=80) or "auto",
                mode=_optional_str(args.get("mode"), max_length=40) or "metadata_only",
                limit=_bounded_limit(args.get("limit"), self.max_limit),
            )
        if name == "list_webcore_business_tables":
            return self.list_webcore_business_tables(
                domain=_optional_str(args.get("domain"), max_length=60),
                include_missing=_optional_bool(args.get("include_missing"), default=True),
            )
        if name == "get_webcore_business_table_schema":
            return self.get_webcore_business_table_schema(table=_required_str(args, "table", max_length=120))
        if name == "get_webcore_business_table_rows":
            return self.get_webcore_business_table_rows(
                table=_required_str(args, "table", max_length=120),
                filters=_optional_mapping(args.get("filters")),
                date_from=_optional_date(args.get("date_from")),
                date_to=_optional_date(args.get("date_to")),
                limit=_bounded_limit(args.get("limit"), self.max_limit),
                cursor=_optional_str(args.get("cursor"), max_length=40),
                offset=_optional_int(args.get("offset"), default=0, minimum=0, maximum=100000),
                order_by=_optional_str(args.get("order_by"), max_length=80),
                include_raw_business_payloads=_optional_bool(args.get("include_raw_business_payloads"), default=False),
            )
        if name == "get_supplier_shipments_registry":
            return self.get_supplier_shipments_registry(
                shipment_id=_optional_str(args.get("shipment_id"), max_length=120),
                invoice_no=_optional_str(args.get("invoice_no"), max_length=160),
                supplier_name=_optional_str(args.get("supplier_name"), max_length=160),
                order_status=_optional_str(args.get("order_status"), max_length=80),
                match_status=_optional_str(args.get("match_status"), max_length=80),
                document_status=_optional_str(args.get("document_status"), max_length=80),
                date_from=_optional_date(args.get("date_from")),
                date_to=_optional_date(args.get("date_to")),
                sort_by=_optional_str(args.get("sort_by"), max_length=80) or "date_desc",
                limit=_bounded_limit(args.get("limit"), self.max_limit),
                cursor=_optional_str(args.get("cursor"), max_length=40),
                offset=_optional_int(args.get("offset"), default=0, minimum=0, maximum=100000),
            )
        if name == "get_supplier_shipment_full_details":
            return self.get_supplier_shipment_full_details(
                shipment_id=_required_str(args, "shipment_id", max_length=120),
                include_raw_business_payloads=_optional_bool(args.get("include_raw_business_payloads"), default=False),
                line_limit=_bounded_limit(args.get("line_limit"), 25 if requested_name == "supplier_shipment" else MAX_LIMIT),
                document_limit=_bounded_limit(args.get("document_limit"), 25 if requested_name == "supplier_shipment" else MAX_LIMIT),
            )
        if name == "get_wb_supplies_registry":
            return self.get_wb_supplies_registry(
                status_filter=_optional_str(args.get("status_filter"), max_length=80),
                warehouse=_optional_str(args.get("warehouse"), max_length=160),
                supply_id=_optional_str(args.get("supply_id"), max_length=120),
                wb_supply_id=_optional_str(args.get("wb_supply_id"), max_length=120),
                preorder_id=_optional_str(args.get("preorder_id"), max_length=120),
                date_from=_optional_date(args.get("date_from")),
                date_to=_optional_date(args.get("date_to")),
                limit=_bounded_limit(args.get("limit"), self.max_limit),
                cursor=_optional_str(args.get("cursor"), max_length=40),
                offset=_optional_int(args.get("offset"), default=0, minimum=0, maximum=100000),
            )
        if name == "get_wb_supply_full_details":
            return self.get_wb_supply_full_details(
                supply_id=_required_str(args, "supply_id", max_length=120),
                include_raw_business_payloads=_optional_bool(
                    args.get("include_raw_business_payloads"),
                    default=requested_name != "wb_supply",
                ),
            )
        if name == "list_supply_artifacts":
            return self.list_supply_artifacts(
                shipment_id=_optional_str(args.get("shipment_id"), max_length=120),
                supplier_order_id=_optional_str(args.get("supplier_order_id"), max_length=120),
                artifact_kind=_optional_str(args.get("artifact_kind"), max_length=80),
                source_domain=_optional_str(args.get("source_domain"), max_length=80),
                limit=_bounded_limit(args.get("limit"), self.max_limit),
                cursor=_optional_str(args.get("cursor"), max_length=40),
                offset=_optional_int(args.get("offset"), default=0, minimum=0, maximum=100000),
            )
        if name == "get_supply_artifact":
            return self.get_supply_artifact(
                artifact_ref=_required_str(args, "artifact_ref", max_length=240),
                mode=_optional_str(args.get("mode"), max_length=40) or "metadata",
                chunk=_optional_int(args.get("chunk"), default=0, minimum=0, maximum=100000),
                offset=_optional_int(args.get("offset"), default=0, minimum=0, maximum=100000000),
                max_bytes=_optional_int(args.get("max_bytes"), default=16384, minimum=1, maximum=65536),
            )
        if name == "get_data_freshness_status":
            return self.get_data_freshness_status()
        if name == "search_business_objects":
            return self.search_business_objects(
                query=_required_str(args, "query", max_length=120),
                object_types=(
                    _optional_str_list(args.get("object_types")) or ["sku", "nomenclature"]
                    if requested_name == "sku_search"
                    else _optional_str_list(args.get("object_types"))
                ),
            )
        if name == "explain_metric_source":
            return self.explain_metric_source(metric_key=_required_str(args, "metric_key", max_length=160))
        if name == "get_wb_supplies_summary":
            return self.get_wb_supplies_summary(
                status_filter=_optional_str(args.get("status_filter"), max_length=40),
                date_from=_optional_date(args.get("date_from")),
                date_to=_optional_date(args.get("date_to")),
                limit=_bounded_limit(args.get("limit"), self.max_limit),
            )
        if name == "get_wb_supply_details":
            return self.get_wb_supply_details(supply_id=_required_str(args, "supply_id", max_length=120))
        if name == "rank_supplier_shipments_by_unit_cost":
            return self.rank_supplier_shipments_by_unit_cost(
                limit=_bounded_limit(args.get("limit"), self.max_limit),
                status_filter=_optional_str(args.get("status_filter"), max_length=80),
            )
        if name == "get_supplier_shipment_details":
            return self.get_supplier_shipment_details(shipment_id=_required_str(args, "shipment_id", max_length=120))
        if name == "get_latest_factory_order_calculation":
            return self.get_latest_factory_order_calculation()
        if name == "list_metrics":
            return self.list_metrics(
                query=_optional_str(args.get("query"), max_length=160),
                section=_optional_str(args.get("section"), max_length=120),
                scope=_optional_str(args.get("scope"), max_length=60),
                limit=_bounded_limit(args.get("limit"), self.max_limit),
            )
        if name == "get_metric_values":
            return self.get_metric_values(
                metric_key_or_label=_required_str(args, "metric_key_or_label", max_length=180),
                date_value=_optional_date(args.get("date")),
                date_from=_optional_date(args.get("date_from")),
                date_to=_optional_date(args.get("date_to")),
                sku_or_nm_id=_optional_str(args.get("sku_or_nm_id"), max_length=120),
                group_by=_optional_str(args.get("group_by"), max_length=40),
                limit=_bounded_limit(args.get("limit"), self.max_limit),
            )
        if name == "get_snapshot_metrics":
            return self.get_snapshot_metrics(
                date_value=_required_date(args, "date"),
                sku_or_nm_id=_optional_str(args.get("sku_or_nm_id"), max_length=120),
                metric_query=_optional_str(args.get("metric_query"), max_length=160),
                limit=_bounded_limit(args.get("limit"), self.max_limit),
            )
        if name == "get_available_metric_dates":
            return self.get_available_metric_dates(
                metric_key_or_label=_optional_str(args.get("metric_key_or_label"), max_length=180),
            )
        if name == "get_stock_report":
            return self.get_stock_report(
                date_value=_optional_date(args.get("date")),
                sku_or_nm_id=_optional_str(args.get("sku_or_nm_id"), max_length=120),
            )
        if name == "get_sku_snapshot":
            return self.get_sku_snapshot(
                sku_or_nm_id=_required_str(args, "sku_or_nm_id", max_length=120),
                date_value=_optional_date(args.get("date")),
            )
        if name == "get_revenue_by_date":
            return self.get_revenue_by_date(
                date_value=_required_date(args, "date"),
                sku_or_nm_id=_optional_str(args.get("sku_or_nm_id"), max_length=120),
                revenue_metric=_optional_str(args.get("revenue_metric"), max_length=160),
            )
        if name == "get_revenue_range":
            return self.get_revenue_range(
                date_from=_required_date(args, "date_from"),
                date_to=_required_date(args, "date_to"),
                group_by=_optional_str(args.get("group_by"), max_length=40) or "date",
                revenue_metric=_optional_str(args.get("revenue_metric"), max_length=160),
            )
        raise WebCoreDataMcpError(f"tool is not implemented: {name}", code="tool_not_implemented")

    def get_data_freshness_status(self) -> dict[str, Any]:
        with self._connect() as conn:
            db_meta = self._db_file_meta()
            result: dict[str, Any] = {
                "status": "ok",
                "source": "runtime_sqlite_read_only",
                "db": db_meta,
                "ready_snapshots": _single_count_min_max(
                    conn,
                    "sheet_vitrina_v1_ready_snapshots",
                    "as_of_date",
                    extra_max_column="refreshed_at",
                ),
                "temporal_slot_sources": self._temporal_slot_sources(conn),
                "temporal_sources": self._temporal_sources(conn),
                "wb_supplies": self._wb_supplies_freshness(conn),
                "supplier_shipments": self._supplier_freshness(conn),
                "factory_order": self._factory_order_freshness(conn),
                "notes": [
                    "All values are read from the runtime SQLite DB through mode=ro/query_only.",
                    "WB supplies are cached-only; this tool never triggers upstream sync/backfill.",
                ],
            }
            return result

    def search_business_objects(self, *, query: str, object_types: list[str] | None = None) -> dict[str, Any]:
        normalized_types = set(object_types or ["sku", "nomenclature", "shipment", "wb_supply", "metric"])
        allowed_types = {"sku", "nomenclature", "shipment", "wb_supply", "metric"}
        unknown_types = sorted(normalized_types - allowed_types)
        normalized_types &= allowed_types
        like = f"%{query}%"
        hits: list[dict[str, Any]] = []
        with self._connect() as conn:
            if "sku" in normalized_types and _table_exists(conn, "registry_upload_config_v2"):
                hits.extend(self._search_skus(conn, like, limit=10))
            if "nomenclature" in normalized_types and _table_exists(conn, "sheet_vitrina_v1_nomenclature_items"):
                hits.extend(self._search_nomenclature(conn, like, limit=10))
            if "shipment" in normalized_types and _table_exists(conn, "sheet_vitrina_v1_supplier_shipments"):
                hits.extend(self._search_shipments(conn, like, limit=10))
            if "wb_supply" in normalized_types and _table_exists(conn, "sheet_vitrina_v1_wb_supplies"):
                hits.extend(self._search_wb_supplies(conn, like, limit=10))
            if "metric" in normalized_types and _table_exists(conn, "registry_upload_metrics_v2"):
                hits.extend(self._search_metrics(conn, like, limit=10))
        return {
            "status": "ok",
            "query": query,
            "object_types": sorted(normalized_types),
            "unknown_object_types": unknown_types,
            "limit_applied": 50,
            "results": hits[:50],
        }

    def explain_metric_source(self, *, metric_key: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._metric_row(conn, metric_key)
            candidates: list[dict[str, Any]] = []
            if row is None:
                candidates = self._metric_candidates(conn, metric_key, limit=12)
                return {
                    "status": "metric_not_found",
                    "metric_key": metric_key,
                    "candidate_metrics": candidates,
                    "source": "registry_upload_metrics_v2",
                }
            inferred_source = _infer_source_key(row.get("calc_ref") or row.get("metric_key") or "")
            formula = self._formula_row(conn, str(row.get("calc_ref") or ""))
            freshness = self._source_freshness(conn, inferred_source) if inferred_source else {}
            return {
                "status": "ok",
                "metric": row,
                "inferred_source_key": inferred_source or "unknown",
                "formula": formula,
                "accepted_freshness": freshness,
                "caveats": _metric_caveats(row, inferred_source),
            }

    def get_wb_supplies_summary(
        self,
        *,
        status_filter: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int,
    ) -> dict[str, Any]:
        _validate_date_range(date_from, date_to)
        rows: list[dict[str, Any]] = []
        status_counts: dict[str, int] = {}
        with self._connect() as conn:
            if not _table_exists(conn, "sheet_vitrina_v1_wb_supplies"):
                return _missing_table_result("sheet_vitrina_v1_wb_supplies")
            where: list[str] = []
            params: list[Any] = []
            if status_filter:
                if status_filter.isdigit():
                    where.append("status_id = ?")
                    params.append(int(status_filter))
                else:
                    where.append("LOWER(normalized_row_json) LIKE ?")
                    params.append(f"%{status_filter.lower()}%")
            if date_from:
                where.append("COALESCE(substr(supply_date, 1, 10), substr(fact_date, 1, 10), substr(updated_date, 1, 10), '') >= ?")
                params.append(date_from)
            if date_to:
                where.append("COALESCE(substr(supply_date, 1, 10), substr(fact_date, 1, 10), substr(updated_date, 1, 10), '') <= ?")
                params.append(date_to)
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""
            for row in conn.execute(
                f"""
                SELECT supply_id, wb_supply_id, preorder_id, normalized_row_json, warehouse_id, status_id,
                       quantity_for_size_filter, source_created_at, supply_date, fact_date, updated_date,
                       synced_at, last_list_synced_at, last_enriched_at, enrichment_status
                FROM sheet_vitrina_v1_wb_supplies
                {where_sql}
                ORDER BY COALESCE(supply_date, fact_date, updated_date, synced_at, '') DESC, supply_id
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall():
                item = _row_dict(row)
                normalized = _json_object(item.pop("normalized_row_json", None))
                status_key = str(item.get("status_id") if item.get("status_id") is not None else "unknown")
                status_counts[status_key] = status_counts.get(status_key, 0) + 1
                item["normalized"] = _select_keys(
                    normalized,
                    (
                        "supply_id",
                        "id",
                        "status",
                        "status_name",
                        "warehouse_name",
                        "quantity",
                        "goods_count",
                        "route",
                        "amount",
                        "currency",
                    ),
                )
                rows.append(item)
        return {
            "status": "ok",
            "source_table": "sheet_vitrina_v1_wb_supplies",
            "cache_only": True,
            "filters": {"status_filter": status_filter, "date_from": date_from, "date_to": date_to},
            "limit": limit,
            "status_counts_in_result": status_counts,
            "rows": rows,
        }

    def get_wb_supply_details(self, *, supply_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            if not _table_exists(conn, "sheet_vitrina_v1_wb_supplies"):
                return _missing_table_result("sheet_vitrina_v1_wb_supplies")
            row = conn.execute(
                """
                SELECT supply_id, cache_key, wb_supply_id, preorder_id, normalized_row_json,
                       raw_detail_json, raw_goods_json, raw_package_json,
                       warehouse_id, status_id, quantity_for_size_filter,
                       source_created_at, supply_date, fact_date, updated_date, synced_at,
                       last_list_synced_at, last_enriched_at, enrichment_status, enrichment_error
                FROM sheet_vitrina_v1_wb_supplies
                WHERE supply_id = ? OR wb_supply_id = ? OR preorder_id = ?
                LIMIT 1
                """,
                (supply_id, supply_id, supply_id),
            ).fetchone()
            if row is None:
                return {"status": "not_found", "supply_id": supply_id, "cache_only": True}
            item = _row_dict(row)
            normalized = _json_object(item.pop("normalized_row_json", None))
            detail_summary = _compact_json_summary(_safe_json_loads(item.pop("raw_detail_json", None)))
            goods_summary = _compact_json_summary(_safe_json_loads(item.pop("raw_goods_json", None)))
            package_summary = _compact_json_summary(_safe_json_loads(item.pop("raw_package_json", None)))
            item["normalized"] = _redact(normalized)
            item["cached_detail_summary"] = detail_summary
            item["cached_goods_summary"] = goods_summary
            item["cached_package_summary"] = package_summary
            item["cache_only"] = True
            item["no_upstream_fetch"] = True
            return {"status": "ok", "source_table": "sheet_vitrina_v1_wb_supplies", "supply": item}

    def rank_supplier_shipments_by_unit_cost(
        self,
        *,
        limit: int,
        status_filter: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            if not _table_exists(conn, "sheet_vitrina_v1_supplier_shipments"):
                return _missing_table_result("sheet_vitrina_v1_supplier_shipments")
            supplier_columns = _table_columns(
                conn, "sheet_vitrina_v1_supplier_shipments"
            )
            historical_exception_select = (
                "s.historical_status_exception"
                if "historical_status_exception" in supplier_columns
                else "'' AS historical_status_exception"
            )
            where = ""
            params: list[Any] = []
            if status_filter:
                today = current_business_date_iso()
                where = "WHERE " + _derived_supplier_status_sql(
                    "s",
                    historical_exception_available=(
                        "historical_status_exception"
                        in _table_columns(conn, "sheet_vitrina_v1_supplier_shipments")
                    ),
                ) + " = ?"
                params.extend((today, today, status_filter))
            rows = conn.execute(
                f"""
                SELECT s.shipment_id, s.shipment_date, s.actual_shipment_date, s.actual_ff_acceptance_date,
                       {historical_exception_select}, s.order_status, s.currency, s.product_qty_total, s.product_amount_total,
                       s.extras_amount_total, s.invoice_amount_total, s.match_status,
                       COUNT(l.line_id) AS line_count,
                       COALESCE(SUM(CASE WHEN l.qty IS NULL THEN 0 ELSE l.qty END), 0) AS line_qty_total,
                       COALESCE(SUM(CASE WHEN l.amount IS NULL THEN 0 ELSE l.amount END), 0) AS line_amount_total,
                       COALESCE(e.expense_amount_rub, 0) AS expense_amount_rub,
                       COALESCE(e.expense_line_count, 0) AS expense_line_count
                FROM sheet_vitrina_v1_supplier_shipments s
                LEFT JOIN sheet_vitrina_v1_supplier_shipment_lines l
                  ON l.shipment_id = s.shipment_id
                LEFT JOIN (
                    SELECT supplier_order_id,
                           SUM(CASE WHEN amount_rub IS NULL THEN 0 ELSE amount_rub END) AS expense_amount_rub,
                           COUNT(*) AS expense_line_count
                    FROM sheet_vitrina_v1_supplier_financial_expense_lines
                    GROUP BY supplier_order_id
                ) e ON e.supplier_order_id = s.shipment_id
                {where}
                GROUP BY s.shipment_id
                ORDER BY s.shipment_date DESC, s.updated_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        ranked: list[dict[str, Any]] = []
        for row in rows:
            item = apply_derived_supplier_status(_row_dict(row))
            qty = _first_number(item.get("product_qty_total"), item.get("line_qty_total"))
            base_amount = _first_number(item.get("invoice_amount_total"), item.get("product_amount_total"), item.get("line_amount_total"))
            expense = _number_or_zero(item.get("expense_amount_rub"))
            total_cost_evidence = (base_amount or 0.0) + expense
            unit_cost = total_cost_evidence / qty if qty and qty > 0 else None
            ranked.append(
                {
                    "shipment_id": item.get("shipment_id"),
                    "shipment_date": item.get("shipment_date"),
                    "order_status": item.get("order_status"),
                    "quantity_evidence": qty,
                    "base_amount_evidence": base_amount,
                    "expense_amount_rub": expense,
                    "unit_cost_evidence": unit_cost,
                    "currency": item.get("currency"),
                    "completeness": {
                        "has_quantity": bool(qty),
                        "has_base_amount": base_amount is not None,
                        "has_expense_lines": bool(item.get("expense_line_count")),
                        "match_status": item.get("match_status"),
                    },
                    "risk_note": "Unit cost uses available invoice/product amount plus RUB expense evidence; it is not a full SKU allocation.",
                }
            )
        ranked.sort(key=lambda item: (item["unit_cost_evidence"] is None, -(item["unit_cost_evidence"] or 0)))
        return {
            "status": "ok",
            "source_tables": [
                "sheet_vitrina_v1_supplier_shipments",
                "sheet_vitrina_v1_supplier_shipment_lines",
                "sheet_vitrina_v1_supplier_financial_expense_lines",
            ],
            "limit": limit,
            "status_filter": status_filter,
            "rows": ranked[:limit],
        }

    def get_supplier_shipment_details(self, *, shipment_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = self._supplier_shipment_row(conn, shipment_id)
            if row is None:
                return {"status": "not_found", "shipment_id": shipment_id}
            line_summary = self._supplier_line_summary(conn, shipment_id)
            financial_docs = self._supplier_financial_doc_summary(conn, shipment_id)
            expenses = self._supplier_expense_summary(conn, shipment_id)
            trade_docs = self._supplier_trade_doc_summary(conn, shipment_id)
            packing_docs = _fetch_packing_list_docs_for_shipments(conn, [shipment_id]).get(shipment_id, [])
            return {
                "status": "ok",
                "source_tables": [
                    "sheet_vitrina_v1_supplier_shipments",
                    "sheet_vitrina_v1_supplier_shipment_lines",
                    "sheet_vitrina_v1_supplier_financial_documents",
                    "sheet_vitrina_v1_supplier_financial_expense_lines",
                    "sheet_vitrina_v1_trade_documents",
                ],
                "shipment": row,
                "line_summary": line_summary,
                "financial_documents": financial_docs,
                "expense_summary": expenses,
                "trade_documents": trade_docs,
                "packing_list_summary": _packing_list_summary_from_documents(packing_docs, line_item_limit=0),
                "redaction": "raw files, file paths, hashes and raw parse JSON are not exposed",
            }

    def get_latest_factory_order_calculation(self) -> dict[str, Any]:
        with self._connect() as conn:
            datasets = []
            if _table_exists(conn, "sheet_vitrina_v1_factory_order_dataset_state"):
                for row in conn.execute(
                    """
                    SELECT dataset_type, uploaded_at, row_count, uploaded_filename, uploaded_content_type
                    FROM sheet_vitrina_v1_factory_order_dataset_state
                    ORDER BY uploaded_at DESC, dataset_type
                    """
                ).fetchall():
                    item = _row_dict(row)
                    item.pop("uploaded_filename", None)
                    item.pop("uploaded_content_type", None)
                    datasets.append(item)
            factory = self._latest_result_summary(conn, "sheet_vitrina_v1_factory_order_result_state")
            regional = self._latest_result_summary(conn, "sheet_vitrina_v1_wb_regional_supply_result_state")
            return {
                "status": "ok",
                "dataset_state": datasets,
                "factory_order_result": factory,
                "wb_regional_supply_result": regional,
                "no_recalculation": True,
            }

    def list_metrics(
        self,
        *,
        query: str | None = None,
        section: str | None = None,
        scope: str | None = None,
        limit: int,
    ) -> dict[str, Any]:
        query_text = (query or "").strip()
        section_text = (section or "").strip()
        scope_text = (scope or "").strip()
        with self._connect() as conn:
            registry = self._metric_catalog(conn)
            latest_snapshot = self._ready_snapshot(conn, None)
            snapshot_catalog = (
                _snapshot_metric_catalog(_safe_json_loads(latest_snapshot.get("plan_json")))
                if latest_snapshot
                else {}
            )
            merged: dict[str, dict[str, Any]] = {}
            for metric_key, meta in registry.items():
                item = {
                    "metric_key": metric_key,
                    "label_ru": meta.get("label_ru") or metric_key,
                    "scope": meta.get("scope") or "",
                    "section_name": meta.get("section_name") or "",
                    "format_name": meta.get("format_name") or "",
                    "enabled": meta.get("enabled"),
                    "source": "registry_upload_metrics_v2",
                    "coverage_levels": [],
                    "latest_snapshot_date": latest_snapshot.get("as_of_date") if latest_snapshot else None,
                    "latest_value_present": False,
                }
                if metric_key in snapshot_catalog:
                    coverage = snapshot_catalog[metric_key]
                    item["coverage_levels"] = coverage.get("levels", [])
                    item["latest_value_present"] = bool(coverage.get("value_count"))
                    item["sample_projection_keys"] = coverage.get("sample_projection_keys", [])
                    item["sample_labels"] = coverage.get("sample_labels", [])
                merged[metric_key] = item
            for metric_key, coverage in snapshot_catalog.items():
                if metric_key in merged:
                    continue
                labels = coverage.get("sample_labels") or []
                merged[metric_key] = {
                    "metric_key": metric_key,
                    "label_ru": labels[0] if labels else metric_key,
                    "scope": ",".join(coverage.get("levels", [])),
                    "section_name": "",
                    "format_name": "",
                    "enabled": None,
                    "source": "sheet_vitrina_v1_ready_snapshots",
                    "coverage_levels": coverage.get("levels", []),
                    "latest_snapshot_date": latest_snapshot.get("as_of_date") if latest_snapshot else None,
                    "latest_value_present": bool(coverage.get("value_count")),
                    "sample_projection_keys": coverage.get("sample_projection_keys", []),
                    "sample_labels": labels[:5],
                }
            rows = [
                item
                for item in merged.values()
                if _matches_metric_filter(item, query=query_text, section=section_text, scope=scope_text)
            ]
            rows.sort(key=lambda item: (_enabled_sort_value(item.get("enabled")), str(item.get("section_name") or ""), str(item.get("metric_key") or "")))
            truncated = len(rows) > limit
            return {
                "status": "ok",
                "source_tables": ["registry_upload_metrics_v2", "sheet_vitrina_v1_ready_snapshots"],
                "query": query,
                "section": section,
                "scope": scope,
                "latest_ready_snapshot": _snapshot_meta(latest_snapshot),
                "limit": limit,
                "truncated": truncated,
                "rows": rows[:limit],
            }

    def get_metric_values(
        self,
        *,
        metric_key_or_label: str,
        date_value: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sku_or_nm_id: str | None = None,
        group_by: str | None = None,
        limit: int,
    ) -> dict[str, Any]:
        if date_value and (date_from or date_to):
            raise WebCoreDataMcpError("pass either date or date_from/date_to, not both", code="invalid_date_range")
        if date_value:
            effective_from = effective_to = date_value
        elif date_from or date_to:
            if not date_from or not date_to:
                raise WebCoreDataMcpError("date_from and date_to must be provided together", code="invalid_date_range")
            effective_from, effective_to = date_from, date_to
        else:
            with self._connect() as conn:
                effective_from = effective_to = self._latest_metric_default_date(conn)
        _validate_date_range(effective_from, effective_to, max_days=MAX_DATE_RANGE_DAYS)
        normalized_group_by = group_by or ""
        if normalized_group_by and normalized_group_by not in {"date", "metric", "sku", "total"}:
            raise WebCoreDataMcpError("group_by must be one of: date, metric, sku, total", code="invalid_group_by")
        with self._connect() as conn:
            metric_keys, metric_query, candidates = self._resolve_metric_selector(conn, metric_key_or_label)
            rows, source_snapshots, truncated = self._metric_rows_for_range(
                conn,
                date_from=effective_from,
                date_to=effective_to,
                metric_keys=metric_keys,
                metric_query=metric_query,
                sku_or_nm_id=sku_or_nm_id,
                limit=limit,
            )
        if rows:
            status = "ok"
        elif candidates:
            status = "projection_unavailable"
        else:
            status = "metric_not_found"
        result: dict[str, Any] = {
            "status": status,
            "metric_key_or_label": metric_key_or_label,
            "resolved_metric_keys": sorted({str(row.get("metric_key")) for row in rows if row.get("metric_key")})[:50],
            "candidate_metrics": candidates[:20],
            "date_from": effective_from,
            "date_to": effective_to,
            "sku_or_nm_id": sku_or_nm_id,
            "source_table": "sheet_vitrina_v1_ready_snapshots",
            "source_snapshots": source_snapshots[:20],
            "limit": limit,
            "truncated": truncated,
            "rows": rows,
            "row_count": len(rows),
            "caveat": "" if rows else "Metric dictionary/ready snapshot evidence exists, but no bounded projected values matched the requested date/SKU.",
        }
        if normalized_group_by:
            result["group_by"] = normalized_group_by
            result["buckets"] = _group_metric_rows(rows, normalized_group_by)
        return result

    def get_snapshot_metrics(
        self,
        *,
        date_value: str,
        sku_or_nm_id: str | None = None,
        metric_query: str | None = None,
        limit: int,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            rows, source_snapshots, truncated = self._metric_rows_for_range(
                conn,
                date_from=date_value,
                date_to=date_value,
                metric_keys=set(),
                metric_query=metric_query,
                sku_or_nm_id=sku_or_nm_id,
                limit=limit,
            )
        return {
            "status": "ok" if rows else "date_not_found_or_no_metric_values",
            "date": date_value,
            "sku_or_nm_id": sku_or_nm_id,
            "metric_query": metric_query,
            "source_table": "sheet_vitrina_v1_ready_snapshots",
            "source_snapshots": source_snapshots[:20],
            "limit": limit,
            "truncated": truncated,
            "rows": rows,
            "row_count": len(rows),
        }

    def get_available_metric_dates(self, *, metric_key_or_label: str | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            if not _table_exists(conn, "sheet_vitrina_v1_ready_snapshots"):
                return _missing_table_result("sheet_vitrina_v1_ready_snapshots")
            metric_keys: set[str] = set()
            metric_query: str | None = None
            candidates: list[dict[str, Any]] = []
            if metric_key_or_label:
                metric_keys, metric_query, candidates = self._resolve_metric_selector(conn, metric_key_or_label)
            rows = conn.execute(
                """
                SELECT as_of_date, snapshot_id, refreshed_at, plan_json
                FROM sheet_vitrina_v1_ready_snapshots
                ORDER BY as_of_date DESC, refreshed_at DESC
                """
            ).fetchall()
            metric_meta = self._metric_catalog(conn)
            sku_meta = self._sku_catalog(conn)
            dates: dict[str, dict[str, Any]] = {}
            for row in rows:
                snapshot = _row_dict(row)
                payload = _safe_json_loads(snapshot.get("plan_json"))
                if metric_key_or_label:
                    metric_rows = _extract_ready_snapshot_metric_rows(
                        payload,
                        metric_keys=metric_keys,
                        metric_query=metric_query,
                        sku_or_nm_id=None,
                        date_from=None,
                        date_to=None,
                        metric_meta=metric_meta,
                        sku_meta=sku_meta,
                        snapshot=snapshot,
                        limit=1000,
                    )
                    for metric_row in metric_rows:
                        date_key = str(metric_row.get("date") or "")
                        if date_key:
                            dates.setdefault(
                                date_key,
                                {
                                    "date": date_key,
                                    "source_snapshot_id": metric_row.get("source_snapshot_id"),
                                    "refreshed_at": metric_row.get("refreshed_at"),
                                    "source_as_of_date": metric_row.get("source_as_of_date"),
                                },
                            )
                else:
                    for date_key in _snapshot_date_columns(payload, fallback=snapshot.get("as_of_date")):
                        dates.setdefault(
                            date_key,
                            {
                                "date": date_key,
                                "source_snapshot_id": snapshot.get("snapshot_id"),
                                "refreshed_at": snapshot.get("refreshed_at"),
                                "source_as_of_date": snapshot.get("as_of_date"),
                            },
                        )
            sorted_dates = [dates[key] for key in sorted(dates.keys(), reverse=True)]
            truncated = len(sorted_dates) > 200
            return {
                "status": "ok" if sorted_dates else ("projection_unavailable" if candidates else "metric_not_found"),
                "metric_key_or_label": metric_key_or_label,
                "candidate_metrics": candidates[:20],
                "source_table": "sheet_vitrina_v1_ready_snapshots",
                "dates": sorted_dates[:200],
                "date_count": len(sorted_dates),
                "truncated": truncated,
            }

    def get_stock_report(self, *, date_value: str | None = None, sku_or_nm_id: str | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            snapshot = self._ready_snapshot(conn, date_value)
            stocks_freshness = self._source_freshness(conn, "stocks")
            if snapshot is None:
                return {
                    "status": "not_found",
                    "date": date_value,
                    "source": "sheet_vitrina_v1_ready_snapshots",
                    "stocks_freshness": stocks_freshness,
                }
            target_date = date_value or str(snapshot.get("as_of_date") or "")
            stock_metrics, _, _ = self._metric_rows_for_range(
                conn,
                date_from=target_date,
                date_to=target_date,
                metric_keys=set(),
                metric_query="stock",
                sku_or_nm_id=sku_or_nm_id,
                limit=50,
            )
            if not stock_metrics:
                payload = _safe_json_loads(snapshot.get("plan_json"))
                stock_metrics = _extract_named_metrics(payload, sku_or_nm_id, name_markers=("stock", "остат", "qty"))
            return {
                "status": "ok" if stock_metrics else "structured_stock_projection_unavailable",
                "date": snapshot.get("as_of_date"),
                "snapshot_id": snapshot.get("snapshot_id"),
                "refreshed_at": snapshot.get("refreshed_at"),
                "sku_or_nm_id": sku_or_nm_id,
                "stocks_freshness": stocks_freshness,
                "metrics": stock_metrics[:50],
                "caveat": "" if stock_metrics else "Ready snapshot exists, but the MVP extractor did not find stable stock metric keys without exposing raw plan_json.",
            }

    def get_sku_snapshot(self, *, sku_or_nm_id: str, date_value: str | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            identity = self._sku_identity(conn, sku_or_nm_id)
            snapshot = self._ready_snapshot(conn, date_value)
            metrics: list[dict[str, Any]] = []
            if snapshot is not None:
                target_date = date_value or str(snapshot.get("as_of_date") or "")
                metrics, _, _ = self._metric_rows_for_range(
                    conn,
                    date_from=target_date,
                    date_to=target_date,
                    metric_keys=set(),
                    metric_query=None,
                    sku_or_nm_id=sku_or_nm_id,
                    limit=80,
                )
                if not metrics:
                    metrics = _extract_named_metrics(_safe_json_loads(snapshot.get("plan_json")), sku_or_nm_id, name_markers=())
            return {
                "status": "ok" if identity or metrics else "not_found",
                "sku_or_nm_id": sku_or_nm_id,
                "date": snapshot.get("as_of_date") if snapshot else date_value,
                "snapshot_id": snapshot.get("snapshot_id") if snapshot else None,
                "refreshed_at": snapshot.get("refreshed_at") if snapshot else None,
                "identity": identity,
                "metrics": metrics[:80],
                "missing_source_flags": _snapshot_missing_flags(snapshot, metrics),
            }

    def get_revenue_by_date(
        self,
        *,
        date_value: str,
        sku_or_nm_id: str | None = None,
        revenue_metric: str | None = None,
    ) -> dict[str, Any]:
        if not revenue_metric:
            return self._ambiguous_revenue_metric(date_from=date_value, date_to=date_value)
        with self._connect() as conn:
            metric_keys, metric_query, candidates = self._resolve_metric_selector(conn, revenue_metric)
            values, source_snapshots, truncated = self._metric_rows_for_range(
                conn,
                date_from=date_value,
                date_to=date_value,
                metric_keys=metric_keys,
                metric_query=metric_query,
                sku_or_nm_id=sku_or_nm_id,
                limit=100,
            )
            return {
                "status": "ok" if values else ("metric_projection_unavailable" if candidates else "metric_not_found"),
                "date": date_value,
                "sku_or_nm_id": sku_or_nm_id,
                "revenue_metric": revenue_metric,
                "candidate_metrics": candidates[:20],
                "source_snapshots": source_snapshots[:20],
                "truncated": truncated,
                "values": values,
                "source": "sheet_vitrina_v1_ready_snapshots",
                "caveat": "" if values else "Metric was requested explicitly, but no bounded projected value matched the requested date/SKU in persisted ready snapshots.",
            }

    def get_revenue_range(
        self,
        *,
        date_from: str,
        date_to: str,
        group_by: str,
        revenue_metric: str | None = None,
    ) -> dict[str, Any]:
        _validate_date_range(date_from, date_to, max_days=MAX_DATE_RANGE_DAYS)
        if group_by not in {"date", "sku", "total"}:
            raise WebCoreDataMcpError("group_by must be one of: date, sku, total", code="invalid_group_by")
        if not revenue_metric:
            return self._ambiguous_revenue_metric(date_from=date_from, date_to=date_to)
        with self._connect() as conn:
            if not _table_exists(conn, "sheet_vitrina_v1_ready_snapshots"):
                return _missing_table_result("sheet_vitrina_v1_ready_snapshots")
            metric_keys, metric_query, candidates = self._resolve_metric_selector(conn, revenue_metric)
            values, source_snapshots, truncated = self._metric_rows_for_range(
                conn,
                date_from=date_from,
                date_to=date_to,
                metric_keys=metric_keys,
                metric_query=metric_query,
                sku_or_nm_id=None,
                limit=100,
            )
        buckets = _group_metric_rows(values, group_by)
        return {
            "status": "ok" if buckets else ("metric_projection_unavailable" if candidates else "metric_not_found"),
            "date_from": date_from,
            "date_to": date_to,
            "group_by": group_by,
            "revenue_metric": revenue_metric,
            "candidate_metrics": candidates[:20],
            "source_snapshots": source_snapshots[:20],
            "buckets": buckets,
            "sample_values": values[:50],
            "truncated": truncated,
            "range_limit_days": MAX_DATE_RANGE_DAYS,
            "caveat": "" if buckets else "Metric was requested explicitly, but no bounded projected value matched the requested date range in persisted ready snapshots.",
        }

    def get_webcore_data_map(
        self,
        *,
        domain: str,
        include_examples: bool,
        include_limitations: bool,
    ) -> dict[str, Any]:
        normalized_domain = domain if domain in _data_map_domains() else "all"
        domains = _domain_catalog()
        filtered_domains = [
            item
            for item in domains
            if normalized_domain == "all" or item.get("domain") == normalized_domain
        ]
        table_catalog = _business_table_catalog()
        artifact_catalog = _artifact_kind_catalog()
        return {
            "status": "ok",
            "contract_name": "webcore_data_mcp_data_map",
            "contract_version": "v1",
            "generated_at": _utc_now(),
            "source": "derived_from_current_mcp_tool_definitions_and_repo_docs",
            "requested_domain": normalized_domain,
            "domains": filtered_domains,
            "tools": [
                {
                    "name": definition.name,
                    "required_scope": definition.scope,
                    "title": definition.title,
                }
                for definition in _tool_definitions()
                if normalized_domain != "all" and _model_tool_domain(definition.name) == normalized_domain
            ],
            "scopes": [
                {"scope": SCOPE_ANALYTICS_READ, "domains": ["navigation", "freshness", "metrics", "sku", "business_tables"]},
                {"scope": SCOPE_SUPPLY_READ, "domains": ["supplier_shipments", "wb_supplies", "artifacts", "cny", "factory_order"]},
                {"scope": SCOPE_FINANCE_READ, "domains": ["finance", "revenue"]},
                {"scope": SCOPE_OPS_READ, "domains": ["ops_diagnostics"]},
            ],
            "intent_examples": _intent_examples() if include_examples else [],
            "business_table_catalog": [
                _table_spec_public(spec)
                for spec in table_catalog.values()
                if normalized_domain == "all" or spec.get("domain") == normalized_domain
            ],
            "artifact_catalog": artifact_catalog if normalized_domain in {"all", "artifacts", "supplier_shipments", "cny"} else [],
            "boundary_rules": _boundary_rules() if include_limitations else [],
            "known_limitations": _known_limitations() if include_limitations else [],
            "not_source_of_truth_note": (
                "This map is a derived navigation layer over current MCP tools, allowlisted runtime tables "
                "and authoritative repo docs. It does not define new business truth."
            ),
        }

    def resolve_webcore_data_request(
        self,
        *,
        intent: str,
        domain: str,
        object_id: str | None,
        shipment_id: str | None,
        supply_id: str | None,
        invoice_no: str | None,
        supplier_name: str | None,
        sku_or_nm_id: str | None,
        metric_key_or_label: str | None,
        date_value: str | None,
        date_from: str | None,
        date_to: str | None,
        artifact_kind: str,
        mode: str,
        limit: int,
    ) -> dict[str, Any]:
        inferred = _infer_request_intent(
            intent,
            domain=domain,
            object_id=object_id,
            shipment_id=shipment_id,
            supply_id=supply_id,
            invoice_no=invoice_no,
            supplier_name=supplier_name,
            sku_or_nm_id=sku_or_nm_id,
            metric_key_or_label=metric_key_or_label,
            artifact_kind=artifact_kind,
        )
        calls: list[dict[str, Any]] = []
        unavailable: list[dict[str, Any]] = []
        resolved_shipment_id = shipment_id or (object_id if inferred.get("object_type") == "shipment" else None)
        resolved_supply_id = supply_id or (object_id if inferred.get("object_type") == "wb_supply" else None)
        action = str(inferred.get("action") or "")
        resolved_domain = str(inferred.get("domain") or "unknown")

        if resolved_domain == "freshness":
            calls.append(_recommended_call(1, "get_data_freshness_status", {}, SCOPE_ANALYTICS_READ, "Per-source freshness/readiness status."))
        elif resolved_domain == "metrics":
            selector = metric_key_or_label or object_id or ""
            if not selector:
                calls.append(_recommended_call(1, "list_metrics", {"query": "", "limit": limit}, SCOPE_ANALYTICS_READ, "Metric catalog with Russian labels and coverage hints."))
            else:
                metric_args: dict[str, Any] = {"metric_key_or_label": selector, "limit": limit}
                if date_value:
                    metric_args["date"] = date_value
                if date_from and date_to:
                    metric_args["date_from"] = date_from
                    metric_args["date_to"] = date_to
                if sku_or_nm_id:
                    metric_args["sku_or_nm_id"] = sku_or_nm_id
                calls.append(_recommended_call(1, "get_metric_values", metric_args, SCOPE_ANALYTICS_READ, "Persisted ready-snapshot metric values."))
        elif resolved_domain == "sku":
            query = sku_or_nm_id or object_id or intent
            calls.append(_recommended_call(1, "search_business_objects", {"query": query, "object_types": ["sku", "nomenclature"]}, SCOPE_ANALYTICS_READ, "Find SKU/nomenclature identity."))
            if sku_or_nm_id or object_id:
                args = {"sku_or_nm_id": sku_or_nm_id or object_id}
                if date_value:
                    args["date"] = date_value
                calls.append(_recommended_call(2, "get_sku_snapshot", args, SCOPE_ANALYTICS_READ, "SKU identity plus persisted ready snapshot metrics."))
        elif resolved_domain == "wb_supplies":
            if resolved_supply_id:
                calls.append(_recommended_call(1, "get_wb_supply_full_details", {"supply_id": resolved_supply_id}, SCOPE_SUPPLY_READ, "Expanded cached WB supply detail and scrubbed cached payloads."))
            else:
                args = {"limit": limit}
                if date_from:
                    args["date_from"] = date_from
                if date_to:
                    args["date_to"] = date_to
                calls.append(_recommended_call(1, "get_wb_supplies_registry", args, SCOPE_SUPPLY_READ, "Cached WB supplies registry/list; no upstream sync."))
        elif resolved_domain in {"supplier_shipments", "artifacts", "cny"}:
            if action in {"show_registry", "find_largest"}:
                args = {"limit": limit}
                if action == "find_largest":
                    args["limit"] = 1
                    args["sort_by"] = "product_qty_total_desc"
                if invoice_no:
                    args["invoice_no"] = invoice_no
                if supplier_name:
                    args["supplier_name"] = supplier_name
                if date_from:
                    args["date_from"] = date_from
                if date_to:
                    args["date_to"] = date_to
                calls.append(_recommended_call(1, "get_supplier_shipments_registry", args, SCOPE_SUPPLY_READ, "Supplier shipment registry rows with financial/document completeness."))
                if action == "find_largest":
                    calls.append(
                        _recommended_call(
                            2,
                            "get_supplier_shipment_full_details",
                            {"shipment_id": "<shipment_id from step 1>"},
                            SCOPE_SUPPLY_READ,
                            "Packing list summary and document metadata for the largest returned shipment.",
                        )
                    )
            elif action == "packing_list_summary":
                if resolved_shipment_id:
                    calls.append(_recommended_call(1, "get_supplier_shipment_full_details", {"shipment_id": resolved_shipment_id}, SCOPE_SUPPLY_READ, "Expanded shipment card with packing_list_summary aliases."))
                    calls.append(_recommended_call(2, "list_supply_artifacts", {"shipment_id": resolved_shipment_id, "artifact_kind": "packing_list", "limit": limit}, SCOPE_SUPPLY_READ, "Packing-list artifact_ref for metadata/parsed reads."))
                else:
                    calls.append(_recommended_call(1, "get_supplier_shipments_registry", {"sort_by": "product_qty_total_desc", "limit": 1}, SCOPE_SUPPLY_READ, "Find the largest shipment and its top-level packing-list fields."))
                    calls.append(
                        _recommended_call(
                            2,
                            "get_supplier_shipment_full_details",
                            {"shipment_id": "<shipment_id from step 1>"},
                            SCOPE_SUPPLY_READ,
                            "Use the returned shipment_id for full packing-list summary and line sample.",
                        )
                    )
            elif action in {"show_documents", "open_artifact"}:
                if not resolved_shipment_id and invoice_no:
                    calls.append(_recommended_call(1, "search_business_objects", {"query": invoice_no, "object_types": ["shipment"]}, SCOPE_ANALYTICS_READ, "Find shipment id by invoice number before listing artifacts."))
                artifact_args: dict[str, Any] = {"limit": limit}
                if resolved_shipment_id:
                    artifact_args["shipment_id"] = resolved_shipment_id
                if artifact_kind and artifact_kind != "auto":
                    artifact_args["artifact_kind"] = artifact_kind
                calls.append(_recommended_call(2 if not resolved_shipment_id and invoice_no else 1, "list_supply_artifacts", artifact_args, SCOPE_SUPPLY_READ, "Server-owned supply artifact metadata and opaque artifact refs."))
                if action == "open_artifact":
                    unavailable.append(
                        {
                            "capability": "direct artifact read without artifact_ref",
                            "reason": "Resolve an artifact_ref with list_supply_artifacts first, then call get_supply_artifact.",
                        }
                    )
            elif resolved_shipment_id:
                calls.append(_recommended_call(1, "get_supplier_shipment_full_details", {"shipment_id": resolved_shipment_id}, SCOPE_SUPPLY_READ, "Expanded shipment header, lines, documents, expenses, CNY links and artifact refs."))
            else:
                search_query = invoice_no or supplier_name or object_id or intent
                calls.append(_recommended_call(1, "search_business_objects", {"query": search_query, "object_types": ["shipment"]}, SCOPE_ANALYTICS_READ, "Find candidate supplier shipment ids."))
                calls.append(_recommended_call(2, "get_supplier_shipments_registry", {"limit": limit}, SCOPE_SUPPLY_READ, "Fallback registry listing if search is insufficient."))
        elif resolved_domain == "business_tables":
            calls.append(_recommended_call(1, "list_webcore_business_tables", {}, SCOPE_ANALYTICS_READ, "Allowlisted runtime business table catalog."))
        else:
            calls.append(_recommended_call(1, "get_webcore_data_map", {"domain": "all"}, SCOPE_ANALYTICS_READ, "Orientation map for current MCP tools and domains."))

        if mode in {"open_or_read", "download_hint"} and action == "open_artifact":
            unavailable.append(
                {
                    "capability": "arbitrary file open/download",
                    "reason": "MCP only reads server-owned artifacts by opaque artifact_ref with bounded modes.",
                    "safe_next_tool": "get_supply_artifact",
                }
            )
        return {
            "status": "ok" if calls else "ambiguous_intent",
            "contract_name": "webcore_data_mcp_request_resolution",
            "contract_version": "v1",
            "interpreted_intent": inferred,
            "confidence": inferred.get("confidence") or "medium",
            "recommended_calls": calls,
            "fallback_path": _fallback_path_for_domain(resolved_domain),
            "unavailable_capabilities": unavailable,
            "notes": ["Resolver did not execute the recommended calls.", "All recommended tools are read-only MCP calls."],
        }

    def list_webcore_business_tables(self, *, domain: str | None = None, include_missing: bool) -> dict[str, Any]:
        normalized_domain = (domain or "").strip()
        catalog = _business_table_catalog()
        with self._connect() as conn:
            rows = []
            for table, spec in catalog.items():
                if normalized_domain and spec.get("domain") != normalized_domain:
                    continue
                exists = _table_exists(conn, table)
                if not exists and not include_missing:
                    continue
                item = _table_spec_public(spec)
                item["exists"] = exists
                if exists:
                    item["column_count"] = len(_table_columns(conn, table))
                rows.append(item)
        return {
            "status": "ok",
            "contract_name": "webcore_data_mcp_business_table_catalog",
            "contract_version": "v1",
            "source": "allowlisted_runtime_tables",
            "domain": domain,
            "tables": rows,
            "boundary": "Generated SELECT only; no arbitrary SQL or auth/session/secrets tables.",
        }

    def get_webcore_business_table_schema(self, *, table: str) -> dict[str, Any]:
        spec = _require_table_spec(table)
        with self._connect() as conn:
            if not _table_exists(conn, table):
                return _missing_table_result(table)
            columns = _table_columns(conn, table)
        safe_columns = _safe_table_columns(columns, spec, include_raw_business_payloads=False)
        raw_columns = [column for column in columns if column in set(spec.get("raw_columns") or [])]
        redacted_columns = [column for column in columns if column in set(spec.get("sensitive_columns") or [])]
        return {
            "status": "ok",
            "contract_name": "webcore_data_mcp_business_table_schema",
            "contract_version": "v1",
            "table": table,
            "domain": spec.get("domain"),
            "description": spec.get("description"),
            "primary_id_columns": spec.get("primary_id_columns") or [],
            "date_columns": spec.get("date_columns") or [],
            "allowlisted_columns": safe_columns,
            "raw_business_payload_columns": raw_columns,
            "sensitive_redacted_columns": redacted_columns,
            "allowed_filters": safe_columns,
            "allowed_order_by": _allowed_order_columns(columns, spec),
            "default_order_by": spec.get("default_order_by") or "",
            "redaction_policy": "Sensitive/path/hash/auth columns are omitted/redacted. Raw business payloads require explicit include flag and are scrubbed/bounded.",
        }

    def get_webcore_business_table_rows(
        self,
        *,
        table: str,
        filters: Mapping[str, Any],
        date_from: str | None,
        date_to: str | None,
        limit: int,
        cursor: str | None,
        offset: int,
        order_by: str | None,
        include_raw_business_payloads: bool,
    ) -> dict[str, Any]:
        spec = _require_table_spec(table)
        effective_offset = _cursor_to_offset(cursor, offset)
        derived_supplier_status_filter: Any = None
        effective_filters = dict(filters)
        if table == "sheet_vitrina_v1_supplier_shipments":
            derived_supplier_status_filter = effective_filters.pop("order_status", None)
        with self._connect() as conn:
            if not _table_exists(conn, table):
                return _missing_table_result(table)
            columns = _table_columns(conn, table)
            selected_columns = _safe_table_columns(columns, spec, include_raw_business_payloads=include_raw_business_payloads)
            where_sql, params, applied_filters = _build_table_where(
                columns,
                spec,
                filters=effective_filters,
                date_from=date_from,
                date_to=date_to,
            )
            order_sql = _order_by_sql(columns, spec, order_by)
            if derived_supplier_status_filter is not None:
                values = (
                    list(derived_supplier_status_filter[:20])
                    if isinstance(derived_supplier_status_filter, list)
                    else [derived_supplier_status_filter]
                )
                if values:
                    today = current_business_date_iso()
                    predicate = _derived_supplier_status_sql(
                        "",
                        historical_exception_available=(
                            "historical_status_exception" in columns
                        ),
                    )
                    predicate += " IN (" + ", ".join("?" for _ in values) + ")"
                    where_sql = (
                        where_sql + " AND " + predicate
                        if where_sql
                        else "WHERE " + predicate
                    )
                    params.extend((today, today, *values))
                    applied_filters["order_status"] = (
                        values if isinstance(derived_supplier_status_filter, list) else values[0]
                    )
            supplier_order_direction = _supplier_status_order_direction(order_by)
            if table == "sheet_vitrina_v1_supplier_shipments" and supplier_order_direction:
                today = current_business_date_iso()
                order_sql = (
                    "ORDER BY "
                    + _derived_supplier_status_sql(
                        "",
                        historical_exception_available=(
                            "historical_status_exception" in columns
                        ),
                    )
                    + f" {supplier_order_direction}"
                )
                params.extend((today, today))
            quoted_columns = ", ".join(_quote_ident(column) for column in selected_columns)
            rows = conn.execute(
                f"""
                SELECT {quoted_columns}
                FROM {_quote_ident(table)}
                {where_sql}
                {order_sql}
                LIMIT ? OFFSET ?
                """,
                (*params, limit + 1, effective_offset),
            ).fetchall()
        raw_columns = set(spec.get("raw_columns") or [])
        payload_rows = [
            _business_row_payload(
                apply_derived_supplier_status(_row_dict(row))
                if table == "sheet_vitrina_v1_supplier_shipments"
                else _row_dict(row),
                raw_columns=raw_columns,
                include_raw_business_payloads=include_raw_business_payloads,
            )
            for row in rows[:limit]
        ]
        truncated = len(rows) > limit
        return {
            "status": "ok",
            "contract_name": "webcore_data_mcp_business_table_rows",
            "contract_version": "v1",
            "source_table": table,
            "domain": spec.get("domain"),
            "columns": selected_columns,
            "applied_filters": applied_filters,
            "include_raw_business_payloads": include_raw_business_payloads,
            "rows": payload_rows,
            "pagination": {
                "limit": limit,
                "offset": effective_offset,
                "next_cursor": str(effective_offset + limit) if truncated else "",
                "truncated": truncated,
            },
            "redaction_notes": [
                "Sensitive/path/hash/auth columns are not returned.",
                "Raw business JSON columns are returned only as scrubbed_payload fields when explicitly requested.",
            ],
        }

    def get_supplier_shipments_registry(
        self,
        *,
        shipment_id: str | None,
        invoice_no: str | None,
        supplier_name: str | None,
        order_status: str | None,
        match_status: str | None,
        document_status: str | None,
        date_from: str | None,
        date_to: str | None,
        sort_by: str,
        limit: int,
        cursor: str | None,
        offset: int,
    ) -> dict[str, Any]:
        effective_offset = _cursor_to_offset(cursor, offset)
        with self._connect() as conn:
            if not _table_exists(conn, "sheet_vitrina_v1_supplier_shipments"):
                return _missing_table_result("sheet_vitrina_v1_supplier_shipments")
            supplier_columns = _table_columns(
                conn, "sheet_vitrina_v1_supplier_shipments"
            )
            historical_exception_select = (
                "s.historical_status_exception"
                if "historical_status_exception" in supplier_columns
                else "'' AS historical_status_exception"
            )
            clauses: list[str] = []
            params: list[Any] = []
            _append_like_filter(clauses, params, "s.shipment_id", shipment_id)
            _append_like_filter(clauses, params, "s.invoice_no", invoice_no)
            _append_like_filter(clauses, params, "s.supplier_name", supplier_name)
            if order_status:
                today = current_business_date_iso()
                clauses.append(
                    _derived_supplier_status_sql(
                        "s",
                        historical_exception_available=(
                            "historical_status_exception"
                            in _table_columns(
                                conn, "sheet_vitrina_v1_supplier_shipments"
                            )
                        ),
                    )
                    + " = ?"
                )
                params.extend((today, today, order_status))
            _append_exact_filter(clauses, params, "s.match_status", match_status)
            if date_from:
                clauses.append("COALESCE(s.shipment_date, s.invoice_date, s.created_at, '') >= ?")
                params.append(date_from)
            if date_to:
                clauses.append("COALESCE(s.shipment_date, s.invoice_date, s.created_at, '') <= ?")
                params.append(date_to)
            if document_status:
                clauses.append(
                    "EXISTS (SELECT 1 FROM sheet_vitrina_v1_supplier_financial_documents fd_status "
                    "WHERE fd_status.supplier_order_id = s.shipment_id AND fd_status.parse_status = ?)"
                )
                params.append(document_status)
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            order_sql = _supplier_registry_order_by(sort_by)
            rows = conn.execute(
                f"""
                SELECT s.shipment_id, s.created_at, s.updated_at, s.shipment_date, s.actual_shipment_date,
                       s.actual_ff_acceptance_date, {historical_exception_select},
                       s.order_status, s.invoice_no, s.invoice_date, s.contract_no,
                       s.contract_date, s.supplier_name, s.currency, s.product_qty_total, s.product_amount_total,
                       s.extras_amount_total, s.invoice_amount_total, s.declared_invoice_total, s.match_status,
                       COUNT(DISTINCT l.line_id) AS line_count,
                       COUNT(DISTINCT CASE WHEN l.internal_nm_id IS NOT NULL THEN l.internal_nm_id END) AS matched_nm_id_count,
                       COALESCE(SUM(CASE WHEN l.qty IS NULL THEN 0 ELSE l.qty END), 0) AS line_qty_total,
                       COUNT(DISTINCT fd.document_id) AS financial_document_count,
                       COALESCE(SUM(CASE WHEN fe.amount_rub IS NULL THEN 0 ELSE fe.amount_rub END), 0) AS expense_amount_rub,
                       COUNT(DISTINCT td.document_id) AS trade_document_count
                FROM sheet_vitrina_v1_supplier_shipments s
                LEFT JOIN sheet_vitrina_v1_supplier_shipment_lines l ON l.shipment_id = s.shipment_id
                LEFT JOIN sheet_vitrina_v1_supplier_financial_documents fd ON fd.supplier_order_id = s.shipment_id
                LEFT JOIN sheet_vitrina_v1_supplier_financial_expense_lines fe ON fe.supplier_order_id = s.shipment_id
                LEFT JOIN sheet_vitrina_v1_trade_documents td ON td.source_shipment_id = s.shipment_id
                {where_sql}
                GROUP BY s.shipment_id
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
                """,
                (*params, limit + 1, effective_offset),
            ).fetchall()
            packing_docs_by_shipment = _fetch_packing_list_docs_for_shipments(
                conn,
                [str(row["shipment_id"] or "") for row in rows[:limit]],
            )
        result_rows = []
        for row in rows[:limit]:
            item = apply_derived_supplier_status(_row_dict(row))
            packing_summary = _packing_list_summary_from_documents(
                packing_docs_by_shipment.get(str(item.get("shipment_id") or ""), []),
                line_item_limit=0,
            )
            qty = _first_number(item.get("product_qty_total"), item.get("line_qty_total"))
            expenses = _number_or_zero(item.get("expense_amount_rub"))
            invoice_amount = _first_number(item.get("invoice_amount_total"), item.get("product_amount_total"))
            item["core_metrics"] = {
                "quantity_evidence": qty,
                "invoice_amount_evidence": invoice_amount,
                "expense_amount_rub": expenses,
                "available_unit_cost_evidence": ((invoice_amount or 0) + expenses) / qty if qty and qty > 0 else None,
            }
            item["packing_list_document_count"] = packing_summary.get("document_count")
            item["packing_list_parse_status"] = packing_summary.get("parse_status")
            item["packing_list_total_cartons"] = packing_summary.get("total_cartons")
            item["packing_list_box_count"] = packing_summary.get("box_count")
            item["packing_list_carton_count"] = packing_summary.get("carton_count")
            item["packing_list_total_boxes"] = packing_summary.get("total_boxes")
            item["packing_list_total_quantity"] = packing_summary.get("total_quantity")
            item["packing_list_total_volume_m3"] = packing_summary.get("total_volume_m3")
            item["packing_list_total_gross_weight_kg"] = packing_summary.get("total_gross_weight_kg")
            item["packing_list_model_count"] = packing_summary.get("model_count")
            item["packing_list_avg_qty_per_carton"] = packing_summary.get("avg_qty_per_carton")
            item["packing_list_reason"] = packing_summary.get("reason")
            item["packing_list_summary"] = packing_summary
            item["completeness_flags"] = {
                "has_lines": bool(item.get("line_count")),
                "has_matched_nm_id": bool(item.get("matched_nm_id_count")),
                "has_financial_documents": bool(item.get("financial_document_count")),
                "has_trade_documents": bool(item.get("trade_document_count")),
                "has_fact_dates": bool(item.get("actual_shipment_date") and item.get("actual_ff_acceptance_date")),
                "has_packing_list": bool(packing_summary.get("document_count")),
                "packing_list_parsed": bool(packing_summary.get("document_count")) and packing_summary.get("parse_status") == "parsed",
            }
            result_rows.append(item)
        return {
            "status": "ok",
            "contract_name": "webcore_data_mcp_supplier_shipments_registry",
            "contract_version": "v1",
            "source_tables": [
                "sheet_vitrina_v1_supplier_shipments",
                "sheet_vitrina_v1_supplier_shipment_lines",
                "sheet_vitrina_v1_supplier_financial_documents",
                "sheet_vitrina_v1_supplier_financial_expense_lines",
                "sheet_vitrina_v1_trade_documents",
            ],
            "filters": {
                "shipment_id": shipment_id,
                "invoice_no": invoice_no,
                "supplier_name": supplier_name,
                "order_status": order_status,
                "match_status": match_status,
                "document_status": document_status,
                "date_from": date_from,
                "date_to": date_to,
                "sort_by": sort_by,
            },
            "rows": result_rows,
            "pagination": {"limit": limit, "offset": effective_offset, "next_cursor": str(effective_offset + limit) if len(rows) > limit else "", "truncated": len(rows) > limit},
        }

    def get_supplier_shipment_full_details(
        self,
        *,
        shipment_id: str,
        include_raw_business_payloads: bool,
        line_limit: int,
        document_limit: int,
    ) -> dict[str, Any]:
        base = self.get_supplier_shipment_details(shipment_id=shipment_id)
        if base.get("status") != "ok":
            return base
        with self._connect() as conn:
            lines = _fetch_table_rows_for_owner(
                conn,
                table="sheet_vitrina_v1_supplier_shipment_lines",
                owner_column="shipment_id",
                owner_id=shipment_id,
                limit=line_limit,
                omit_columns={"raw_json"},
                include_raw_business_payloads=include_raw_business_payloads,
                order_by="sort_order ASC, line_id ASC",
            )
            financial_documents = _fetch_table_rows_for_owner(
                conn,
                table="sheet_vitrina_v1_supplier_financial_documents",
                owner_column="supplier_order_id",
                owner_id=shipment_id,
                limit=document_limit,
                omit_columns={"stored_file_path", "file_sha256", "raw_parse_json"},
                include_raw_business_payloads=include_raw_business_payloads,
                order_by="document_date DESC, uploaded_at DESC, document_id ASC",
            )
            expense_lines = _fetch_table_rows_for_owner(
                conn,
                table="sheet_vitrina_v1_supplier_financial_expense_lines",
                owner_column="supplier_order_id",
                owner_id=shipment_id,
                limit=MAX_LIMIT,
                omit_columns={"raw_json"},
                include_raw_business_payloads=include_raw_business_payloads,
                order_by="financial_document_id ASC, sort_order ASC, line_id ASC",
            )
            trade_documents = _fetch_table_rows_for_owner(
                conn,
                table="sheet_vitrina_v1_trade_documents",
                owner_column="source_shipment_id",
                owner_id=shipment_id,
                limit=document_limit,
                omit_columns={"file_path", "file_sha256"},
                include_raw_business_payloads=include_raw_business_payloads,
                order_by="updated_at DESC, document_id ASC",
            )
            cny_documents = _fetch_table_rows_for_owner(
                conn,
                table="sheet_vitrina_v1_cny_documents",
                owner_column="source_order_id",
                owner_id=shipment_id,
                limit=document_limit,
                omit_columns={"stored_file_path", "file_sha256", "raw_parse_json"},
                include_raw_business_payloads=include_raw_business_payloads,
                order_by="operation_date DESC, document_id ASC",
            )
            artifact_rows = self._artifact_rows(conn, shipment_id=shipment_id, supplier_order_id=shipment_id, artifact_kind=None, source_domain=None)
            packing_docs = _fetch_packing_list_docs_for_shipments(conn, [shipment_id]).get(shipment_id, [])
        packing_summary = _packing_list_summary_from_documents(packing_docs, line_item_limit=min(line_limit, 20))
        return {
            **base,
            "contract_name": "webcore_data_mcp_supplier_shipment_full_details",
            "contract_version": "v1",
            "lines": lines,
            "financial_documents_metadata": _with_artifact_refs(financial_documents, source_domain="financial_documents"),
            "financial_expense_lines": expense_lines,
            "trade_documents_metadata": _with_artifact_refs(trade_documents, source_domain="trade_documents"),
            "cny_documents_metadata": _with_artifact_refs(cny_documents, source_domain="cny_documents"),
            "artifact_refs": [_artifact_public(row) for row in artifact_rows[:document_limit]],
            "packing_list_summary": packing_summary,
            "document_parsed_fields_summary": _document_parsed_fields_summary(packing_docs),
            "redaction": "No absolute paths, hashes, secrets, raw DB payloads or unbounded file contents are exposed.",
        }

    def get_wb_supplies_registry(
        self,
        *,
        status_filter: str | None,
        warehouse: str | None,
        supply_id: str | None,
        wb_supply_id: str | None,
        preorder_id: str | None,
        date_from: str | None,
        date_to: str | None,
        limit: int,
        cursor: str | None,
        offset: int,
    ) -> dict[str, Any]:
        effective_offset = _cursor_to_offset(cursor, offset)
        with self._connect() as conn:
            if not _table_exists(conn, "sheet_vitrina_v1_wb_supplies"):
                return _missing_table_result("sheet_vitrina_v1_wb_supplies")
            clauses: list[str] = []
            params: list[Any] = []
            _append_like_filter(clauses, params, "s.supply_id", supply_id)
            _append_like_filter(clauses, params, "s.wb_supply_id", wb_supply_id)
            _append_like_filter(clauses, params, "s.preorder_id", preorder_id)
            if status_filter:
                if status_filter.isdigit():
                    clauses.append("s.status_id = ?")
                    params.append(int(status_filter))
                else:
                    clauses.append("LOWER(s.normalized_row_json) LIKE ?")
                    params.append(f"%{status_filter.lower()}%")
            if warehouse:
                clauses.append("LOWER(s.normalized_row_json) LIKE ?")
                params.append(f"%{warehouse.lower()}%")
            if date_from:
                clauses.append("COALESCE(substr(s.supply_date, 1, 10), substr(s.fact_date, 1, 10), substr(s.updated_date, 1, 10), '') >= ?")
                params.append(date_from)
            if date_to:
                clauses.append("COALESCE(substr(s.supply_date, 1, 10), substr(s.fact_date, 1, 10), substr(s.updated_date, 1, 10), '') <= ?")
                params.append(date_to)
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"""
                SELECT s.supply_id, s.cache_key, s.wb_supply_id, s.preorder_id, s.normalized_row_json,
                       s.warehouse_id, s.status_id, s.quantity_for_size_filter, s.source_created_at,
                       s.supply_date, s.fact_date, s.updated_date, s.synced_at, s.last_list_synced_at,
                       s.last_enriched_at, s.enrichment_status, s.enrichment_error
                FROM sheet_vitrina_v1_wb_supplies s
                {where_sql}
                ORDER BY COALESCE(s.supply_date, s.fact_date, s.updated_date, s.synced_at, '') DESC, s.supply_id
                LIMIT ? OFFSET ?
                """,
                (*params, limit + 1, effective_offset),
            ).fetchall()
        result_rows = []
        for row in rows[:limit]:
            item = _row_dict(row)
            normalized = _json_object(item.pop("normalized_row_json", None))
            item["normalized"] = _scrub_business_payload(
                _select_keys(
                    normalized,
                    (
                        "supply_id",
                        "id",
                        "status",
                        "status_name",
                        "warehouse_name",
                        "planned_warehouse_name",
                        "target_warehouse_name",
                        "quantity",
                        "goods_count",
                        "route",
                        "amount",
                        "currency",
                        "cost_total",
                    ),
                )
            )
            item["cache_only"] = True
            result_rows.append(item)
        return {
            "status": "ok",
            "contract_name": "webcore_data_mcp_wb_supplies_registry",
            "contract_version": "v1",
            "source_table": "sheet_vitrina_v1_wb_supplies",
            "cache_only": True,
            "filters": {
                "status_filter": status_filter,
                "warehouse": warehouse,
                "supply_id": supply_id,
                "wb_supply_id": wb_supply_id,
                "preorder_id": preorder_id,
                "date_from": date_from,
                "date_to": date_to,
            },
            "rows": result_rows,
            "pagination": {"limit": limit, "offset": effective_offset, "next_cursor": str(effective_offset + limit) if len(rows) > limit else "", "truncated": len(rows) > limit},
        }

    def get_wb_supply_full_details(self, *, supply_id: str, include_raw_business_payloads: bool) -> dict[str, Any]:
        with self._connect() as conn:
            if not _table_exists(conn, "sheet_vitrina_v1_wb_supplies"):
                return _missing_table_result("sheet_vitrina_v1_wb_supplies")
            row = conn.execute(
                """
                SELECT *
                FROM sheet_vitrina_v1_wb_supplies
                WHERE supply_id = ? OR wb_supply_id = ? OR preorder_id = ?
                LIMIT 1
                """,
                (supply_id, supply_id, supply_id),
            ).fetchone()
        if row is None:
            return {"status": "not_found", "supply_id": supply_id, "cache_only": True}
        item = _row_dict(row)
        normalized = _json_object(item.pop("normalized_row_json", None))
        detail = _safe_json_loads(item.pop("raw_detail_json", None))
        goods = _safe_json_loads(item.pop("raw_goods_json", None))
        package = _safe_json_loads(item.pop("raw_package_json", None))
        item = _omit_keys(item, {"file_path", "stored_file_path", "source_file_path", "file_sha256", "sha256"})
        payload: dict[str, Any] = {
            "status": "ok",
            "contract_name": "webcore_data_mcp_wb_supply_full_details",
            "contract_version": "v1",
            "source_table": "sheet_vitrina_v1_wb_supplies",
            "cache_only": True,
            "no_upstream_fetch": True,
            "supply": {**item, "normalized": _scrub_business_payload(normalized)},
            "cached_payloads": {
                "detail": _scrub_business_payload(detail) if include_raw_business_payloads else _compact_json_summary(detail),
                "goods": _scrub_business_payload(goods) if include_raw_business_payloads else _compact_json_summary(goods),
                "package": _scrub_business_payload(package) if include_raw_business_payloads else _compact_json_summary(package),
            },
            "redaction": "Cached business payloads are scrubbed and bounded; no upstream fetch is performed.",
        }
        return payload

    def list_supply_artifacts(
        self,
        *,
        shipment_id: str | None,
        supplier_order_id: str | None,
        artifact_kind: str | None,
        source_domain: str | None,
        limit: int,
        cursor: str | None,
        offset: int,
    ) -> dict[str, Any]:
        effective_offset = _cursor_to_offset(cursor, offset)
        with self._connect() as conn:
            rows = self._artifact_rows(
                conn,
                shipment_id=shipment_id,
                supplier_order_id=supplier_order_id,
                artifact_kind=artifact_kind,
                source_domain=source_domain,
            )
        page = rows[effective_offset : effective_offset + limit]
        return {
            "status": "ok",
            "contract_name": "webcore_data_mcp_supply_artifacts",
            "contract_version": "v1",
            "source": "allowlisted_runtime_artifact_registry",
            "filters": {
                "shipment_id": shipment_id,
                "supplier_order_id": supplier_order_id,
                "artifact_kind": artifact_kind,
                "source_domain": source_domain,
            },
            "artifacts": [_artifact_public(row) for row in page],
            "pagination": {"limit": limit, "offset": effective_offset, "next_cursor": str(effective_offset + limit) if effective_offset + limit < len(rows) else "", "truncated": effective_offset + limit < len(rows)},
            "boundary": "artifact_ref is opaque; no absolute server paths are exposed.",
        }

    def get_supply_artifact(
        self,
        *,
        artifact_ref: str,
        mode: str,
        chunk: int,
        offset: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        normalized_mode = mode if mode in {"metadata", "parsed", "text", "text_chunk", "base64_chunk"} else "metadata"
        with self._connect() as conn:
            artifact = self._resolve_artifact_ref(conn, artifact_ref)
        if artifact is None:
            return {"status": "not_found", "artifact_ref": artifact_ref}
        public = _artifact_public(artifact)
        if normalized_mode == "metadata":
            path_result = self._resolve_artifact_file_path(artifact)
            if path_result.get("status") == "ok":
                public["availability"] = {**(public.get("availability") or {}), "file_available": True}
                public["size_bytes"] = path_result["path"].stat().st_size
            else:
                public["availability"] = {
                    **(public.get("availability") or {}),
                    "file_available": False,
                    "file_status": path_result.get("status"),
                }
            return {"status": "ok", "contract_name": "webcore_data_mcp_supply_artifact", "mode": "metadata", "artifact": public}
        if normalized_mode == "parsed":
            parsed = artifact.get("parsed_payload")
            if parsed in (None, "", {}, []):
                return {"status": "parsed_unavailable", "artifact": public}
            result = {
                "status": "ok",
                "contract_name": "webcore_data_mcp_supply_artifact",
                "mode": "parsed",
                "artifact": public,
                "parsed_business_payload": _scrub_business_payload(parsed),
            }
            if public.get("artifact_kind") == "packing_list":
                result["packing_list_summary"] = _packing_list_summary_from_documents(
                    [
                        {
                            "document_id": public.get("linked_document_id"),
                            "document_type": "packing_list",
                            "parse_status": public.get("parse_status"),
                            "normalized_parse_json": parsed,
                        }
                    ],
                    line_item_limit=20,
                )
            return result
        path_result = self._resolve_artifact_file_path(artifact)
        if path_result.get("status") != "ok":
            return {"status": path_result.get("status"), "artifact": public, "reason": path_result.get("reason") or ""}
        file_path = path_result["path"]
        size = file_path.stat().st_size
        if normalized_mode in {"text", "text_chunk"}:
            if not _artifact_text_content_supported(public):
                return {"status": "text_unavailable", "artifact": public, "reason": "No safe text extractor is exposed for this content type in MCP."}
            if normalized_mode == "text" and size > max_bytes:
                return {
                    "status": "too_large",
                    "artifact": public,
                    "reason": "Use mode=text_chunk with offset/chunk for bounded reads.",
                    "size_bytes": size,
                    "max_bytes": max_bytes,
                }
            start = offset if normalized_mode == "text_chunk" else chunk * max_bytes
            data = _read_file_chunk(file_path, start=start, max_bytes=max_bytes)
            text = _safe_decode_bytes(data)
            return {
                "status": "ok",
                "contract_name": "webcore_data_mcp_supply_artifact",
                "mode": normalized_mode,
                "artifact": public,
                "chunk": {"offset": start, "size_bytes": len(data), "next_offset": start + len(data) if start + len(data) < size else None, "total_size_bytes": size},
                "text": _redact_sensitive_text(text),
            }
        if normalized_mode == "base64_chunk":
            if not _artifact_binary_content_allowed(public):
                return {"status": "unsupported_content_type", "artifact": public}
            start = offset or (chunk * max_bytes)
            data = _read_file_chunk(file_path, start=start, max_bytes=max_bytes)
            return {
                "status": "ok",
                "contract_name": "webcore_data_mcp_supply_artifact",
                "mode": "base64_chunk",
                "artifact": public,
                "chunk": {"offset": start, "size_bytes": len(data), "next_offset": start + len(data) if start + len(data) < size else None, "total_size_bytes": size},
                "base64": base64.b64encode(data).decode("ascii"),
            }
        return {"status": "unsupported_mode", "artifact": public}

    def _artifact_rows(
        self,
        conn: sqlite3.Connection,
        *,
        shipment_id: str | None,
        supplier_order_id: str | None,
        artifact_kind: str | None,
        source_domain: str | None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        shipment_filter = shipment_id or supplier_order_id
        if _source_domain_matches(source_domain, "trade_documents") and _table_exists(conn, "sheet_vitrina_v1_trade_documents"):
            columns = set(_table_columns(conn, "sheet_vitrina_v1_trade_documents"))
            selected = _select_existing_columns(
                columns,
                [
                    "document_id",
                    "document_type",
                    "number",
                    "document_date",
                    "supplier_name",
                    "currency",
                    "amount_total",
                    "source_shipment_id",
                    "file_original_name",
                    "file_content_type",
                    "file_path",
                    "parsed_metadata_json",
                    "warnings_json",
                    "errors_json",
                    "status",
                    "created_at",
                    "updated_at",
                ],
            )
            clauses: list[str] = []
            params: list[Any] = []
            if shipment_filter and "source_shipment_id" in columns:
                clauses.append("source_shipment_id = ?")
                params.append(shipment_filter)
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            for row in conn.execute(
                f"SELECT {', '.join(_quote_ident(col) for col in selected)} FROM sheet_vitrina_v1_trade_documents {where_sql} ORDER BY updated_at DESC",
                params,
            ).fetchall():
                item = _row_dict(row)
                parsed = _safe_json_loads(item.pop("parsed_metadata_json", None))
                rows.append(
                    _artifact_row(
                        artifact_ref=f"trade_document:{item.get('document_id')}",
                        artifact_kind=str(item.get("document_type") or "unknown_business_document"),
                        source_domain="trade_documents",
                        source_table="sheet_vitrina_v1_trade_documents",
                        linked_shipment_id=str(item.get("source_shipment_id") or ""),
                        linked_document_id=str(item.get("document_id") or ""),
                        filename=str(item.get("file_original_name") or "document"),
                        content_type=str(item.get("file_content_type") or "application/octet-stream"),
                        stored_path=str(item.get("file_path") or ""),
                        uploaded_at=str(item.get("created_at") or ""),
                        updated_at=str(item.get("updated_at") or ""),
                        status=str(item.get("status") or ""),
                        parse_status=str(item.get("status") or ""),
                        parsed_payload=parsed,
                    )
                )
        if _source_domain_matches(source_domain, "financial_documents") and _table_exists(conn, "sheet_vitrina_v1_supplier_financial_documents"):
            columns = set(_table_columns(conn, "sheet_vitrina_v1_supplier_financial_documents"))
            selected = _select_existing_columns(
                columns,
                [
                    "document_id",
                    "supplier_order_id",
                    "document_type",
                    "original_filename",
                    "stored_file_path",
                    "file_content_type",
                    "uploaded_at",
                    "updated_at",
                    "parse_status",
                    "document_number",
                    "document_date",
                    "currency",
                    "total_amount",
                    "total_amount_rub",
                    "normalized_parse_json",
                    "warnings_json",
                    "errors_json",
                ],
            )
            clauses = []
            params = []
            if shipment_filter and "supplier_order_id" in columns:
                clauses.append("supplier_order_id = ?")
                params.append(shipment_filter)
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            for row in conn.execute(
                f"SELECT {', '.join(_quote_ident(col) for col in selected)} FROM sheet_vitrina_v1_supplier_financial_documents {where_sql} ORDER BY uploaded_at DESC",
                params,
            ).fetchall():
                item = _row_dict(row)
                parsed = _safe_json_loads(item.pop("normalized_parse_json", None))
                rows.append(
                    _artifact_row(
                        artifact_ref=f"financial_document:{item.get('supplier_order_id')}:{item.get('document_id')}",
                        artifact_kind=str(item.get("document_type") or "unknown_business_document"),
                        source_domain="financial_documents",
                        source_table="sheet_vitrina_v1_supplier_financial_documents",
                        linked_shipment_id=str(item.get("supplier_order_id") or ""),
                        linked_document_id=str(item.get("document_id") or ""),
                        filename=str(item.get("original_filename") or "financial-document.pdf"),
                        content_type=str(item.get("file_content_type") or "application/pdf"),
                        stored_path=str(item.get("stored_file_path") or ""),
                        uploaded_at=str(item.get("uploaded_at") or ""),
                        updated_at=str(item.get("updated_at") or ""),
                        status=str(item.get("parse_status") or ""),
                        parse_status=str(item.get("parse_status") or ""),
                        parsed_payload=parsed,
                    )
                )
        if _source_domain_matches(source_domain, "cny_documents") and _table_exists(conn, "sheet_vitrina_v1_cny_documents"):
            columns = set(_table_columns(conn, "sheet_vitrina_v1_cny_documents"))
            selected = _select_existing_columns(
                columns,
                [
                    "document_id",
                    "document_type",
                    "source_order_id",
                    "context_order_id",
                    "linked_financial_document_id",
                    "original_filename",
                    "stored_file_path",
                    "file_content_type",
                    "uploaded_at",
                    "updated_at",
                    "operation_date",
                    "status",
                    "document_number",
                    "currency",
                    "rub_amount",
                    "cny_amount",
                    "parsed_payload_json",
                    "warnings_json",
                    "errors_json",
                ],
            )
            clauses = []
            params = []
            if shipment_filter and "source_order_id" in columns:
                clauses.append("(source_order_id = ? OR context_order_id = ?)")
                params.extend([shipment_filter, shipment_filter])
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            for row in conn.execute(
                f"SELECT {', '.join(_quote_ident(col) for col in selected)} FROM sheet_vitrina_v1_cny_documents {where_sql} ORDER BY operation_date DESC, uploaded_at DESC",
                params,
            ).fetchall():
                item = _row_dict(row)
                parsed = _safe_json_loads(item.pop("parsed_payload_json", None))
                rows.append(
                    _artifact_row(
                        artifact_ref=f"cny_document:{item.get('document_id')}",
                        artifact_kind=str(item.get("document_type") or "unknown_business_document"),
                        source_domain="cny_documents",
                        source_table="sheet_vitrina_v1_cny_documents",
                        linked_shipment_id=str(item.get("source_order_id") or item.get("context_order_id") or ""),
                        linked_document_id=str(item.get("document_id") or ""),
                        filename=str(item.get("original_filename") or "cny-document.pdf"),
                        content_type=str(item.get("file_content_type") or "application/pdf"),
                        stored_path=str(item.get("stored_file_path") or ""),
                        uploaded_at=str(item.get("uploaded_at") or ""),
                        updated_at=str(item.get("updated_at") or ""),
                        status=str(item.get("status") or ""),
                        parse_status=str(item.get("status") or ""),
                        parsed_payload=parsed,
                    )
                )
        if artifact_kind:
            rows = [row for row in rows if str(row.get("artifact_kind") or "") == artifact_kind]
        rows.sort(key=lambda item: str(item.get("updated_at") or item.get("uploaded_at") or ""), reverse=True)
        return rows

    def _resolve_artifact_ref(self, conn: sqlite3.Connection, artifact_ref: str) -> dict[str, Any] | None:
        parts = artifact_ref.split(":")
        rows = self._artifact_rows(conn, shipment_id=None, supplier_order_id=None, artifact_kind=None, source_domain=None)
        for row in rows:
            if row.get("artifact_ref") == artifact_ref:
                return row
        if len(parts) >= 2:
            return None
        return None

    def _resolve_artifact_file_path(self, artifact: Mapping[str, Any]) -> dict[str, Any]:
        stored_path = str(artifact.get("_stored_path") or "").strip()
        if not stored_path:
            return {"status": "file_missing", "reason": "artifact has no stored runtime file"}
        root = self.runtime_dir.resolve()
        raw_path = Path(stored_path)
        path = raw_path.expanduser().resolve() if raw_path.is_absolute() else (root / raw_path).resolve()
        if root != path and root not in path.parents:
            return {"status": "path_outside_runtime_root", "reason": "stored artifact path is outside WebCore runtime root"}
        if not path.exists() or not path.is_file():
            return {"status": "file_missing", "reason": "stored artifact file does not exist"}
        return {"status": "ok", "path": path}

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise WebCoreDataMcpError(f"runtime DB does not exist: {self.db_path}", code="runtime_db_missing")
        deadline_at = getattr(self._call_context, "deadline_at", None)
        cancel_event = getattr(self._call_context, "cancel_event", None)
        remaining = max(0.05, float(deadline_at - time.monotonic())) if deadline_at is not None else 5.0
        quoted_path = quote(str(self.db_path.resolve()), safe="/:")
        conn = sqlite3.connect(f"file:{quoted_path}?mode=ro", uri=True, timeout=min(remaining, 5.0))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute(f"PRAGMA busy_timeout={max(1, int(min(remaining, 5.0) * 1000))}")
        if deadline_at is not None or cancel_event is not None:
            def should_interrupt() -> int:
                if cancel_event is not None and cancel_event.is_set():
                    return 1
                if deadline_at is not None and time.monotonic() >= deadline_at:
                    return 1
                return 0

            conn.set_progress_handler(should_interrupt, 1000)
        return conn

    def _audit(self, payload: Mapping[str, Any]) -> None:
        serialized = json.dumps(dict(payload), ensure_ascii=True, sort_keys=True)
        with self._audit_lock:
            if self.audit_log_path is not None:
                self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.audit_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(serialized + "\n")
            if self.emit_audit_to_stdout:
                print(serialized, flush=True)

    def _db_file_meta(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {"exists": False}
        stat = self.db_path.stat()
        return {
            "exists": True,
            "path_label": self.db_path.name,
            "size_bytes": stat.st_size,
            "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }

    def _temporal_slot_sources(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        if not _table_exists(conn, "temporal_source_slot_snapshots"):
            return []
        rows = conn.execute(
            """
            SELECT source_key, COUNT(*) AS row_count, MIN(snapshot_date) AS min_snapshot_date,
                   MAX(snapshot_date) AS max_snapshot_date, MAX(captured_at) AS max_captured_at
            FROM temporal_source_slot_snapshots
            GROUP BY source_key
            ORDER BY source_key
            """
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def _temporal_sources(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        if not _table_exists(conn, "temporal_source_snapshots"):
            return []
        rows = conn.execute(
            """
            SELECT source_key, COUNT(*) AS row_count, MIN(snapshot_date) AS min_snapshot_date,
                   MAX(snapshot_date) AS max_snapshot_date, MAX(captured_at) AS max_captured_at
            FROM temporal_source_snapshots
            GROUP BY source_key
            ORDER BY source_key
            """
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def _wb_supplies_freshness(self, conn: sqlite3.Connection) -> dict[str, Any]:
        if not _table_exists(conn, "sheet_vitrina_v1_wb_supplies"):
            return {"status": "missing_table"}
        base = _single_count_min_max(conn, "sheet_vitrina_v1_wb_supplies", "supply_date", extra_max_column="synced_at")
        if _table_exists(conn, "sheet_vitrina_v1_wb_supplies_sync_runs"):
            run = conn.execute(
                """
                SELECT run_id, mode, status, started_at, updated_at, completed_at, raw_fetched, upserted, last_error
                FROM sheet_vitrina_v1_wb_supplies_sync_runs
                ORDER BY started_at DESC
                LIMIT 1
                """
            ).fetchone()
            base["latest_sync_run"] = _row_dict(run) if run else None
        return base

    def _supplier_freshness(self, conn: sqlite3.Connection) -> dict[str, Any]:
        return {
            "shipments": _single_count_min_max(
                conn,
                "sheet_vitrina_v1_supplier_shipments",
                "shipment_date",
                extra_max_column="updated_at",
            ),
            "financial_documents": _single_count_min_max(
                conn,
                "sheet_vitrina_v1_supplier_financial_documents",
                "document_date",
                extra_max_column="uploaded_at",
            ),
        }

    def _factory_order_freshness(self, conn: sqlite3.Connection) -> dict[str, Any]:
        return {
            "datasets": _single_count_min_max(
                conn,
                "sheet_vitrina_v1_factory_order_dataset_state",
                "uploaded_at",
            ),
            "factory_result": _single_count_min_max(
                conn,
                "sheet_vitrina_v1_factory_order_result_state",
                "calculated_at",
            ),
            "wb_regional_result": _single_count_min_max(
                conn,
                "sheet_vitrina_v1_wb_regional_supply_result_state",
                "calculated_at",
            ),
        }

    def _source_freshness(self, conn: sqlite3.Connection, source_key: str) -> dict[str, Any]:
        if not source_key or not _table_exists(conn, "temporal_source_slot_snapshots"):
            return {}
        row = conn.execute(
            """
            SELECT source_key, COUNT(*) AS row_count, MIN(snapshot_date) AS min_snapshot_date,
                   MAX(snapshot_date) AS max_snapshot_date, MAX(captured_at) AS max_captured_at
            FROM temporal_source_slot_snapshots
            WHERE source_key = ?
            GROUP BY source_key
            """,
            (source_key,),
        ).fetchone()
        return _row_dict(row) if row else {}

    def _search_skus(self, conn: sqlite3.Connection, like: str, *, limit: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT 'sku' AS object_type, c.nm_id AS id, c.display_name AS title, c.group_name,
                   c.enabled, c.display_order
            FROM registry_upload_config_v2 c
            WHERE CAST(c.nm_id AS TEXT) LIKE ? OR c.display_name LIKE ? OR c.group_name LIKE ?
            ORDER BY c.enabled DESC, c.display_order, c.nm_id
            LIMIT ?
            """,
            (like, like, like, limit),
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def _search_nomenclature(self, conn: sqlite3.Connection, like: str, *, limit: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT 'nomenclature' AS object_type, item_id AS id, nomenclature_name AS title,
                   our_sku, nm_id, product_type, is_active
            FROM sheet_vitrina_v1_nomenclature_items
            WHERE CAST(nm_id AS TEXT) LIKE ? OR our_sku LIKE ? OR nomenclature_name LIKE ? OR product_type LIKE ?
            ORDER BY is_active DESC, nomenclature_name
            LIMIT ?
            """,
            (like, like, like, like, limit),
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def _search_shipments(self, conn: sqlite3.Connection, like: str, *, limit: int) -> list[dict[str, Any]]:
        historical_exception_select = (
            "historical_status_exception"
            if "historical_status_exception"
            in _table_columns(conn, "sheet_vitrina_v1_supplier_shipments")
            else "'' AS historical_status_exception"
        )
        rows = conn.execute(
            f"""
            SELECT 'shipment' AS object_type, shipment_id AS id, shipment_id AS title,
                   shipment_date, actual_shipment_date, actual_ff_acceptance_date,
                   {historical_exception_select},
                   order_status, invoice_no, supplier_name, match_status
            FROM sheet_vitrina_v1_supplier_shipments
            WHERE shipment_id LIKE ? OR invoice_no LIKE ? OR supplier_name LIKE ?
            ORDER BY shipment_date DESC, updated_at DESC
            LIMIT ?
            """,
            (like, like, like, limit),
        ).fetchall()
        return [apply_derived_supplier_status(_row_dict(row)) for row in rows]

    def _search_wb_supplies(self, conn: sqlite3.Connection, like: str, *, limit: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT 'wb_supply' AS object_type, supply_id AS id, COALESCE(wb_supply_id, supply_id) AS title,
                   wb_supply_id, preorder_id, status_id, supply_date, synced_at
            FROM sheet_vitrina_v1_wb_supplies
            WHERE supply_id LIKE ? OR wb_supply_id LIKE ? OR preorder_id LIKE ?
            ORDER BY COALESCE(supply_date, fact_date, updated_date, synced_at, '') DESC
            LIMIT ?
            """,
            (like, like, like, limit),
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def _search_metrics(self, conn: sqlite3.Connection, like: str, *, limit: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT 'metric' AS object_type, metric_key AS id, label_ru AS title,
                   scope, calc_type, calc_ref, section_name, enabled
            FROM registry_upload_metrics_v2
            WHERE metric_key LIKE ? OR label_ru LIKE ? OR calc_ref LIKE ? OR section_name LIKE ?
            ORDER BY enabled DESC, display_order, metric_key
            LIMIT ?
            """,
            (like, like, like, like, limit),
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def _metric_row(self, conn: sqlite3.Connection, metric_key: str) -> dict[str, Any] | None:
        if not _table_exists(conn, "registry_upload_metrics_v2"):
            return None
        row = conn.execute(
            """
            SELECT metric_key, enabled, scope, label_ru, calc_type, calc_ref, show_in_data,
                   format_name, display_order, section_name
            FROM registry_upload_metrics_v2
            WHERE metric_key = ?
            ORDER BY enabled DESC
            LIMIT 1
            """,
            (metric_key,),
        ).fetchone()
        return _row_dict(row) if row else None

    def _metric_candidates(self, conn: sqlite3.Connection, text: str, *, limit: int) -> list[dict[str, Any]]:
        if not _table_exists(conn, "registry_upload_metrics_v2"):
            return []
        like = f"%{text}%"
        rows = conn.execute(
            """
            SELECT metric_key, label_ru, scope, calc_type, calc_ref, section_name
            FROM registry_upload_metrics_v2
            WHERE metric_key LIKE ? OR label_ru LIKE ? OR calc_ref LIKE ?
            ORDER BY enabled DESC, display_order, metric_key
            LIMIT ?
            """,
            (like, like, like, limit),
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def _formula_row(self, conn: sqlite3.Connection, formula_id: str) -> dict[str, Any] | None:
        if not formula_id or not _table_exists(conn, "registry_upload_formulas_v2"):
            return None
        row = conn.execute(
            """
            SELECT formula_id, expression, description
            FROM registry_upload_formulas_v2
            WHERE formula_id = ?
            LIMIT 1
            """,
            (formula_id,),
        ).fetchone()
        return _row_dict(row) if row else None

    def _latest_result_summary(self, conn: sqlite3.Connection, table_name: str) -> dict[str, Any]:
        if not _table_exists(conn, table_name):
            return {"status": "missing_table"}
        row = conn.execute(
            f"""
            SELECT calculated_at, result_json
            FROM {table_name}
            WHERE slot = 1
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return {"status": "not_found"}
        return {
            "status": "ok",
            "calculated_at": row["calculated_at"],
            "result_summary": _compact_json_summary(_safe_json_loads(row["result_json"])),
        }

    def _supplier_shipment_row(self, conn: sqlite3.Connection, shipment_id: str) -> dict[str, Any] | None:
        if not _table_exists(conn, "sheet_vitrina_v1_supplier_shipments"):
            return None
        historical_exception_select = (
            "historical_status_exception"
            if "historical_status_exception"
            in _table_columns(conn, "sheet_vitrina_v1_supplier_shipments")
            else "'' AS historical_status_exception"
        )
        row = conn.execute(
            f"""
            SELECT shipment_id, created_at, updated_at, shipment_date, actual_shipment_date,
                   actual_ff_acceptance_date, {historical_exception_select},
                   order_status, invoice_no, invoice_date, contract_no,
                   contract_date, supplier_name, customer_name, currency, product_qty_total,
                   product_amount_total, extras_amount_total, invoice_amount_total,
                   declared_invoice_total, match_status, parser_version, warnings_json, errors_json
            FROM sheet_vitrina_v1_supplier_shipments
            WHERE shipment_id = ?
            LIMIT 1
            """,
            (shipment_id,),
        ).fetchone()
        if row is None:
            return None
        item = apply_derived_supplier_status(_row_dict(row))
        item["warnings"] = _bounded_list(_safe_json_loads(item.pop("warnings_json", None)), limit=20)
        item["errors"] = _bounded_list(_safe_json_loads(item.pop("errors_json", None)), limit=20)
        return item

    def _supplier_line_summary(self, conn: sqlite3.Connection, shipment_id: str) -> dict[str, Any]:
        if not _table_exists(conn, "sheet_vitrina_v1_supplier_shipment_lines"):
            return {"status": "missing_table"}
        row = conn.execute(
            """
            SELECT COUNT(*) AS line_count,
                   COUNT(DISTINCT internal_nm_id) AS matched_nm_id_count,
                   SUM(CASE WHEN qty IS NULL THEN 0 ELSE qty END) AS qty_total,
                   SUM(CASE WHEN amount IS NULL THEN 0 ELSE amount END) AS amount_total
            FROM sheet_vitrina_v1_supplier_shipment_lines
            WHERE shipment_id = ?
            """,
            (shipment_id,),
        ).fetchone()
        statuses = conn.execute(
            """
            SELECT COALESCE(match_status, 'unknown') AS match_status, COUNT(*) AS count
            FROM sheet_vitrina_v1_supplier_shipment_lines
            WHERE shipment_id = ?
            GROUP BY COALESCE(match_status, 'unknown')
            ORDER BY count DESC, match_status
            """,
            (shipment_id,),
        ).fetchall()
        return {**_row_dict(row), "match_status_counts": [_row_dict(item) for item in statuses]}

    def _supplier_financial_doc_summary(self, conn: sqlite3.Connection, shipment_id: str) -> list[dict[str, Any]]:
        if not _table_exists(conn, "sheet_vitrina_v1_supplier_financial_documents"):
            return []
        rows = conn.execute(
            """
            SELECT document_type, parse_status, COUNT(*) AS count,
                   SUM(CASE WHEN total_amount_rub IS NULL THEN 0 ELSE total_amount_rub END) AS total_amount_rub,
                   MAX(uploaded_at) AS latest_uploaded_at
            FROM sheet_vitrina_v1_supplier_financial_documents
            WHERE supplier_order_id = ?
            GROUP BY document_type, parse_status
            ORDER BY document_type, parse_status
            """,
            (shipment_id,),
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def _supplier_expense_summary(self, conn: sqlite3.Connection, shipment_id: str) -> list[dict[str, Any]]:
        if not _table_exists(conn, "sheet_vitrina_v1_supplier_financial_expense_lines"):
            return []
        rows = conn.execute(
            """
            SELECT category, stage, COUNT(*) AS count,
                   SUM(CASE WHEN amount_rub IS NULL THEN 0 ELSE amount_rub END) AS amount_rub
            FROM sheet_vitrina_v1_supplier_financial_expense_lines
            WHERE supplier_order_id = ?
            GROUP BY category, stage
            ORDER BY category, stage
            """,
            (shipment_id,),
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def _supplier_trade_doc_summary(self, conn: sqlite3.Connection, shipment_id: str) -> list[dict[str, Any]]:
        if not _table_exists(conn, "sheet_vitrina_v1_trade_documents"):
            return []
        rows = conn.execute(
            """
            SELECT document_type, status, COUNT(*) AS count, MAX(updated_at) AS latest_updated_at
            FROM sheet_vitrina_v1_trade_documents
            WHERE source_shipment_id = ?
            GROUP BY document_type, status
            ORDER BY document_type, status
            """,
            (shipment_id,),
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def _ready_snapshot(self, conn: sqlite3.Connection, date_value: str | None) -> dict[str, Any] | None:
        if not _table_exists(conn, "sheet_vitrina_v1_ready_snapshots"):
            return None
        if date_value:
            row = conn.execute(
                """
                SELECT as_of_date, snapshot_id, refreshed_at, plan_json
                FROM sheet_vitrina_v1_ready_snapshots
                WHERE as_of_date = ?
                ORDER BY refreshed_at DESC
                LIMIT 1
                """,
                (date_value,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT as_of_date, snapshot_id, refreshed_at, plan_json
                FROM sheet_vitrina_v1_ready_snapshots
                ORDER BY as_of_date DESC, refreshed_at DESC
                LIMIT 1
                """
            ).fetchone()
        return _row_dict(row) if row else None

    def _sku_identity(self, conn: sqlite3.Connection, sku_or_nm_id: str) -> dict[str, Any]:
        identity: dict[str, Any] = {}
        if _table_exists(conn, "registry_upload_config_v2"):
            row = conn.execute(
                """
                SELECT nm_id, enabled, display_name, group_name, display_order
                FROM registry_upload_config_v2
                WHERE CAST(nm_id AS TEXT) = ? OR display_name = ?
                ORDER BY enabled DESC
                LIMIT 1
                """,
                (sku_or_nm_id, sku_or_nm_id),
            ).fetchone()
            if row:
                identity["registry_config"] = _row_dict(row)
        if _table_exists(conn, "sheet_vitrina_v1_nomenclature_items"):
            row = conn.execute(
                """
                SELECT item_id, is_active, our_sku, nm_id, nomenclature_name, product_type,
                       purchase_price_yuan, updated_at
                FROM sheet_vitrina_v1_nomenclature_items
                WHERE CAST(nm_id AS TEXT) = ? OR our_sku = ? OR item_id = ?
                ORDER BY is_active DESC, updated_at DESC
                LIMIT 1
                """,
                (sku_or_nm_id, sku_or_nm_id, sku_or_nm_id),
            ).fetchone()
            if row:
                identity["nomenclature"] = _row_dict(row)
        return identity

    def _metric_catalog(self, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        if not _table_exists(conn, "registry_upload_metrics_v2"):
            return {}
        rows = conn.execute(
            """
            SELECT metric_key, enabled, scope, label_ru, calc_type, calc_ref, show_in_data,
                   format_name, display_order, section_name
            FROM registry_upload_metrics_v2
            ORDER BY enabled DESC, display_order, metric_key
            """
        ).fetchall()
        catalog: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = _row_dict(row)
            metric_key = str(item.get("metric_key") or "")
            if metric_key and metric_key not in catalog:
                catalog[metric_key] = item
        return catalog

    def _sku_catalog(self, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        if not _table_exists(conn, "registry_upload_config_v2"):
            return {}
        rows = conn.execute(
            """
            SELECT nm_id, enabled, display_name, group_name, display_order
            FROM registry_upload_config_v2
            ORDER BY enabled DESC, display_order, nm_id
            """
        ).fetchall()
        catalog: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = _row_dict(row)
            nm_id = str(item.get("nm_id") or "")
            if nm_id and nm_id not in catalog:
                catalog[nm_id] = item
        return catalog

    def _resolve_metric_selector(
        self,
        conn: sqlite3.Connection,
        metric_key_or_label: str,
    ) -> tuple[set[str], str | None, list[dict[str, Any]]]:
        selector = metric_key_or_label.strip()
        if not selector:
            return set(), None, []
        registry = self._metric_catalog(conn)
        latest_snapshot = self._ready_snapshot(conn, None)
        snapshot_catalog = (
            _snapshot_metric_catalog(_safe_json_loads(latest_snapshot.get("plan_json")))
            if latest_snapshot
            else {}
        )
        candidates = _metric_selector_candidates(selector, registry, snapshot_catalog)
        exact_keys = {
            key
            for key in (registry.keys() | snapshot_catalog.keys())
            if key == selector
        }
        if exact_keys:
            return exact_keys, None, candidates
        label_exact_keys = {
            str(item.get("metric_key") or "")
            for item in candidates
            if str(item.get("label_ru") or "").casefold() == selector.casefold()
        }
        label_exact_keys.discard("")
        if len(label_exact_keys) == 1:
            return label_exact_keys, None, candidates
        return set(), selector, candidates

    def _latest_metric_default_date(self, conn: sqlite3.Connection) -> str:
        snapshot = self._ready_snapshot(conn, None)
        if not snapshot:
            return _utc_now()[:10]
        return str(snapshot.get("as_of_date") or _utc_now()[:10])

    def _ready_snapshots_covering_range(
        self,
        conn: sqlite3.Connection,
        *,
        date_from: str,
        date_to: str,
    ) -> list[dict[str, Any]]:
        if not _table_exists(conn, "sheet_vitrina_v1_ready_snapshots"):
            return []
        snapshot_from = _date_shift(date_from, -1)
        rows = conn.execute(
            """
            SELECT as_of_date, snapshot_id, refreshed_at, plan_json
            FROM sheet_vitrina_v1_ready_snapshots
            WHERE as_of_date >= ? AND as_of_date <= ?
            ORDER BY as_of_date ASC, refreshed_at ASC
            """,
            (snapshot_from, date_to),
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def _metric_rows_for_range(
        self,
        conn: sqlite3.Connection,
        *,
        date_from: str,
        date_to: str,
        metric_keys: set[str],
        metric_query: str | None,
        sku_or_nm_id: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        metric_meta = self._metric_catalog(conn)
        sku_meta = self._sku_catalog(conn)
        source_snapshots: list[dict[str, Any]] = []
        collected: list[dict[str, Any]] = []
        for snapshot in self._ready_snapshots_covering_range(conn, date_from=date_from, date_to=date_to):
            source_snapshots.append(_snapshot_meta(snapshot))
            collected.extend(
                _extract_ready_snapshot_metric_rows(
                    _safe_json_loads(snapshot.get("plan_json")),
                    metric_keys=metric_keys,
                    metric_query=metric_query,
                    sku_or_nm_id=sku_or_nm_id,
                    date_from=date_from,
                    date_to=date_to,
                    metric_meta=metric_meta,
                    sku_meta=sku_meta,
                    snapshot=snapshot,
                    limit=limit + 1,
                )
            )
        deduped = _dedupe_metric_rows(collected)
        deduped.sort(
            key=lambda item: (
                str(item.get("date") or ""),
                str(item.get("level") or ""),
                str(item.get("sku_or_nm_id") or ""),
                str(item.get("metric_key") or ""),
            )
        )
        truncated = len(deduped) > limit
        return deduped[:limit], source_snapshots, truncated

    def _metric_values_for_date(
        self,
        conn: sqlite3.Connection,
        date_value: str,
        metric_key: str,
        sku_or_nm_id: str | None,
    ) -> list[dict[str, Any]]:
        metric_keys, metric_query, _ = self._resolve_metric_selector(conn, metric_key)
        values, _, _ = self._metric_rows_for_range(
            conn,
            date_from=date_value,
            date_to=date_value,
            metric_keys=metric_keys,
            metric_query=metric_query,
            sku_or_nm_id=sku_or_nm_id,
            limit=100,
        )
        return values[:100]

    def _ambiguous_revenue_metric(self, *, date_from: str, date_to: str) -> dict[str, Any]:
        with self._connect() as conn:
            candidates = self._revenue_candidates(conn)
            ready = _single_count_min_max(conn, "sheet_vitrina_v1_ready_snapshots", "as_of_date", extra_max_column="refreshed_at")
            fin = self._source_freshness(conn, "fin_report_daily")
        return {
            "status": "ambiguous_revenue_metric",
            "date_from": date_from,
            "date_to": date_to,
            "candidate_metrics": candidates,
            "available_freshness": {"ready_snapshots": ready, "fin_report_daily": fin},
            "message": "Revenue is not canonical in the MCP MVP. Pass revenue_metric explicitly after choosing the business definition.",
        }

    def _revenue_candidates(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        if not _table_exists(conn, "registry_upload_metrics_v2"):
            return []
        predicates = " OR ".join(["LOWER(metric_key) LIKE ? OR LOWER(label_ru) LIKE ? OR LOWER(calc_ref) LIKE ?" for _ in REVENUE_CANDIDATE_MARKERS])
        params: list[Any] = []
        for marker in REVENUE_CANDIDATE_MARKERS:
            like = f"%{marker.lower()}%"
            params.extend([like, like, like])
        rows = conn.execute(
            f"""
            SELECT metric_key, label_ru, scope, calc_type, calc_ref, section_name
            FROM registry_upload_metrics_v2
            WHERE {predicates}
            ORDER BY enabled DESC, display_order, metric_key
            LIMIT 20
            """,
            params,
        ).fetchall()
        return [_row_dict(row) for row in rows]


def _tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            "freshness",
            "WebCore data freshness",
            "Use this when the user asks whether WebCore business data is fresh or which persisted sources are current. It never triggers refresh or sync.",
            _schema({}),
            _strict_output_schema("status", "source", "db", "ready_snapshots", "temporal_slot_sources", "temporal_sources", "wb_supplies", "supplier_shipments", "factory_order", "notes"),
        ),
        ToolDefinition(
            "metric_catalog",
            "Metric catalog",
            "Use this only to discover metric keys or Russian labels when the requested metric is not already known. Returns bounded persisted metric metadata, not values.",
            _schema({
                "query": _documented(_string_schema(0, 160), "Optional metric key or Russian-label search text."),
                "section": _documented(_string_schema(0, 120), "Optional exact section filter."),
                "scope": _documented(_string_schema(0, 60), "Optional metric scope filter."),
                "limit": _documented(_int_schema(1, MAX_LIMIT), "Maximum rows.", default=DEFAULT_MAX_LIMIT),
            }),
            _strict_output_schema("status", "query", "section", "scope", "limit", "rows", "truncated", "latest_ready_snapshot", "source_tables"),
        ),
        ToolDefinition(
            "metric_values",
            "Metric values",
            "Use this for a metric value by key or Russian label for one date or a bounded date range, optionally for one SKU. Reads persisted ready snapshots only.",
            _schema({
                "metric_key_or_label": _documented(_string_schema(1, 180), "Exact metric key or an unambiguous Russian label."),
                "date": _documented(_date_schema(), "Single snapshot date; do not combine with date_from/date_to."),
                "date_from": _documented(_date_schema(), "Inclusive range start, paired with date_to."),
                "date_to": _documented(_date_schema(), "Inclusive range end, paired with date_from; maximum 62 days."),
                "sku_or_nm_id": _documented(_string_schema(0, 120), "Optional SKU, vendor code or nmId filter."),
                "group_by": _documented(_enum_schema(["date", "metric", "sku", "total"]), "Optional result grouping."),
                "limit": _documented(_int_schema(1, MAX_LIMIT), "Maximum value rows.", default=DEFAULT_MAX_LIMIT),
            }, required=["metric_key_or_label"]),
            _strict_output_schema("status", "metric_key_or_label", "resolved_metric_keys", "candidate_metrics", "date_from", "date_to", "sku_or_nm_id", "row_count", "limit", "truncated", "rows", "source_snapshots", "source_table", "caveat"),
        ),
        ToolDefinition(
            "sku_search",
            "Search SKU",
            "Use this to find a SKU, nmId, vendor code or nomenclature identity from a bounded text query. For a known SKU's metrics use sku_snapshot instead.",
            _schema({
                "query": _documented(_string_schema(1, 120), "SKU name, vendor code or nmId."),
                "object_types": _documented(_array_schema(_enum_schema(["sku", "nomenclature"])), "Optional SKU identity kinds; defaults to both sku and nomenclature."),
            }, required=["query"]),
            _strict_output_schema("status", "query", "object_types", "unknown_object_types", "limit_applied", "results"),
        ),
        ToolDefinition(
            "sku_snapshot",
            "SKU snapshot",
            "Use this for one known SKU or nmId with its persisted metrics and freshness flags on an optional date. It does not search broad text.",
            _schema({
                "sku_or_nm_id": _documented(_string_schema(1, 120), "Known SKU, vendor code or nmId."),
                "date": _documented(_date_schema(), "Optional snapshot date; defaults to the latest available snapshot."),
            }, required=["sku_or_nm_id"]),
            _strict_output_schema("status", "sku_or_nm_id", "date", "snapshot_id", "refreshed_at", "identity", "metrics", "missing_source_flags"),
        ),
        ToolDefinition(
            "supplier_shipments",
            "Supplier shipments",
            "Use this for the supplier shipment registry/list, including physical totals, finance and document completeness. For one known shipment use supplier_shipment.",
            _schema(
                {
                    "shipment_id": _documented(_string_schema(0, 120), "Optional exact shipment identifier."),
                    "invoice_no": _documented(_string_schema(0, 160), "Optional supplier invoice number."),
                    "supplier_name": _documented(_string_schema(0, 160), "Optional supplier-name text filter."),
                    "order_status": _documented(_string_schema(0, 80), "Optional order status filter."),
                    "match_status": _documented(_string_schema(0, 80), "Optional nomenclature match status."),
                    "document_status": _documented(_string_schema(0, 80), "Optional document completeness status."),
                    "date_from": _documented(_date_schema(), "Inclusive shipment-date range start."),
                    "date_to": _documented(_date_schema(), "Inclusive shipment-date range end."),
                    "sort_by": _documented(_enum_schema(["date_desc", "shipment_date_desc", "product_qty_total_desc", "invoice_amount_total_desc", "expense_amount_rub_desc"]), "Stable server-side sort.", default="date_desc"),
                    "limit": _documented(_int_schema(1, MAX_LIMIT), "Page size.", default=DEFAULT_MAX_LIMIT),
                    "cursor": _documented(_string_schema(0, 40), "Opaque next_cursor from the prior response."),
                    "offset": _documented(_int_schema(0, 100000), "Compatibility offset; prefer cursor.", default=0),
                }
            ),
            _strict_output_schema("status", "contract_name", "contract_version", "source_tables", "filters", "rows", "pagination", "source_table", "table"),
            scope=SCOPE_SUPPLY_READ,
        ),
        ToolDefinition(
            "supplier_shipment",
            "Supplier shipment detail",
            "Use this for one known supplier shipment id. Returns bounded lines, finance, CNY/document metadata and stable artifact_ref identifiers for follow-up reads.",
            _schema(
                {
                    "shipment_id": _documented(_string_schema(1, 120), "Stable shipment_id returned by supplier_shipments."),
                    "include_raw_business_payloads": _documented({"type": "boolean"}, "Include scrubbed bounded business payload fields.", default=False),
                    "line_limit": _documented(_int_schema(1, MAX_LIMIT), "Maximum product lines.", default=25),
                    "document_limit": _documented(_int_schema(1, MAX_LIMIT), "Maximum document rows.", default=25),
                },
                required=["shipment_id"],
            ),
            _strict_output_schema("status", "shipment_id", "contract_name", "contract_version", "shipment", "lines", "line_summary", "financial_documents", "financial_documents_metadata", "financial_expense_lines", "expense_summary", "trade_documents", "trade_documents_metadata", "cny_documents_metadata", "packing_list_summary", "document_parsed_fields_summary", "artifact_refs", "source_tables", "redaction", "source_table", "table"),
            scope=SCOPE_SUPPLY_READ,
        ),
        ToolDefinition(
            "wb_supplies",
            "WB supplies",
            "Use this for the cached read-only WB FBW supply list. It never syncs, backfills or calls WB upstream; for one known supply use wb_supply.",
            _schema(
                {
                    "status_filter": _documented(_string_schema(0, 80), "Optional status id or status-name filter."),
                    "warehouse": _documented(_string_schema(0, 160), "Optional warehouse-name filter."),
                    "supply_id": _documented(_string_schema(0, 120), "Optional internal supply id."),
                    "wb_supply_id": _documented(_string_schema(0, 120), "Optional WB supply id."),
                    "preorder_id": _documented(_string_schema(0, 120), "Optional preorder id."),
                    "date_from": _documented(_date_schema(), "Inclusive supply-date range start."),
                    "date_to": _documented(_date_schema(), "Inclusive supply-date range end."),
                    "limit": _documented(_int_schema(1, MAX_LIMIT), "Page size.", default=DEFAULT_MAX_LIMIT),
                    "cursor": _documented(_string_schema(0, 40), "Opaque next_cursor from the prior response."),
                    "offset": _documented(_int_schema(0, 100000), "Compatibility offset; prefer cursor.", default=0),
                }
            ),
            _strict_output_schema("status", "contract_name", "contract_version", "source_table", "table", "cache_only", "filters", "rows", "pagination"),
            scope=SCOPE_SUPPLY_READ,
        ),
        ToolDefinition(
            "wb_supply",
            "WB supply detail",
            "Use this for one known cached WB supply id. Returns normalized/detail/goods/package evidence without any upstream fetch.",
            _schema({
                "supply_id": _documented(_string_schema(1, 120), "Internal supply_id, WB supply id or preorder id returned by wb_supplies."),
                "include_raw_business_payloads": _documented({"type": "boolean"}, "Include scrubbed bounded cached business payload fields.", default=False),
            }, required=["supply_id"]),
            _strict_output_schema("status", "supply_id", "contract_name", "contract_version", "source_table", "table", "cache_only", "no_upstream_fetch", "supply", "cached_payloads", "redaction"),
            scope=SCOPE_SUPPLY_READ,
        ),
        ToolDefinition(
            "supply_artifacts",
            "Supply documents",
            "Use this to find server-owned supplier, trade, finance or CNY documents and obtain stable opaque artifact_ref identifiers. No filesystem paths are exposed.",
            _schema(
                {
                    "shipment_id": _documented(_string_schema(0, 120), "Optional shipment id."),
                    "supplier_order_id": _documented(_string_schema(0, 120), "Compatibility alias for shipment id."),
                    "artifact_kind": _documented(_string_schema(0, 80), "Optional kind such as invoice, packing_list or contract."),
                    "source_domain": _documented(_string_schema(0, 80), "Optional allowlisted artifact source domain."),
                    "limit": _documented(_int_schema(1, MAX_LIMIT), "Page size.", default=DEFAULT_MAX_LIMIT),
                    "cursor": _documented(_string_schema(0, 40), "Opaque next_cursor from the prior response."),
                    "offset": _documented(_int_schema(0, 100000), "Compatibility offset; prefer cursor.", default=0),
                }
            ),
            _strict_output_schema("status", "contract_name", "contract_version", "source", "filters", "artifacts", "pagination", "boundary"),
            scope=SCOPE_SUPPLY_READ,
        ),
        ToolDefinition(
            "supply_artifact",
            "Supply document content",
            "Use this after supply_artifacts with one opaque artifact_ref. Reads metadata, parsed data or one bounded text/base64 chunk; never accepts server paths.",
            _schema(
                {
                    "artifact_ref": _documented(_string_schema(1, 240), "Opaque artifact_ref returned by supply_artifacts or supplier_shipment."),
                    "mode": _documented(_enum_schema(["metadata", "parsed", "text", "text_chunk", "base64_chunk"]), "Bounded read mode.", default="metadata"),
                    "chunk": _documented(_int_schema(0, 100000), "Zero-based chunk number.", default=0),
                    "offset": _documented(_int_schema(0, 100000000), "Byte offset for chunk modes.", default=0),
                    "max_bytes": _documented(_int_schema(1, 65536), "Maximum bytes returned in this call.", default=16384),
                },
                required=["artifact_ref"],
            ),
            _strict_output_schema("status", "artifact_ref", "contract_name", "mode", "artifact", "parsed_business_payload", "packing_list_summary", "chunk", "text", "base64", "reason", "size_bytes", "max_bytes"),
            scope=SCOPE_SUPPLY_READ,
        ),
        ToolDefinition(
            "factory_order",
            "Factory order state",
            "Use this for the latest persisted factory-order and WB regional calculation state. It never recalculates or writes data.",
            _schema({}),
            _strict_output_schema("status", "dataset_state", "factory_order_result", "wb_regional_supply_result", "no_recalculation"),
            scope=SCOPE_SUPPLY_READ,
        ),
        ToolDefinition(
            "stock_report",
            "Stock report",
            "Use this for persisted ready-side stock metrics on an optional date and SKU. It does not refresh data; use sku_snapshot for broader SKU metrics.",
            _schema({
                "date": _documented(_date_schema(), "Optional snapshot date; defaults to latest available."),
                "sku_or_nm_id": _documented(_string_schema(0, 120), "Optional SKU, vendor code or nmId."),
            }),
            _strict_output_schema("status", "date", "sku_or_nm_id", "snapshot_id", "refreshed_at", "metrics", "stocks_freshness", "source", "caveat"),
        ),
        ToolDefinition(
            "runtime_health",
            "Runtime health",
            "Use this for bounded production health of fixed WebCore units, storage, database and aggregated MCP call status including timeouts and in-flight calls.",
            _schema({}),
            _strict_output_schema("status", "generated_at", "boundary", "allowed_units", "units", "runtime_storage", "mcp_calls", "limits"),
            scope=SCOPE_OPS_READ,
        ),
        ToolDefinition(
            "refresh_diagnostics",
            "Refresh diagnostics",
            "Use this for persisted refresh/load diagnostics on one date or a bounded date range. It never starts refresh, load or upstream calls.",
            _schema({
                "date": _documented(_date_schema(), "Single diagnostic date; do not combine with date_from/date_to."),
                "date_from": _documented(_date_schema(), "Inclusive range start."),
                "date_to": _documented(_date_schema(), "Inclusive range end; maximum 62 days."),
            }),
            _strict_output_schema("status", "generated_at", "date_range", "latest_refresh", "latest_load", "snapshot_presence", "source_statuses", "closure_states", "likely_failure_area", "latest_successful", "raw_payloads_returned", "upstream_calls", "mutations"),
            scope=SCOPE_OPS_READ,
        ),
        ToolDefinition(
            "deploy_state",
            "Deploy state",
            "Use this to verify the active production target and deployed commit. Returns safe version labels only, without env, credentials, SSH commands or runtime paths.",
            _schema({}),
            _strict_output_schema("status", "generated_at", "app", "target_identity", "source_mtimes", "runtime_storage", "credential_values_returned", "raw_env_returned"),
            scope=SCOPE_OPS_READ,
        ),
    ]


def tool_required_scope(name: str) -> str:
    canonical_name = MODEL_TOOL_ALIASES.get(name, name)
    for definition in _tool_definitions():
        if MODEL_TOOL_ALIASES.get(definition.name, definition.name) == canonical_name:
            return definition.scope
    if canonical_name in OPS_TOOL_NAMES:
        return SCOPE_OPS_READ
    if canonical_name in {
        "get_supplier_shipments_registry", "get_supplier_shipment_full_details",
        "get_wb_supplies_registry", "get_wb_supply_full_details", "list_supply_artifacts",
        "get_supply_artifact", "rank_supplier_shipments_by_unit_cost",
        "get_supplier_shipment_details", "get_latest_factory_order_calculation",
    }:
        return SCOPE_SUPPLY_READ
    if canonical_name in {"get_revenue_by_date", "get_revenue_range"}:
        return SCOPE_FINANCE_READ
    return SCOPE_ANALYTICS_READ


def _model_tool_domain(name: str) -> str:
    return {
        "freshness": "freshness",
        "metric_catalog": "metrics",
        "metric_values": "metrics",
        "sku_search": "sku",
        "sku_snapshot": "sku",
        "stock_report": "sku",
        "supplier_shipments": "supplier_shipments",
        "supplier_shipment": "supplier_shipments",
        "wb_supplies": "wb_supplies",
        "wb_supply": "wb_supplies",
        "supply_artifacts": "artifacts",
        "supply_artifact": "artifacts",
        "factory_order": "factory_order",
        "runtime_health": "ops_diagnostics",
        "refresh_diagnostics": "ops_diagnostics",
        "deploy_state": "ops_diagnostics",
    }.get(name, "all")


def _data_map_domains() -> set[str]:
    return {"all", "freshness", "metrics", "sku", "supplier_shipments", "wb_supplies", "artifacts", "cny", "factory_order", "business_tables", "ops_diagnostics"}


def _domain_catalog() -> list[dict[str, Any]]:
    return [
        {
            "domain": "freshness",
            "description": "Ready snapshots, temporal source slots, WB supplies sync, supplier docs and factory calculation freshness.",
            "primary_tools": ["freshness"],
            "required_scope": SCOPE_ANALYTICS_READ,
            "recommended_first_call": "get_data_freshness_status",
        },
        {
            "domain": "ops_diagnostics",
            "description": "Read-only production diagnostics for fixed WebCore runtime units, sanitized logs, refresh/load state, snapshot presence and deploy labels.",
            "primary_tools": [
                "runtime_health",
                "refresh_diagnostics",
                "deploy_state",
            ],
            "required_scope": SCOPE_OPS_READ,
            "known_caveat": "No arbitrary shell, SSH, filesystem browsing, SQL, env, secrets or mutations are exposed.",
        },
        {
            "domain": "metrics",
            "description": "Persisted DATA_VITRINA metrics by key/Russian label/date/SKU from ready snapshots.",
            "primary_tools": ["metric_catalog", "metric_values"],
            "required_scope": SCOPE_ANALYTICS_READ,
            "known_caveat": "Revenue remains ambiguous unless revenue_metric is explicitly selected.",
        },
        {
            "domain": "sku",
            "description": "SKU identity, registry config, server-owned nomenclature and persisted stock/SKU snapshots.",
            "primary_tools": ["sku_search", "sku_snapshot", "stock_report"],
            "required_scope": SCOPE_ANALYTICS_READ,
        },
        {
            "domain": "supplier_shipments",
            "description": "Supplier shipment registry, shipment cards, line rows, price conformity, packing-list summaries, financial docs, trade docs and CNY links.",
            "primary_tools": ["supplier_shipments", "supplier_shipment"],
            "legacy_tools": ["rank_supplier_shipments_by_unit_cost", "get_supplier_shipment_details"],
            "required_scope": SCOPE_SUPPLY_READ,
        },
        {
            "domain": "wb_supplies",
            "description": "Cached read-only WB FBW supplies registry/detail and cached normalized business payloads.",
            "primary_tools": ["wb_supplies", "wb_supply"],
            "legacy_tools": ["get_wb_supplies_summary", "get_wb_supply_details"],
            "required_scope": SCOPE_SUPPLY_READ,
            "boundary": "cache-only; no WB sync/backfill/lazy fetch.",
        },
        {
            "domain": "artifacts",
            "description": "Server-owned supplier/trade/financial/CNY artifacts resolved only through opaque artifact_ref.",
            "primary_tools": ["supply_artifacts", "supply_artifact"],
            "required_scope": SCOPE_SUPPLY_READ,
            "boundary": "no arbitrary filesystem paths; bounded metadata/parsed/text/base64 modes only.",
        },
        {
            "domain": "cny",
            "description": "CNY currency-account documents, ledger operations and supplier-order CNY payment evidence.",
            "primary_tools": ["get_supplier_shipment_full_details", "list_supply_artifacts", "get_webcore_business_table_rows"],
            "required_scope": SCOPE_SUPPLY_READ,
        },
        {
            "domain": "factory_order",
            "description": "Latest factory-order and WB regional calculation state, without recalculation.",
            "primary_tools": ["factory_order"],
            "required_scope": SCOPE_SUPPLY_READ,
        },
        {
            "domain": "business_tables",
            "description": "Allowlisted runtime business table catalog/schema/rows via generated SELECT only.",
            "primary_tools": ["list_webcore_business_tables", "get_webcore_business_table_schema", "get_webcore_business_table_rows"],
            "required_scope": SCOPE_ANALYTICS_READ,
        },
    ]


def _intent_examples() -> list[dict[str, Any]]:
    return [
        {"intent": "покажи реестр поставок", "call": {"tool": "get_supplier_shipments_registry", "arguments": {"limit": 50}}},
        {"intent": "найди самую большую поставку", "call": {"tool": "get_supplier_shipments_registry", "arguments": {"sort_by": "product_qty_total_desc", "limit": 1}}},
        {"intent": "сколько коробок по упаковочному листу", "call": {"tool": "get_supplier_shipment_full_details", "arguments": {"shipment_id": "SHIP-1"}}},
        {"intent": "найди поставку по инвойсу INV-1", "call": {"tool": "search_business_objects", "arguments": {"query": "INV-1", "object_types": ["shipment"]}}},
        {"intent": "покажи карточку поставки SHIP-1", "call": {"tool": "get_supplier_shipment_full_details", "arguments": {"shipment_id": "SHIP-1"}}},
        {"intent": "покажи документы по поставке SHIP-1", "call": {"tool": "list_supply_artifacts", "arguments": {"shipment_id": "SHIP-1"}}},
        {"intent": "открой инвойс", "call": {"tool": "list_supply_artifacts", "arguments": {"artifact_kind": "invoice"}}},
        {"intent": "открой packing list", "call": {"tool": "list_supply_artifacts", "arguments": {"artifact_kind": "packing_list"}}},
        {"intent": "покажи WB supply", "call": {"tool": "get_wb_supplies_registry", "arguments": {"limit": 50}}},
        {"intent": "покажи метрику за дату", "call": {"tool": "get_metric_values", "arguments": {"metric_key_or_label": "total_orderSum", "date": "YYYY-MM-DD"}}},
        {"intent": "найди SKU", "call": {"tool": "search_business_objects", "arguments": {"query": "nmId or name", "object_types": ["sku", "nomenclature"]}}},
        {"intent": "проверь свежесть данных", "call": {"tool": "get_data_freshness_status", "arguments": {}}},
    ]


def _boundary_rules() -> list[str]:
    return [
        "Auth-gated production access only.",
        "SQLite is opened mode=ro with PRAGMA query_only=ON.",
        "No arbitrary SQL; business table access is allowlisted/generated SELECT only.",
        "No shell, SSH, upstream sync/backfill/refresh/replay, upload, delete or mutation tools.",
        "No unauthenticated business data.",
        "No secrets, tokens, passwords, cookies, authorization headers, OAuth/session material or storage_state.",
        "No absolute server paths; artifacts use opaque artifact_ref only.",
        "Raw business payloads require explicit tool flags and are scrubbed/bounded.",
        "Artifact files must resolve through allowlisted DB rows and stay inside the WebCore runtime root.",
    ]


def _known_limitations() -> list[str]:
    return [
        "Artifact text extraction is intentionally limited; PDFs may expose parsed metadata or bounded base64 chunks, not automatic full OCR/text.",
        "WB supplies are cached-only and never trigger upstream sync/backfill/lazy fetch through MCP.",
        "Business table rows are allowlisted runtime projections, not arbitrary SQL and not a schema migration source.",
        "Revenue tools still require an explicit revenue_metric when the business definition is ambiguous.",
        "resources/list remains secondary; tools are the primary navigation layer.",
    ]


def _business_table_catalog() -> dict[str, dict[str, Any]]:
    return {
        "sheet_vitrina_v1_ready_snapshots": _table_spec("metrics", "Persisted ready snapshot envelopes.", ["snapshot_id"], ["as_of_date", "refreshed_at"], raw=["plan_json"]),
        "temporal_source_snapshots": _table_spec("freshness", "Temporal source snapshots.", ["source_key", "snapshot_date"], ["snapshot_date", "captured_at"], raw=["payload_json"]),
        "temporal_source_slot_snapshots": _table_spec("freshness", "Temporal source slot snapshots.", ["source_key", "snapshot_date", "snapshot_role"], ["snapshot_date", "captured_at"], raw=["payload_json"]),
        "registry_upload_config_v2": _table_spec("sku", "Active registry SKU configuration.", ["nm_id"], [], order="display_order ASC, nm_id ASC"),
        "registry_upload_metrics_v2": _table_spec("metrics", "Metric registry with Russian labels and calc references.", ["metric_key"], [], order="display_order ASC, metric_key ASC"),
        "registry_upload_formulas_v2": _table_spec("metrics", "Metric formula registry.", ["formula_id"], [], order="row_order ASC, formula_id ASC"),
        "sheet_vitrina_v1_wb_supplies": _table_spec("wb_supplies", "Cached WB FBW supplies rows.", ["supply_id"], ["supply_date", "fact_date", "updated_date", "synced_at"], raw=["normalized_row_json", "raw_detail_json", "raw_goods_json", "raw_package_json"], sensitive=["raw_list_hash", "raw_detail_hash", "raw_goods_hash", "raw_package_hash"]),
        "sheet_vitrina_v1_wb_supplies_sync_state": _table_spec("wb_supplies", "WB supplies sync state.", ["slot"], ["last_synced_at", "last_successful_sync_at"]),
        "sheet_vitrina_v1_wb_supplies_sync_runs": _table_spec("wb_supplies", "WB supplies sync/backfill run history.", ["run_id"], ["started_at", "updated_at", "completed_at"]),
        "sheet_vitrina_v1_wb_supplies_warehouses": _table_spec("wb_supplies", "Cached WB supplies warehouses.", ["warehouse_id"], ["updated_at"], raw=["raw_json"]),
        "sheet_vitrina_v1_wb_supply_transit_cost_enrichment": _table_spec("wb_supplies", "Supplemental Seller Portal transit-cost facts.", ["supply_id"], ["fetched_at", "created_at", "updated_at"], sensitive=["source_endpoint_path"]),
        "sheet_vitrina_v1_wb_supply_transit_cost_enrichment_runs": _table_spec("wb_supplies", "Transit-cost enrichment run status.", ["run_id"], ["started_at", "updated_at", "completed_at"]),
        "sheet_vitrina_v1_supplier_shipments": _table_spec("supplier_shipments", "Supplier shipment/order headers.", ["shipment_id"], ["shipment_date", "invoice_date", "created_at", "updated_at"], sensitive=["source_file_path", "source_file_sha256"], raw=["warnings_json", "errors_json"]),
        "sheet_vitrina_v1_supplier_shipment_lines": _table_spec("supplier_shipments", "Supplier shipment product/extra lines.", ["line_id"], [], raw=["raw_json"], order="sort_order ASC, line_id ASC"),
        "sheet_vitrina_v1_supplier_shipment_uploads": _table_spec("supplier_shipments", "Supplier invoice staged upload metadata.", ["upload_id"], ["created_at"], sensitive=["source_file_path", "source_file_sha256"], raw=["parsed_payload_json"]),
        "sheet_vitrina_v1_supplier_financial_documents": _table_spec("supplier_shipments", "Supplier-order financial document metadata.", ["document_id"], ["document_date", "uploaded_at", "updated_at"], sensitive=["stored_file_path", "file_sha256"], raw=["raw_parse_json", "normalized_parse_json", "warnings_json", "errors_json"]),
        "sheet_vitrina_v1_supplier_financial_expense_lines": _table_spec("supplier_shipments", "Normalized supplier financial expense lines.", ["line_id"], [], raw=["raw_json"], order="financial_document_id ASC, sort_order ASC, line_id ASC"),
        "sheet_vitrina_v1_trade_documents": _table_spec("artifacts", "Invoice/contract document registry.", ["document_id"], ["document_date", "created_at", "updated_at"], sensitive=["file_path", "file_sha256"], raw=["parsed_metadata_json", "warnings_json", "errors_json"]),
        "sheet_vitrina_v1_invoice_contract_links": _table_spec("artifacts", "Invoice-to-contract links.", ["invoice_document_id"], ["created_at", "updated_at"]),
        "sheet_vitrina_v1_nomenclature_items": _table_spec("sku", "Server-owned nomenclature dictionary.", ["item_id"], ["created_at", "updated_at"], raw=["barcodes_json", "barcode_evidence_json", "aliases_json", "compatible_model_keys_json"]),
        "sheet_vitrina_v1_cny_documents": _table_spec("cny", "CNY account and supplier payment document metadata.", ["document_id"], ["operation_date", "operation_datetime", "uploaded_at", "created_at", "updated_at"], sensitive=["stored_file_path", "file_sha256", "natural_key"], raw=["parsed_payload_json", "raw_parse_json", "warnings_json", "errors_json"]),
        "sheet_vitrina_v1_cny_ledger_operations": _table_spec("cny", "CNY ledger replay operations.", ["operation_id"], ["operation_date", "operation_datetime", "created_at", "updated_at"]),
        "sheet_vitrina_v1_cny_ledger_replay_state": _table_spec("cny", "CNY ledger replay state.", ["slot"], ["replayed_at"], raw=["diagnostics_json"]),
        "sheet_vitrina_v1_factory_order_dataset_state": _table_spec("factory_order", "Factory order dataset state.", ["dataset_type"], ["uploaded_at"], raw=["rows_json"]),
        "sheet_vitrina_v1_factory_order_result_state": _table_spec("factory_order", "Latest factory-order calculation result.", ["slot"], ["calculated_at"], raw=["result_json"]),
        "sheet_vitrina_v1_wb_regional_supply_result_state": _table_spec("factory_order", "Latest WB regional supply calculation result.", ["slot"], ["calculated_at"], raw=["result_json"]),
    }


def _table_spec(
    domain: str,
    description: str,
    primary_id_columns: list[str],
    date_columns: list[str],
    *,
    raw: list[str] | None = None,
    sensitive: list[str] | None = None,
    order: str = "",
) -> dict[str, Any]:
    return {
        "domain": domain,
        "description": description,
        "primary_id_columns": primary_id_columns,
        "date_columns": date_columns,
        "raw_columns": raw or [],
        "sensitive_columns": sensitive or [],
        "default_order_by": order,
    }


def _table_spec_public(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "domain": spec.get("domain"),
        "description": spec.get("description"),
        "primary_id_columns": list(spec.get("primary_id_columns") or []),
        "date_columns": list(spec.get("date_columns") or []),
        "raw_business_payload_columns": list(spec.get("raw_columns") or []),
        "sensitive_redacted_columns": list(spec.get("sensitive_columns") or []),
        "default_order_by": spec.get("default_order_by") or "",
    }


def _artifact_kind_catalog() -> list[dict[str, Any]]:
    return [
        {"artifact_kind": "invoice", "source_domain": "trade_documents", "read_modes": ["metadata", "parsed", "base64_chunk"]},
        {"artifact_kind": "contract", "source_domain": "trade_documents", "read_modes": ["metadata", "parsed", "base64_chunk"]},
        {"artifact_kind": "packing_list", "source_domain": "financial_documents", "read_modes": ["metadata", "parsed", "base64_chunk"]},
        {"artifact_kind": "logistics_quote", "source_domain": "financial_documents", "read_modes": ["metadata", "parsed", "base64_chunk"]},
        {"artifact_kind": "logistics_invoice", "source_domain": "financial_documents", "read_modes": ["metadata", "parsed", "base64_chunk"]},
        {"artifact_kind": "customs_declaration", "source_domain": "financial_documents", "read_modes": ["metadata", "parsed", "base64_chunk"]},
        {"artifact_kind": "bank_control_statement", "source_domain": "financial_documents", "read_modes": ["metadata", "parsed", "base64_chunk"]},
        {"artifact_kind": "bank_transfer_application", "source_domain": "financial_documents", "read_modes": ["metadata", "parsed", "base64_chunk"]},
        {"artifact_kind": "bank_fee_statement", "source_domain": "financial_documents", "read_modes": ["metadata", "parsed", "base64_chunk"]},
        {"artifact_kind": "cny_conversion_purchase", "source_domain": "cny_documents", "read_modes": ["metadata", "parsed", "base64_chunk"]},
        {"artifact_kind": "supplier_cny_payment", "source_domain": "cny_documents", "read_modes": ["metadata", "parsed", "base64_chunk"]},
        {"artifact_kind": "document_package", "source_domain": "server_generated", "read_modes": ["metadata"]},
        {"artifact_kind": "unknown_business_document", "source_domain": "mixed", "read_modes": ["metadata", "base64_chunk"]},
    ]


def _require_table_spec(table: str) -> dict[str, Any]:
    normalized = str(table or "").strip()
    if normalized not in _business_table_catalog():
        raise WebCoreDataMcpError(f"business table is not allowlisted: {normalized}", code="table_not_allowlisted")
    return _business_table_catalog()[normalized]


def _schema(properties: dict[str, Any], *, required: Iterable[str] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_NO_DEFAULT = object()


def _documented(schema: dict[str, Any], description: str, *, default: Any = _NO_DEFAULT) -> dict[str, Any]:
    documented = dict(schema)
    documented["description"] = description
    if default is not _NO_DEFAULT:
        documented["default"] = default
    return documented


def _strict_output_schema(*field_names: str) -> dict[str, Any]:
    array_of_strings = {
        "allowed_units", "missing_source_flags", "notes", "object_types", "resolved_metric_keys",
        "source_tables", "unknown_object_types",
    }
    array_of_objects = {
        "artifact_refs", "artifacts", "candidate_metrics", "closure_states", "cny_documents_metadata", "dataset_state",
        "expense_summary",
        "financial_documents", "financial_documents_metadata", "financial_expense_lines", "lines", "metrics",
        "results", "rows", "snapshot_presence", "source_mtimes", "source_snapshots", "source_statuses",
        "temporal_slot_sources", "temporal_sources", "trade_documents", "trade_documents_metadata", "units",
    }
    object_fields = {
        "app", "artifact", "cached_payloads", "chunk", "date_range", "db", "factory_order",
        "factory_order_result", "filters", "identity", "latest_load", "latest_ready_snapshot", "latest_refresh",
        "latest_successful", "likely_failure_area", "limits", "line_summary", "packing_list_summary", "pagination",
        "ready_snapshots", "runtime_storage", "shipment", "stocks_freshness", "supplier_shipments", "supply", "target_identity",
        "wb_regional_supply_result", "wb_supplies", "document_parsed_fields_summary", "parsed_business_payload",
        "mcp_calls",
    }
    boolean_fields = {
        "cache_only", "credential_values_returned", "mutations", "no_recalculation", "no_upstream_fetch",
        "raw_env_returned", "raw_payloads_returned", "truncated", "upstream_calls",
    }
    integer_fields = {"limit", "limit_applied", "max_bytes", "row_count", "size_bytes"}
    properties: dict[str, Any] = {}
    for name in field_names:
        if name in array_of_strings:
            properties[name] = {"type": "array", "items": {"type": "string"}}
        elif name in array_of_objects:
            properties[name] = {"type": "array", "items": {"type": "object", "additionalProperties": True}}
        elif name in object_fields:
            properties[name] = {"type": ["object", "null"], "additionalProperties": True}
        elif name in boolean_fields:
            properties[name] = {"type": "boolean"}
        elif name in integer_fields:
            properties[name] = {"type": "integer", "minimum": 0}
        elif name == "base64":
            properties[name] = {"type": "string", "contentEncoding": "base64"}
        else:
            properties[name] = {"type": ["string", "null"]}
    return {
        "type": "object",
        "properties": properties,
        "required": ["status"],
        "additionalProperties": False,
    }


def _object_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": True}


def _string_schema(min_length: int, max_length: int) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "maxLength": max_length}
    if min_length:
        schema["minLength"] = min_length
    return schema


def _date_schema() -> dict[str, Any]:
    return {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"}


def _array_schema(item_schema: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": item_schema, "maxItems": 10}


def _enum_schema(values: list[str]) -> dict[str, Any]:
    return {"type": "string", "enum": values}


def _int_schema(minimum: int, maximum: int) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _optional_bool(value: Any, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "да"}:
        return True
    if text in {"0", "false", "no", "n", "нет"}:
        return False
    raise WebCoreDataMcpError("boolean argument is invalid", code="invalid_arguments")


def _optional_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WebCoreDataMcpError("integer argument is invalid", code="invalid_arguments") from exc
    if parsed < minimum or parsed > maximum:
        raise WebCoreDataMcpError(f"integer argument must be between {minimum} and {maximum}", code="invalid_arguments")
    return parsed


def _optional_mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise WebCoreDataMcpError("filters must be an object", code="invalid_arguments")
    return value


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({_quote_ident(table_name)})").fetchall()]


def _quote_ident(value: str) -> str:
    text = str(value or "")
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", text):
        raise WebCoreDataMcpError(f"unsafe SQL identifier: {text}", code="unsafe_identifier")
    return '"' + text.replace('"', '""') + '"'


def _safe_table_columns(columns: list[str], spec: Mapping[str, Any], *, include_raw_business_payloads: bool) -> list[str]:
    sensitive = set(spec.get("sensitive_columns") or [])
    raw = set(spec.get("raw_columns") or [])
    selected = []
    for column in columns:
        if column in sensitive:
            continue
        if column in raw and not include_raw_business_payloads:
            continue
        if _is_sensitive_column_name(column):
            continue
        selected.append(column)
    return selected or [columns[0]]


def _allowed_order_columns(columns: list[str], spec: Mapping[str, Any]) -> list[str]:
    blocked = set(spec.get("sensitive_columns") or []) | set(spec.get("raw_columns") or [])
    return [column for column in columns if column not in blocked and not _is_sensitive_column_name(column)]


def _build_table_where(
    columns: list[str],
    spec: Mapping[str, Any],
    *,
    filters: Mapping[str, Any],
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list[Any], dict[str, Any]]:
    allowed = set(_safe_table_columns(columns, spec, include_raw_business_payloads=False))
    clauses: list[str] = []
    params: list[Any] = []
    applied: dict[str, Any] = {}
    for key, value in filters.items():
        column = str(key or "").strip()
        if column not in allowed:
            raise WebCoreDataMcpError(f"filter column is not allowed: {column}", code="filter_not_allowed")
        if isinstance(value, list):
            bounded = [item for item in value[:20]]
            if not bounded:
                continue
            clauses.append(f"{_quote_ident(column)} IN ({', '.join('?' for _ in bounded)})")
            params.extend(bounded)
            applied[column] = bounded
        else:
            clauses.append(f"{_quote_ident(column)} = ?")
            params.append(value)
            applied[column] = value
    date_columns = [column for column in spec.get("date_columns") or [] if column in columns]
    if (date_from or date_to) and not date_columns:
        raise WebCoreDataMcpError("table has no allowlisted date column for date range filter", code="date_filter_not_supported")
    if date_columns:
        date_column = date_columns[0]
        if date_from:
            clauses.append(f"substr({_quote_ident(date_column)}, 1, 10) >= ?")
            params.append(date_from)
            applied["date_from"] = date_from
        if date_to:
            clauses.append(f"substr({_quote_ident(date_column)}, 1, 10) <= ?")
            params.append(date_to)
            applied["date_to"] = date_to
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params, applied


def _order_by_sql(columns: list[str], spec: Mapping[str, Any], order_by: str | None) -> str:
    allowed = set(_allowed_order_columns(columns, spec))
    requested = str(order_by or "").strip()
    if requested:
        direction = "ASC"
        column = requested
        if requested.startswith("-"):
            column = requested[1:]
            direction = "DESC"
        elif " " in requested:
            parts = requested.split()
            column = parts[0]
            if len(parts) > 1 and parts[1].upper() in {"ASC", "DESC"}:
                direction = parts[1].upper()
        if column not in allowed:
            raise WebCoreDataMcpError(f"order_by column is not allowed: {column}", code="order_by_not_allowed")
        return f"ORDER BY {_quote_ident(column)} {direction}"
    default_order = str(spec.get("default_order_by") or "").strip()
    if default_order:
        safe_parts = []
        for part in default_order.split(","):
            tokens = part.strip().split()
            if not tokens:
                continue
            column = tokens[0]
            direction = tokens[1].upper() if len(tokens) > 1 and tokens[1].upper() in {"ASC", "DESC"} else "ASC"
            if column in allowed:
                safe_parts.append(f"{_quote_ident(column)} {direction}")
        if safe_parts:
            return "ORDER BY " + ", ".join(safe_parts)
    date_columns = [column for column in spec.get("date_columns") or [] if column in allowed]
    if date_columns:
        return f"ORDER BY {_quote_ident(date_columns[-1])} DESC"
    primary = [column for column in spec.get("primary_id_columns") or [] if column in allowed]
    if primary:
        return f"ORDER BY {_quote_ident(primary[0])} ASC"
    return ""


def _cursor_to_offset(cursor: str | None, offset: int) -> int:
    if not cursor:
        return offset
    try:
        parsed = int(cursor)
    except ValueError as exc:
        raise WebCoreDataMcpError("cursor must be a numeric offset cursor", code="invalid_cursor") from exc
    if parsed < 0:
        raise WebCoreDataMcpError("cursor must be non-negative", code="invalid_cursor")
    return parsed


def _is_sensitive_column_name(column: str) -> bool:
    lowered = column.lower()
    return any(marker in lowered for marker in ("password", "secret", "token", "cookie", "authorization", "storage_state", "session", "oauth", "private_key"))


def _business_row_payload(row: Mapping[str, Any], *, raw_columns: set[str], include_raw_business_payloads: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in row.items():
        if key in raw_columns:
            if include_raw_business_payloads:
                parsed = _safe_json_loads(value)
                payload[f"{key}_scrubbed_payload"] = _scrub_business_payload(parsed if parsed is not None else value)
            else:
                payload[f"{key}_summary"] = _compact_json_summary(_safe_json_loads(value))
        else:
            payload[key] = value
    return payload


def _single_count_min_max(
    conn: sqlite3.Connection,
    table_name: str,
    date_column: str,
    *,
    extra_max_column: str | None = None,
) -> dict[str, Any]:
    if not _table_exists(conn, table_name):
        return {"status": "missing_table", "table": table_name}
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
    if date_column not in columns:
        return {"status": "missing_column", "table": table_name, "column": date_column}
    extra_sql = f", MAX({extra_max_column}) AS max_{extra_max_column}" if extra_max_column and extra_max_column in columns else ""
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS row_count, MIN({date_column}) AS min_{date_column},
               MAX({date_column}) AS max_{date_column}
               {extra_sql}
        FROM {table_name}
        """
    ).fetchone()
    result = _row_dict(row)
    result["status"] = "ok"
    result["table"] = table_name
    return result


def _missing_table_result(table_name: str) -> dict[str, Any]:
    return {"status": "missing_table", "table": table_name}


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {str(key): row[key] for key in row.keys()}


def _safe_json_loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return None


def _json_object(value: Any) -> dict[str, Any]:
    parsed = _safe_json_loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _compact_json_summary(value: Any) -> dict[str, Any]:
    if value is None:
        return {"present": False}
    if isinstance(value, dict):
        keys = sorted(str(key) for key in value.keys())
        return {
            "present": True,
            "type": "object",
            "key_count": len(keys),
            "keys_sample": keys[:20],
        }
    if isinstance(value, list):
        return {"present": True, "type": "array", "length": len(value)}
    return {"present": True, "type": type(value).__name__}


def _select_keys(payload: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for key in keys:
        if key in payload:
            selected[key] = payload[key]
    return selected


def _scrub_business_payload(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated_depth]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:200]:
            text_key = str(key)
            lowered = text_key.lower()
            if _is_sensitive_column_name(text_key) or any(marker in lowered for marker in ("file_path", "stored_file_path", "source_file_path", "absolute_path", "sha256", "hash")):
                result[text_key] = "[redacted]"
            else:
                result[text_key] = _scrub_business_payload(item, depth=depth + 1)
        if len(value) > 200:
            result["_truncated_keys"] = len(value) - 200
        return result
    if isinstance(value, list):
        result = [_scrub_business_payload(item, depth=depth + 1) for item in value[:200]]
        if len(value) > 200:
            result.append({"_truncated_items": len(value) - 200})
        return result
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    return value


def _redact_sensitive_text(value: str) -> str:
    text = str(value)
    lowered = text.lower()
    if any(marker in lowered for marker in SECRET_TEXT_MARKERS):
        return "[redacted]"
    text = re.sub(r"(?i)(password|token|secret|authorization|cookie)\s*[:=]\s*\S+", r"\1=[redacted]", text)
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    if len(text) > 12000:
        return text[:12000] + "...[truncated]"
    return text


def _omit_keys(payload: Mapping[str, Any], keys: set[str]) -> dict[str, Any]:
    return {str(key): value for key, value in payload.items() if str(key) not in keys and not _is_sensitive_column_name(str(key))}


def _fetch_table_rows_for_owner(
    conn: sqlite3.Connection,
    *,
    table: str,
    owner_column: str,
    owner_id: str,
    limit: int,
    omit_columns: set[str],
    include_raw_business_payloads: bool,
    order_by: str,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, table):
        return []
    columns = _table_columns(conn, table)
    if owner_column not in columns:
        return []
    selected = [
        column
        for column in columns
        if column not in omit_columns and not _is_sensitive_column_name(column) and not any(marker in column.lower() for marker in ("file_path", "sha256", "hash"))
    ]
    raw_columns = {column for column in columns if column in omit_columns and column.endswith("_json")}
    if include_raw_business_payloads:
        selected.extend(column for column in raw_columns if column not in selected)
    if not selected:
        selected = [owner_column]
    safe_order = _safe_order_clause_for_existing_columns(order_by, columns)
    rows = conn.execute(
        f"""
        SELECT {', '.join(_quote_ident(column) for column in selected)}
        FROM {_quote_ident(table)}
        WHERE {_quote_ident(owner_column)} = ?
        {safe_order}
        LIMIT ?
        """,
        (owner_id, limit),
    ).fetchall()
    return [
        _business_row_payload(
            _row_dict(row),
            raw_columns={column for column in selected if column.endswith("_json")},
            include_raw_business_payloads=include_raw_business_payloads,
        )
        for row in rows
    ]


def _fetch_packing_list_docs_for_shipments(conn: sqlite3.Connection, shipment_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    ids = []
    seen = set()
    for value in shipment_ids:
        text = str(value or "").strip()
        if text and text not in seen:
            ids.append(text)
            seen.add(text)
    if not ids or not _table_exists(conn, "sheet_vitrina_v1_supplier_financial_documents"):
        return {}
    columns = set(_table_columns(conn, "sheet_vitrina_v1_supplier_financial_documents"))
    if "supplier_order_id" not in columns or "document_type" not in columns:
        return {}
    selected = _select_existing_columns(
        columns,
        [
            "document_id",
            "supplier_order_id",
            "document_type",
            "original_filename",
            "uploaded_at",
            "updated_at",
            "parse_status",
            "document_number",
            "document_date",
            "currency",
            "total_amount",
            "total_amount_rub",
            "normalized_parse_json",
            "warnings_json",
            "errors_json",
        ],
    )
    placeholders = ", ".join("?" for _ in ids)
    safe_order = _safe_order_clause_for_existing_columns("uploaded_at DESC, document_id ASC", list(columns))
    rows = conn.execute(
        f"""
        SELECT {', '.join(_quote_ident(column) for column in selected)}
        FROM sheet_vitrina_v1_supplier_financial_documents
        WHERE supplier_order_id IN ({placeholders}) AND document_type = ?
        {safe_order}
        LIMIT ?
        """,
        (*ids, "packing_list", min(MAX_LIMIT, max(1, len(ids) * 10))),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = _row_dict(row)
        supplier_order_id = str(item.get("supplier_order_id") or "")
        if not supplier_order_id:
            continue
        grouped.setdefault(supplier_order_id, []).append(item)
    return grouped


def _packing_list_summary_from_documents(documents: list[Mapping[str, Any]], *, line_item_limit: int) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "document_count": len(documents),
        "parsed_document_count": 0,
        "parse_status": None,
        "document_ids": [str(document.get("document_id") or "") for document in documents if document.get("document_id")],
        "document_number": "",
        "total_cartons": None,
        "box_count": None,
        "carton_count": None,
        "total_boxes": None,
        "total_quantity": None,
        "total_gross_weight_kg": None,
        "total_volume_m3": None,
        "carton_size": "",
        "model_count": None,
        "avg_qty_per_carton": None,
        "line_item_count": 0,
        "line_items_sample": [],
        "missing_fields": [],
        "reason": "",
    }
    if not documents:
        summary["reason"] = "packing_list_document_not_found"
        return summary
    statuses = [str(document.get("parse_status") or "").strip() for document in documents if str(document.get("parse_status") or "").strip()]
    summary["parse_status"] = statuses[0] if len(set(statuses)) == 1 else ", ".join(sorted(set(statuses)))
    parsed_payloads: list[dict[str, Any]] = []
    for document in documents:
        parsed = document.get("normalized_parse")
        if not isinstance(parsed, Mapping):
            parsed = _safe_json_loads(document.get("normalized_parse_json"))
        if isinstance(parsed, Mapping) and isinstance(parsed.get("normalized_parse"), Mapping):
            parsed = parsed.get("normalized_parse")
        if isinstance(parsed, Mapping):
            parsed_payloads.append(dict(parsed))
    summary["parsed_document_count"] = len(parsed_payloads)
    if not parsed_payloads:
        summary["reason"] = "packing_list_parse_payload_unavailable"
        return summary
    primary = parsed_payloads[0]
    total_cartons = _sum_present_numbers(payload.get("total_cartons") for payload in parsed_payloads)
    total_quantity = _sum_present_numbers(payload.get("total_quantity") for payload in parsed_payloads)
    total_gross = _sum_present_numbers(payload.get("total_gross_weight_kg") for payload in parsed_payloads)
    total_volume = _sum_present_numbers(payload.get("total_volume_m3") for payload in parsed_payloads)
    summary["document_number"] = str(primary.get("document_number") or primary.get("document_title") or "")
    summary["total_cartons"] = total_cartons
    summary["box_count"] = total_cartons
    summary["carton_count"] = total_cartons
    summary["total_boxes"] = total_cartons
    summary["total_quantity"] = total_quantity
    summary["total_gross_weight_kg"] = total_gross
    summary["total_volume_m3"] = total_volume
    summary["carton_size"] = _first_present_text(payload.get("carton_size") for payload in parsed_payloads)
    summary["model_count"] = _first_present_number(payload.get("model_count") for payload in parsed_payloads)
    summary["avg_qty_per_carton"] = (
        total_quantity / total_cartons
        if total_quantity is not None and total_cartons not in (None, 0)
        else _first_present_number(payload.get("avg_qty_per_carton") for payload in parsed_payloads)
    )
    line_items: list[Any] = []
    line_count = 0
    for payload in parsed_payloads:
        payload_items = payload.get("line_items")
        if isinstance(payload_items, list):
            line_items.extend(payload_items)
            line_count += len(payload_items)
        else:
            count = _first_present_number([payload.get("line_item_count")])
            if count is not None:
                line_count += int(count)
    summary["line_item_count"] = line_count
    if line_item_limit > 0:
        summary["line_items_sample"] = _scrub_business_payload(line_items[:line_item_limit])
    missing = [
        field
        for field in ("total_cartons", "total_quantity", "total_gross_weight_kg", "total_volume_m3")
        if summary.get(field) is None
    ]
    summary["missing_fields"] = missing
    if missing:
        summary["reason"] = "packing_list_parsed_payload_missing_fields"
    return summary


def _document_parsed_fields_summary(packing_docs: list[Mapping[str, Any]]) -> dict[str, Any]:
    packing_summary = _packing_list_summary_from_documents(packing_docs, line_item_limit=0)
    return {
        "packing_list": {
            "document_count": packing_summary.get("document_count"),
            "parsed_document_count": packing_summary.get("parsed_document_count"),
            "parse_status": packing_summary.get("parse_status"),
            "available_fields": [
                field
                for field in (
                    "total_cartons",
                    "box_count",
                    "carton_count",
                    "total_boxes",
                    "total_quantity",
                    "total_gross_weight_kg",
                    "total_volume_m3",
                    "carton_size",
                    "model_count",
                    "avg_qty_per_carton",
                    "line_item_count",
                )
                if packing_summary.get(field) not in (None, "", [])
            ],
            "missing_fields": packing_summary.get("missing_fields") or [],
            "reason": packing_summary.get("reason") or "",
        }
    }


def _supplier_registry_order_by(sort_by: str) -> str:
    key = str(sort_by or "").strip()
    return {
        "date_desc": "COALESCE(s.invoice_date, s.shipment_date, s.created_at, '') DESC, s.shipment_id",
        "shipment_date_desc": "COALESCE(s.shipment_date, s.invoice_date, s.created_at, '') DESC, s.shipment_id",
        "product_qty_total_desc": "COALESCE(s.product_qty_total, SUM(CASE WHEN l.qty IS NULL THEN 0 ELSE l.qty END), 0) DESC, COALESCE(s.invoice_date, s.shipment_date, s.created_at, '') DESC, s.shipment_id",
        "invoice_amount_total_desc": "COALESCE(s.invoice_amount_total, s.product_amount_total, 0) DESC, COALESCE(s.invoice_date, s.shipment_date, s.created_at, '') DESC, s.shipment_id",
        "expense_amount_rub_desc": "COALESCE(SUM(CASE WHEN fe.amount_rub IS NULL THEN 0 ELSE fe.amount_rub END), 0) DESC, COALESCE(s.invoice_date, s.shipment_date, s.created_at, '') DESC, s.shipment_id",
    }.get(key, "COALESCE(s.invoice_date, s.shipment_date, s.created_at, '') DESC, s.shipment_id")


def _sum_present_numbers(values: Iterable[Any]) -> float | None:
    total = 0.0
    seen = False
    for value in values:
        number = _coerce_number(value)
        if number is None:
            continue
        total += number
        seen = True
    return total if seen else None


def _first_present_number(values: Iterable[Any]) -> float | None:
    for value in values:
        number = _coerce_number(value)
        if number is not None:
            return number
    return None


def _first_present_text(values: Iterable[Any]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _safe_order_clause_for_existing_columns(order_by: str, columns: list[str]) -> str:
    parts: list[str] = []
    for raw_part in str(order_by or "").split(","):
        tokens = raw_part.strip().split()
        if not tokens:
            continue
        column = tokens[0]
        direction = tokens[1].upper() if len(tokens) > 1 and tokens[1].upper() in {"ASC", "DESC"} else "ASC"
        if column in columns:
            parts.append(f"{_quote_ident(column)} {direction}")
    return "ORDER BY " + ", ".join(parts) if parts else ""


def _select_existing_columns(columns: set[str], requested: list[str]) -> list[str]:
    return [column for column in requested if column in columns]


def _with_artifact_refs(rows: list[dict[str, Any]], *, source_domain: str) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        item = dict(row)
        document_id = str(item.get("document_id") or "")
        supplier_order_id = str(item.get("supplier_order_id") or item.get("source_order_id") or "")
        if source_domain == "financial_documents" and document_id and supplier_order_id:
            item["artifact_ref"] = f"financial_document:{supplier_order_id}:{document_id}"
        elif source_domain == "trade_documents" and document_id:
            item["artifact_ref"] = f"trade_document:{document_id}"
        elif source_domain == "cny_documents" and document_id:
            item["artifact_ref"] = f"cny_document:{document_id}"
        result.append(item)
    return result


def _artifact_row(
    *,
    artifact_ref: str,
    artifact_kind: str,
    source_domain: str,
    source_table: str,
    linked_shipment_id: str,
    linked_document_id: str,
    filename: str,
    content_type: str,
    stored_path: str,
    uploaded_at: str,
    updated_at: str,
    status: str,
    parse_status: str,
    parsed_payload: Any,
) -> dict[str, Any]:
    return {
        "artifact_ref": artifact_ref,
        "artifact_kind": _normalize_artifact_kind(artifact_kind),
        "source_domain": source_domain,
        "source_table": source_table,
        "linked_shipment_id": linked_shipment_id,
        "linked_document_id": linked_document_id,
        "filename": _safe_display_filename(filename),
        "content_type": content_type or "application/octet-stream",
        "uploaded_at": uploaded_at,
        "updated_at": updated_at,
        "document_status": status,
        "parse_status": parse_status,
        "supported_read_modes": _artifact_read_modes(content_type),
        "availability": {"has_registered_file": bool(stored_path), "server_owned_ref": True},
        "parsed_payload": parsed_payload,
        "_stored_path": stored_path,
    }


def _artifact_public(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "artifact_ref": row.get("artifact_ref"),
        "artifact_kind": row.get("artifact_kind"),
        "source_domain": row.get("source_domain"),
        "source_table": row.get("source_table"),
        "linked_shipment_id": row.get("linked_shipment_id"),
        "linked_document_id": row.get("linked_document_id"),
        "filename": row.get("filename"),
        "content_type": row.get("content_type"),
        "uploaded_at": row.get("uploaded_at"),
        "updated_at": row.get("updated_at"),
        "document_status": row.get("document_status"),
        "parse_status": row.get("parse_status"),
        "availability": row.get("availability"),
        "supported_read_modes": row.get("supported_read_modes"),
    }


def _artifact_read_modes(content_type: str) -> list[str]:
    modes = ["metadata", "parsed"]
    if _artifact_text_content_supported({"content_type": content_type, "filename": ""}):
        modes.extend(["text", "text_chunk"])
    if _artifact_binary_content_allowed({"content_type": content_type}):
        modes.append("base64_chunk")
    return modes


def _artifact_text_content_supported(artifact: Mapping[str, Any]) -> bool:
    content_type = str(artifact.get("content_type") or "").lower()
    filename = str(artifact.get("filename") or "").lower()
    return content_type.startswith("text/") or content_type in {"application/json", "application/xml"} or filename.endswith((".txt", ".json", ".csv", ".xml"))


def _artifact_binary_content_allowed(artifact: Mapping[str, Any]) -> bool:
    content_type = str(artifact.get("content_type") or "").lower()
    return content_type in {
        "application/pdf",
        "application/octet-stream",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/jpeg",
        "image/png",
        "text/plain",
        "application/json",
    } or content_type.startswith("text/")


def _safe_decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_file_chunk(path: Path, *, start: int, max_bytes: int) -> bytes:
    with path.open("rb") as fh:
        fh.seek(max(0, start))
        return fh.read(max(0, max_bytes))


def _safe_display_filename(value: str) -> str:
    name = Path(str(value or "document")).name
    return name[:180] or "document"


def _normalize_artifact_kind(value: str) -> str:
    text = str(value or "").strip()
    aliases = {
        "contract": "contract",
        "invoice": "invoice",
        "packing_list": "packing_list",
        "packing list": "packing_list",
        "packing-list": "packing_list",
        "logistics_quote": "logistics_quote",
        "logistics_invoice": "logistics_invoice",
        "customs_declaration": "customs_declaration",
        "bank_control_statement": "bank_control_statement",
        "bank_transfer_application": "bank_transfer_application",
        "bank_fee_statement": "bank_fee_statement",
        "cny_conversion_purchase": "cny_conversion_purchase",
        "supplier_cny_payment": "supplier_cny_payment",
        "bank_fee": "bank_fee_statement",
    }
    return aliases.get(text, text or "unknown_business_document")


def _source_domain_matches(requested: str | None, current: str) -> bool:
    return not requested or requested == current or (requested == "supplier_shipments" and current in {"trade_documents", "financial_documents", "cny_documents"})


def _append_like_filter(clauses: list[str], params: list[Any], column_sql: str, value: str | None) -> None:
    if value:
        clauses.append(f"{column_sql} LIKE ?")
        params.append(f"%{value}%")


def _append_exact_filter(clauses: list[str], params: list[Any], column_sql: str, value: str | None) -> None:
    if value:
        clauses.append(f"{column_sql} = ?")
        params.append(value)


def _derived_supplier_status_sql(
    alias: str, *, historical_exception_available: bool = True
) -> str:
    prefix = str(alias or "").strip()
    qualifier = f"{prefix}." if prefix else ""
    acceptance = f"{qualifier}actual_ff_acceptance_date"
    shipment = f"{qualifier}actual_shipment_date"
    exception_clause = (
        f"WHEN {qualifier}historical_status_exception="
        "'legacy_ff_accepted_without_date' "
        f"AND COALESCE({shipment},'')='' AND COALESCE({acceptance},'')='' "
        "THEN 'accepted_ff' "
        if historical_exception_available
        else ""
    )
    return (
        "CASE "
        f"WHEN length({acceptance})=10 AND date({acceptance})={acceptance} AND {acceptance}<=? "
        "THEN 'accepted_ff' "
        f"{exception_clause}"
        f"WHEN length({shipment})=10 AND date({shipment})={shipment} AND {shipment}<=? "
        "THEN 'in_transit' ELSE 'production' END"
    )


def _supplier_status_order_direction(order_by: str | None) -> str:
    requested = str(order_by or "").strip()
    if not requested:
        return ""
    direction = "ASC"
    column = requested
    if requested.startswith("-"):
        column = requested[1:]
        direction = "DESC"
    elif " " in requested:
        parts = requested.split()
        column = parts[0]
        if len(parts) > 1 and parts[1].upper() in {"ASC", "DESC"}:
            direction = parts[1].upper()
    return direction if column == "order_status" else ""


def _recommended_call(step: int, tool: str, arguments: dict[str, Any], scope: str, expected_result: str) -> dict[str, Any]:
    return {
        "step": step,
        "tool": tool,
        "arguments": arguments,
        "required_scope": scope,
        "expected_result": expected_result,
        "fallback_if_empty": _fallback_path_for_tool(tool),
        "known_limitations": _tool_limitations(tool),
    }


def _fallback_path_for_tool(tool: str) -> str:
    return {
        "get_supplier_shipment_full_details": "Use search_business_objects by invoice/supplier, then retry with shipment_id.",
        "list_supply_artifacts": "Use get_supplier_shipment_full_details to inspect linked document metadata.",
        "get_wb_supply_full_details": "Use get_wb_supplies_registry or search_business_objects with object_types=['wb_supply'].",
        "get_metric_values": "Use list_metrics and get_available_metric_dates to resolve metric/date coverage.",
    }.get(tool, "Call get_webcore_data_map or resolve_webcore_data_request for next-step routing.")


def _tool_limitations(tool: str) -> list[str]:
    if tool in {"get_wb_supplies_registry", "get_wb_supply_full_details"}:
        return ["Cache-only; no upstream sync/backfill/fetch."]
    if tool in {"list_supply_artifacts", "get_supply_artifact"}:
        return ["Server-owned artifacts only; no arbitrary filesystem paths.", "File content is bounded/chunked and scrubbed."]
    if tool == "get_webcore_business_table_rows":
        return ["Allowlisted generated SELECT only; raw payloads require explicit flag."]
    return []


def _fallback_path_for_domain(domain: str) -> str:
    return {
        "supplier_shipments": "Search shipment by invoice/supplier, then use full details or artifact tools.",
        "artifacts": "List artifacts first to get artifact_ref, then read metadata/parsed/chunk.",
        "wb_supplies": "Use cached registry first, then full detail by supply id.",
        "metrics": "Use list_metrics to resolve key/Russian label.",
        "sku": "Use search_business_objects for sku/nomenclature.",
    }.get(domain, "Use get_webcore_data_map for orientation.")


def _infer_request_intent(intent: str, **hints: Any) -> dict[str, Any]:
    text = " ".join(str(item or "") for item in [intent, hints.get("domain"), hints.get("artifact_kind")]).casefold()
    explicit_domain = str(hints.get("domain") or "auto")
    action = "lookup"
    domain = explicit_domain if explicit_domain != "auto" else "unknown"
    object_type = ""
    artifact_kind = _artifact_kind_from_text(text, str(hints.get("artifact_kind") or "auto"))
    if any(marker in text for marker in ("свеж", "freshness")):
        domain, action = "freshness", "check_freshness"
    elif any(marker in text for marker in ("метрик", "metric", "остат", "выруч", "заказ")) and not any(marker in text for marker in ("поставк", "инвойс", "договор")):
        domain, action = "metrics", "get_metric"
    elif any(marker in text for marker in ("sku", "номенклат", "nmid", "nm_id")):
        domain, action, object_type = "sku", "find_sku", "sku"
    elif any(marker in text for marker in ("wb supply", "wildberries", "вб постав", "wb постав")):
        domain, action, object_type = "wb_supplies", "show_wb_supply", "wb_supply"
    elif any(marker in text for marker in ("самая большая постав", "самую большую постав", "largest shipment", "biggest shipment")):
        domain, action, object_type = "supplier_shipments", "find_largest", "shipment"
    elif any(marker in text for marker in ("упаковоч", "packing list", "короб", "carton", "box count", "boxes", "parsed-пол", "parsed пол")):
        if any(marker in text for marker in ("открой", "open", "прочитай", "read", "документ")):
            domain, action, object_type = "artifacts", "open_artifact", "artifact"
        else:
            domain, action, object_type = "supplier_shipments", "packing_list_summary", "shipment"
    elif any(marker in text for marker in ("реестр постав", "registry")):
        domain, action, object_type = "supplier_shipments", "show_registry", "shipment"
    elif any(marker in text for marker in ("инвойс", "договор", "бтт", "втб", "платёж", "платеж", "заявление на перевод", "вбк", "ведомость банковского", "дт", "тамож", "кп логист", "счёт логист", "счет логист", "документ")):
        domain, action, object_type = "artifacts", "open_artifact" if any(marker in text for marker in ("открой", "open", "прочитай", "read")) else "show_documents", "artifact"
    elif any(marker in text for marker in ("счёт cny", "счет cny", "cny", "конвертац")):
        domain, action = "cny", "lookup_cny"
    elif any(marker in text for marker in ("поставк", "от поставщика", "shipment")):
        domain, action, object_type = "supplier_shipments", "show_shipment", "shipment"
    elif any(marker in text for marker in ("таблиц", "table")):
        domain, action = "business_tables", "list_tables"
    if hints.get("shipment_id"):
        object_type = "shipment"
    if hints.get("supply_id"):
        object_type = "wb_supply"
    return {
        "domain": domain,
        "action": action,
        "object_type": object_type,
        "object_id": hints.get("shipment_id") or hints.get("supply_id") or hints.get("object_id"),
        "artifact_kind": artifact_kind,
        "confidence": "high" if domain != "unknown" else "low",
    }


def _artifact_kind_from_text(text: str, explicit: str) -> str:
    if explicit and explicit != "auto":
        return explicit
    checks = [
        ("packing_list", ("packing list", "упаковоч", "короб", "carton", "box count", "boxes")),
        ("bank_control_statement", ("вбк", "ведомость банковского")),
        ("bank_transfer_application", ("бтт", "втб", "платёж", "платеж", "заявление на перевод")),
        ("customs_declaration", ("дт", "тамож")),
        ("logistics_quote", ("кп логист", "коммерческое предложение")),
        ("logistics_invoice", ("счёт логист", "счет логист")),
        ("bank_fee_statement", ("банковская выписка", "комисси")),
        ("cny_conversion_purchase", ("конвертац", "покупка cny")),
        ("supplier_cny_payment", ("оплата cny", "платеж cny", "платёж cny")),
        ("contract", ("договор", "contract")),
        ("invoice", ("инвойс", "invoice")),
    ]
    for kind, markers in checks:
        if any(marker in text for marker in markers):
            return kind
    return "auto"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in FORBIDDEN_OUTPUT_KEY_MARKERS):
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value[:300]]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _redact_string(value: str) -> str:
    lowered = value.lower()
    if any(marker in lowered for marker in SECRET_TEXT_MARKERS):
        return "[redacted]"
    if len(value) > 600:
        return value[:600] + "...[truncated]"
    return value


def _bounded_list(value: Any, *, limit: int) -> list[Any]:
    if isinstance(value, list):
        return _redact(value[:limit])
    return []


def _required_str(args: Mapping[str, Any], name: str, *, max_length: int) -> str:
    value = args.get(name)
    text = str(value or "").strip()
    if not text:
        raise WebCoreDataMcpError(f"{name} is required", code="invalid_arguments")
    if len(text) > max_length:
        raise WebCoreDataMcpError(f"{name} is too long", code="invalid_arguments")
    return text


def _optional_str(value: Any, *, max_length: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_length:
        raise WebCoreDataMcpError("string argument is too long", code="invalid_arguments")
    return text


def _optional_str_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise WebCoreDataMcpError("object_types must be an array", code="invalid_arguments")
    return [str(item).strip() for item in value if str(item).strip()][:10]


def _required_date(args: Mapping[str, Any], name: str) -> str:
    return _validate_date(_required_str(args, name, max_length=10))


def _optional_date(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return _validate_date(str(value).strip())


def _validate_date(value: str) -> str:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        raise WebCoreDataMcpError("date must be YYYY-MM-DD", code="invalid_date")
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _validate_date_range(date_from: str | None, date_to: str | None, *, max_days: int = MAX_DATE_RANGE_DAYS) -> None:
    if date_from:
        _validate_date(date_from)
    if date_to:
        _validate_date(date_to)
    if date_from and date_to:
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
        if end < start:
            raise WebCoreDataMcpError("date_to must be >= date_from", code="invalid_date_range")
        if (end - start).days > max_days:
            raise WebCoreDataMcpError(f"date range must be <= {max_days} days", code="date_range_too_large")


def _bounded_limit(value: Any, default_limit: int) -> int:
    if value is None or value == "":
        return default_limit
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WebCoreDataMcpError("limit must be an integer", code="invalid_limit") from exc
    return max(1, min(parsed, MAX_LIMIT))


def _infer_source_key(value: str) -> str:
    text = value.lower()
    known = (
        "fin_report_daily",
        "sales_funnel_history",
        "seller_funnel_snapshot",
        "stocks",
        "onec_stocks",
        "ads_compact",
        "ads_bids",
        "prices_snapshot",
        "promo_by_price",
        "spp_proxy",
        "spp",
        "web_source_snapshot",
    )
    for item in known:
        if item in text:
            return item
    return ""


def _metric_caveats(row: Mapping[str, Any], inferred_source: str) -> list[str]:
    caveats = []
    if not inferred_source:
        caveats.append("source key could not be inferred from calc_ref/metric_key")
    if str(row.get("enabled")) not in {"1", "True", "true"}:
        caveats.append("metric is not marked enabled in registry_upload_metrics_v2")
    return caveats


def _first_number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _number_or_zero(value: Any) -> float:
    return _first_number(value) or 0.0


def _extract_named_metrics(value: Any, sku_or_nm_id: str | None, *, name_markers: tuple[str, ...]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def visit(node: Any, context: dict[str, Any]) -> None:
        if len(results) >= 100:
            return
        if isinstance(node, dict):
            next_context = dict(context)
            for key in ("nm_id", "nmId", "sku", "sku_id", "object_id", "id"):
                if key in node and node[key] not in (None, ""):
                    next_context.setdefault("nm_id", str(node[key]))
            if sku_or_nm_id and next_context.get("nm_id") not in {None, sku_or_nm_id}:
                return
            metrics = node.get("metrics")
            if isinstance(metrics, dict):
                for key, metric_value in metrics.items():
                    if _metric_name_matches(str(key), name_markers):
                        results.append({"metric_key": str(key), "value": metric_value, **next_context})
            for key, item in node.items():
                if isinstance(item, (int, float)) and _metric_name_matches(str(key), name_markers):
                    results.append({"metric_key": str(key), "value": item, **next_context})
                elif isinstance(item, (dict, list)):
                    visit(item, next_context)
        elif isinstance(node, list):
            for item in node[:500]:
                visit(item, context)

    visit(value, {})
    return _redact(results)


def _extract_metric_values(value: Any, metric_key: str, sku_or_nm_id: str | None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def visit(node: Any, context: dict[str, Any]) -> None:
        if len(results) >= 300:
            return
        if isinstance(node, dict):
            next_context = dict(context)
            for key in ("nm_id", "nmId", "sku", "sku_id", "object_id", "id"):
                if key in node and node[key] not in (None, ""):
                    next_context.setdefault("nm_id", str(node[key]))
            if sku_or_nm_id and next_context.get("nm_id") not in {None, sku_or_nm_id}:
                return
            metrics = node.get("metrics")
            if isinstance(metrics, dict) and metric_key in metrics:
                results.append({"metric_key": metric_key, "value": metrics[metric_key], **next_context})
            if metric_key in node and isinstance(node[metric_key], (int, float)):
                results.append({"metric_key": metric_key, "value": node[metric_key], **next_context})
            for item in node.values():
                if isinstance(item, (dict, list)):
                    visit(item, next_context)
        elif isinstance(node, list):
            for item in node[:500]:
                visit(item, context)

    visit(value, {})
    return _redact(results)


def _extract_ready_snapshot_metric_rows(
    value: Any,
    *,
    metric_keys: set[str],
    metric_query: str | None,
    sku_or_nm_id: str | None,
    date_from: str | None,
    date_to: str | None,
    metric_meta: Mapping[str, Mapping[str, Any]],
    sku_meta: Mapping[str, Mapping[str, Any]],
    snapshot: Mapping[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    lowered_query = (metric_query or "").casefold().strip()
    for table in _data_vitrina_tables(value):
        header = table.get("header") or []
        body = table.get("rows") or []
        if not isinstance(header, list) or not isinstance(body, list):
            continue
        try:
            label_idx = header.index("label")
            key_idx = header.index("key")
        except ValueError:
            continue
        date_columns = [
            (idx, str(name))
            for idx, name in enumerate(header)
            if isinstance(name, str) and _is_date_value(name)
        ]
        for row in body:
            if len(rows) > limit:
                return rows
            if not isinstance(row, list) or key_idx >= len(row):
                continue
            label = str(row[label_idx] if label_idx < len(row) and row[label_idx] is not None else "")
            projection_key = str(row[key_idx] or "").strip()
            if not projection_key:
                continue
            parsed = _parse_projection_key(projection_key)
            metric_key = str(parsed.get("metric_key") or "")
            if not metric_key:
                continue
            if metric_keys and metric_key not in metric_keys and projection_key not in metric_keys:
                continue
            meta = metric_meta.get(metric_key) or {}
            label_ru = str(meta.get("label_ru") or label or metric_key)
            if lowered_query and not _metric_text_matches(lowered_query, metric_key, projection_key, label_ru, meta):
                continue
            row_sku = str(parsed.get("sku_or_nm_id") or "")
            if sku_or_nm_id and row_sku != str(sku_or_nm_id):
                continue
            row_sku_meta = sku_meta.get(row_sku) if row_sku else {}
            for date_idx, date_key in date_columns:
                if date_from and date_key < date_from:
                    continue
                if date_to and date_key > date_to:
                    continue
                if date_idx >= len(row):
                    continue
                cell_value = _safe_metric_scalar(row[date_idx])
                if cell_value is None:
                    continue
                rows.append(
                    {
                        "date": date_key,
                        "metric_key": metric_key,
                        "label_ru": label_ru,
                        "scope": str(meta.get("scope") or parsed.get("scope") or ""),
                        "level": str(parsed.get("level") or "unknown"),
                        "sku_or_nm_id": row_sku or None,
                        "group_name": parsed.get("group_name") or row_sku_meta.get("group_name"),
                        "display_name": row_sku_meta.get("display_name") if row_sku_meta else None,
                        "value": cell_value,
                        "unit": str(meta.get("format_name") or ""),
                        "format_name": str(meta.get("format_name") or ""),
                        "section_name": str(meta.get("section_name") or ""),
                        "source_snapshot_id": snapshot.get("snapshot_id"),
                        "source_as_of_date": snapshot.get("as_of_date"),
                        "refreshed_at": snapshot.get("refreshed_at"),
                        "source_table": "sheet_vitrina_v1_ready_snapshots",
                        "projection_label": f"DATA_VITRINA[{projection_key}][{date_key}]",
                    }
                )
        if rows:
            return rows
    if not rows and metric_keys:
        for metric_key in metric_keys:
            legacy_rows = _extract_metric_values(value, metric_key, sku_or_nm_id)
            for legacy_row in legacy_rows:
                if len(rows) > limit:
                    break
                metric_value = _safe_metric_scalar(legacy_row.get("value"))
                if metric_value is None:
                    continue
                rows.append(
                    {
                        "date": str(snapshot.get("as_of_date") or ""),
                        "metric_key": metric_key,
                        "label_ru": str((metric_meta.get(metric_key) or {}).get("label_ru") or metric_key),
                        "scope": str((metric_meta.get(metric_key) or {}).get("scope") or ""),
                        "level": "legacy",
                        "sku_or_nm_id": legacy_row.get("nm_id") or legacy_row.get("sku"),
                        "group_name": None,
                        "value": metric_value,
                        "unit": str((metric_meta.get(metric_key) or {}).get("format_name") or ""),
                        "format_name": str((metric_meta.get(metric_key) or {}).get("format_name") or ""),
                        "section_name": str((metric_meta.get(metric_key) or {}).get("section_name") or ""),
                        "source_snapshot_id": snapshot.get("snapshot_id"),
                        "source_as_of_date": snapshot.get("as_of_date"),
                        "refreshed_at": snapshot.get("refreshed_at"),
                        "source_table": "sheet_vitrina_v1_ready_snapshots",
                        "projection_label": f"legacy_metrics[{metric_key}]",
                    }
                )
    return rows


def _data_vitrina_tables(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    result: list[dict[str, Any]] = []
    sheets = value.get("sheets")
    if isinstance(sheets, list):
        for sheet in sheets:
            if not isinstance(sheet, dict):
                continue
            name = str(sheet.get("sheet_name") or sheet.get("name") or sheet.get("title") or "")
            if name != "DATA_VITRINA":
                continue
            result.append(
                {
                    "header": sheet.get("header") or sheet.get("headers") or sheet.get("columns"),
                    "rows": sheet.get("rows") or sheet.get("data") or [],
                }
            )
    table = value.get("DATA_VITRINA")
    if isinstance(table, dict):
        result.append({"header": table.get("header") or table.get("headers") or table.get("columns"), "rows": table.get("rows") or []})
    return result


def _snapshot_metric_catalog(value: Any) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for table in _data_vitrina_tables(value):
        header = table.get("header") or []
        body = table.get("rows") or []
        if not isinstance(header, list) or not isinstance(body, list):
            continue
        try:
            label_idx = header.index("label")
            key_idx = header.index("key")
        except ValueError:
            continue
        date_indices = [idx for idx, name in enumerate(header) if isinstance(name, str) and _is_date_value(name)]
        for row in body:
            if not isinstance(row, list) or key_idx >= len(row):
                continue
            projection_key = str(row[key_idx] or "").strip()
            if not projection_key:
                continue
            parsed = _parse_projection_key(projection_key)
            metric_key = str(parsed.get("metric_key") or "")
            if not metric_key:
                continue
            item = catalog.setdefault(
                metric_key,
                {
                    "metric_key": metric_key,
                    "levels": [],
                    "sample_projection_keys": [],
                    "sample_labels": [],
                    "value_count": 0,
                },
            )
            level = str(parsed.get("level") or "unknown")
            if level not in item["levels"]:
                item["levels"].append(level)
            if projection_key not in item["sample_projection_keys"] and len(item["sample_projection_keys"]) < 5:
                item["sample_projection_keys"].append(projection_key)
            label = str(row[label_idx] if label_idx < len(row) and row[label_idx] is not None else "")
            if label and label not in item["sample_labels"] and len(item["sample_labels"]) < 5:
                item["sample_labels"].append(label)
            for idx in date_indices:
                if idx < len(row) and _safe_metric_scalar(row[idx]) is not None:
                    item["value_count"] += 1
    return catalog


def _snapshot_date_columns(value: Any, *, fallback: Any = None) -> list[str]:
    dates: list[str] = []
    if isinstance(value, dict):
        date_columns = value.get("date_columns")
        if isinstance(date_columns, list):
            for item in date_columns:
                text = str(item)
                if _is_date_value(text) and text not in dates:
                    dates.append(text)
        for table in _data_vitrina_tables(value):
            header = table.get("header") or []
            if isinstance(header, list):
                for item in header:
                    text = str(item)
                    if _is_date_value(text) and text not in dates:
                        dates.append(text)
    fallback_text = str(fallback or "")
    if not dates and _is_date_value(fallback_text):
        dates.append(fallback_text)
    return sorted(dates)


def _parse_projection_key(value: str) -> dict[str, Any]:
    context, sep, metric_key = value.partition("|")
    if not sep:
        return {"metric_key": value, "level": "unknown", "scope": "", "group_name": None, "sku_or_nm_id": None}
    if context == "TOTAL":
        return {"metric_key": metric_key, "level": "total", "scope": "total", "group_name": None, "sku_or_nm_id": None}
    if context.startswith("SKU:"):
        return {"metric_key": metric_key, "level": "sku", "scope": "sku", "group_name": None, "sku_or_nm_id": context.split(":", 1)[1]}
    if context.startswith("GROUP:"):
        return {"metric_key": metric_key, "level": "group", "scope": "group", "group_name": context.split(":", 1)[1], "sku_or_nm_id": None}
    return {"metric_key": metric_key, "level": context.lower() or "unknown", "scope": context.lower(), "group_name": None, "sku_or_nm_id": None}


def _safe_metric_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if len(text) > 240:
            return text[:240] + "...[truncated]"
        return _redact_string(text)
    return None


def _metric_text_matches(
    lowered_query: str,
    metric_key: str,
    projection_key: str,
    label_ru: str,
    meta: Mapping[str, Any],
) -> bool:
    haystack = " ".join(
        [
            metric_key,
            projection_key,
            label_ru,
            str(meta.get("section_name") or ""),
            str(meta.get("calc_ref") or ""),
        ]
    ).casefold()
    return lowered_query in haystack


def _dedupe_metric_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("date") or ""),
            str(row.get("metric_key") or ""),
            str(row.get("level") or ""),
            str(row.get("sku_or_nm_id") or ""),
            str(row.get("group_name") or ""),
            str(row.get("projection_label") or ""),
        )
        current = latest.get(key)
        if current is None or str(row.get("refreshed_at") or "") >= str(current.get("refreshed_at") or ""):
            latest[key] = row
    return list(latest.values())


def _group_metric_rows(rows: list[dict[str, Any]], group_by: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = row.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if group_by == "date":
            key = str(row.get("date") or "")
        elif group_by == "metric":
            key = str(row.get("metric_key") or "")
        elif group_by == "sku":
            key = str(row.get("sku_or_nm_id") or "total")
        elif group_by == "total":
            key = "total"
        else:
            key = "unknown"
        item = buckets.setdefault(key, {"key": key, "value": 0.0, "row_count": 0})
        item["value"] = float(item["value"]) + float(value)
        item["row_count"] = int(item["row_count"]) + 1
    return [buckets[key] for key in sorted(buckets.keys())]


def _snapshot_meta(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {}
    return {
        "as_of_date": snapshot.get("as_of_date"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "refreshed_at": snapshot.get("refreshed_at"),
    }


def _metric_selector_candidates(
    selector: str,
    registry: Mapping[str, Mapping[str, Any]],
    snapshot_catalog: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    lowered = selector.casefold()
    candidates: dict[str, dict[str, Any]] = {}
    for metric_key, meta in registry.items():
        if lowered in str(metric_key).casefold() or lowered in str(meta.get("label_ru") or "").casefold() or lowered in str(meta.get("section_name") or "").casefold():
            candidates[metric_key] = {
                "metric_key": metric_key,
                "label_ru": meta.get("label_ru") or metric_key,
                "scope": meta.get("scope") or "",
                "section_name": meta.get("section_name") or "",
                "format_name": meta.get("format_name") or "",
                "source": "registry_upload_metrics_v2",
            }
    for metric_key, coverage in snapshot_catalog.items():
        labels = coverage.get("sample_labels") or []
        if lowered in metric_key.casefold() or any(lowered in str(label).casefold() for label in labels):
            candidates.setdefault(
                metric_key,
                {
                    "metric_key": metric_key,
                    "label_ru": labels[0] if labels else metric_key,
                    "scope": ",".join(coverage.get("levels", [])),
                    "section_name": "",
                    "format_name": "",
                    "source": "sheet_vitrina_v1_ready_snapshots",
                },
            )
    return sorted(candidates.values(), key=lambda item: str(item.get("metric_key") or ""))[:50]


def _matches_metric_filter(item: Mapping[str, Any], *, query: str, section: str, scope: str) -> bool:
    if query:
        lowered = query.casefold()
        text = " ".join(
            [
                str(item.get("metric_key") or ""),
                str(item.get("label_ru") or ""),
                str(item.get("section_name") or ""),
                " ".join(str(key) for key in item.get("sample_projection_keys") or []),
                " ".join(str(label) for label in item.get("sample_labels") or []),
            ]
        ).casefold()
        if lowered not in text:
            return False
    if section and section.casefold() not in str(item.get("section_name") or "").casefold():
        return False
    if scope:
        scope_text = " ".join([str(item.get("scope") or ""), " ".join(str(level) for level in item.get("coverage_levels") or [])]).casefold()
        if scope.casefold() not in scope_text:
            return False
    return True


def _enabled_sort_value(value: Any) -> int:
    return 0 if str(value) in {"1", "True", "true"} else 1


def _is_date_value(value: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", value))


def _date_shift(value: str, days: int) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    return (parsed + timedelta(days=days)).isoformat()


def _metric_name_matches(metric_key: str, markers: tuple[str, ...]) -> bool:
    if not markers:
        return True
    lowered = metric_key.lower()
    return any(marker.lower() in lowered for marker in markers)


def _snapshot_missing_flags(snapshot: Mapping[str, Any] | None, metrics: list[dict[str, Any]]) -> list[str]:
    flags = []
    if snapshot is None:
        flags.append("ready_snapshot_missing")
    if snapshot is not None and not metrics:
        flags.append("stable_metric_projection_missing")
    return flags


def _estimate_row_count(result: Mapping[str, Any]) -> int:
    for key in ("rows", "results", "values"):
        value = result.get(key)
        if isinstance(value, list):
            return len(value)
    return 1


def _hash_json(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True)
    except TypeError:
        text = str(value)
    return _hash_text(text)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error_text(value: str) -> str:
    return _redact_string(re.sub(r"\s+", " ", value).strip()[:240])
