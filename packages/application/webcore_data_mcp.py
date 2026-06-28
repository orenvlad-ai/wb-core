"""Read-only business-data gateway for the WebCore Data MCP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Iterable, Mapping
from urllib.parse import quote

DB_FILENAME = "registry_upload_runtime.sqlite3"
DEFAULT_MAX_LIMIT = 50
MAX_LIMIT = 100
MAX_DATE_RANGE_DAYS = 62
AUDIT_SCHEMA_VERSION = "webcore_data_mcp_audit_v1"
SCOPE_ANALYTICS_READ = "webcore.analytics.read"
SCOPE_SUPPLY_READ = "webcore.supply.read"
SCOPE_FINANCE_READ = "webcore.finance.read"

APPROVED_TOOL_NAMES = (
    "get_data_freshness_status",
    "search_business_objects",
    "explain_metric_source",
    "get_wb_supplies_summary",
    "get_wb_supply_details",
    "rank_supplier_shipments_by_unit_cost",
    "get_supplier_shipment_details",
    "get_latest_factory_order_calculation",
    "get_stock_report",
    "get_sku_snapshot",
    "get_revenue_by_date",
    "get_revenue_range",
)

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
    ) -> None:
        if db_path is None:
            resolved_runtime_dir = runtime_dir or Path(
                os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", ".runtime/registry_upload")
            )
            db_path = resolved_runtime_dir / DB_FILENAME
        self.db_path = Path(db_path).expanduser()
        self.audit_log_path = Path(audit_log_path).expanduser() if audit_log_path else None
        self.max_limit = max(1, min(int(max_limit or DEFAULT_MAX_LIMIT), MAX_LIMIT))

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for definition in _tool_definitions():
            security_schemes = [{"type": "oauth2", "scopes": [definition.scope]}]
            tools.append(
                {
                    "name": definition.name,
                    "description": definition.description,
                    "inputSchema": definition.input_schema,
                    "outputSchema": definition.output_schema or _object_schema(),
                    "annotations": {"readOnlyHint": True},
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
    ) -> dict[str, Any]:
        args = dict(arguments or {})
        started = time.monotonic()
        status = "ok"
        row_count = 0
        try:
            if name not in APPROVED_TOOL_NAMES:
                raise WebCoreDataMcpError(f"tool is not allowlisted: {name}", code="tool_not_allowlisted")
            result = self._call_tool(name, args)
            row_count = _estimate_row_count(result)
            return _redact(result)
        except Exception:
            status = "error"
            raise
        finally:
            self._audit(
                {
                    "schema_version": AUDIT_SCHEMA_VERSION,
                    "at": _utc_now(),
                    "tool": name,
                    "identity_hash": _hash_text(identity) if identity else "",
                    "argument_keys": sorted(str(key) for key in args.keys()),
                    "arguments_hash": _hash_json(args),
                    "status": status,
                    "row_count": row_count,
                    "duration_ms": int((time.monotonic() - started) * 1000),
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
        if name == "get_data_freshness_status":
            return self.get_data_freshness_status()
        if name == "search_business_objects":
            return self.search_business_objects(
                query=_required_str(args, "query", max_length=120),
                object_types=_optional_str_list(args.get("object_types")),
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
            where = ""
            params: list[Any] = []
            if status_filter:
                where = "WHERE s.order_status = ?"
                params.append(status_filter)
            rows = conn.execute(
                f"""
                SELECT s.shipment_id, s.shipment_date, s.actual_shipment_date, s.actual_ff_acceptance_date,
                       s.order_status, s.currency, s.product_qty_total, s.product_amount_total,
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
            item = _row_dict(row)
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
            values = self._metric_values_for_date(conn, date_value, revenue_metric, sku_or_nm_id)
            return {
                "status": "ok" if values else "metric_projection_unavailable",
                "date": date_value,
                "sku_or_nm_id": sku_or_nm_id,
                "revenue_metric": revenue_metric,
                "values": values,
                "source": "sheet_vitrina_v1_ready_snapshots",
                "caveat": "" if values else "Metric was requested explicitly, but the MVP extractor could not find it in persisted ready snapshots without raw payload exposure.",
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
        buckets: dict[str, float] = {}
        values: list[dict[str, Any]] = []
        with self._connect() as conn:
            if not _table_exists(conn, "sheet_vitrina_v1_ready_snapshots"):
                return _missing_table_result("sheet_vitrina_v1_ready_snapshots")
            for row in conn.execute(
                """
                SELECT as_of_date, snapshot_id, refreshed_at, plan_json
                FROM sheet_vitrina_v1_ready_snapshots
                WHERE as_of_date >= ? AND as_of_date <= ?
                ORDER BY as_of_date
                """,
                (date_from, date_to),
            ).fetchall():
                date_key = str(row["as_of_date"])
                extracted = _extract_metric_values(_safe_json_loads(row["plan_json"]), revenue_metric, sku_or_nm_id=None)
                for item in extracted:
                    metric_value = item.get("value")
                    if not isinstance(metric_value, (int, float)):
                        continue
                    bucket = "total"
                    if group_by == "date":
                        bucket = date_key
                    elif group_by == "sku":
                        bucket = str(item.get("nm_id") or item.get("sku") or "unknown")
                    buckets[bucket] = buckets.get(bucket, 0.0) + float(metric_value)
                values.extend({"date": date_key, **item} for item in extracted[:50])
        return {
            "status": "ok" if buckets else "metric_projection_unavailable",
            "date_from": date_from,
            "date_to": date_to,
            "group_by": group_by,
            "revenue_metric": revenue_metric,
            "buckets": [{"key": key, "value": value} for key, value in sorted(buckets.items())],
            "sample_values": values[:50],
            "range_limit_days": MAX_DATE_RANGE_DAYS,
            "caveat": "" if buckets else "Metric was requested explicitly, but the MVP extractor could not find it in persisted ready snapshots without raw payload exposure.",
        }

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise WebCoreDataMcpError(f"runtime DB does not exist: {self.db_path}", code="runtime_db_missing")
        quoted_path = quote(str(self.db_path.resolve()), safe="/:")
        conn = sqlite3.connect(f"file:{quoted_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def _audit(self, payload: Mapping[str, Any]) -> None:
        if self.audit_log_path is None:
            return
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_redact(dict(payload)), ensure_ascii=True, sort_keys=True) + "\n")

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
        rows = conn.execute(
            """
            SELECT 'shipment' AS object_type, shipment_id AS id, shipment_id AS title,
                   shipment_date, order_status, invoice_no, supplier_name, match_status
            FROM sheet_vitrina_v1_supplier_shipments
            WHERE shipment_id LIKE ? OR invoice_no LIKE ? OR supplier_name LIKE ?
            ORDER BY shipment_date DESC, updated_at DESC
            LIMIT ?
            """,
            (like, like, like, limit),
        ).fetchall()
        return [_row_dict(row) for row in rows]

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
        row = conn.execute(
            """
            SELECT shipment_id, created_at, updated_at, shipment_date, actual_shipment_date,
                   actual_ff_acceptance_date, order_status, invoice_no, invoice_date, contract_no,
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
        item = _row_dict(row)
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

    def _metric_values_for_date(
        self,
        conn: sqlite3.Connection,
        date_value: str,
        metric_key: str,
        sku_or_nm_id: str | None,
    ) -> list[dict[str, Any]]:
        snapshot = self._ready_snapshot(conn, date_value)
        if not snapshot:
            return []
        values = _extract_metric_values(_safe_json_loads(snapshot.get("plan_json")), metric_key, sku_or_nm_id)
        for value in values:
            value["snapshot_id"] = snapshot.get("snapshot_id")
            value["refreshed_at"] = snapshot.get("refreshed_at")
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
        ToolDefinition("get_data_freshness_status", "Use this when the user asks whether WebCore data is fresh. Returns per-source freshness without triggering refresh/sync.", _schema({})),
        ToolDefinition("search_business_objects", "Use this to find SKU/nmId, nomenclature, shipment ids, WB supply ids, or metric keys by a bounded text query.", _schema({"query": _string_schema(1, 120), "object_types": _array_schema(_enum_schema(["sku", "nomenclature", "shipment", "wb_supply", "metric"]))}, required=["query"])),
        ToolDefinition("explain_metric_source", "Use this to explain where a metric comes from, its formula/reference and accepted freshness caveats.", _schema({"metric_key": _string_schema(1, 160)}, required=["metric_key"])),
        ToolDefinition("get_wb_supplies_summary", "Use this for cached-only WB supplies summaries. Never syncs or backfills upstream data.", _schema({"status_filter": _string_schema(0, 40), "date_from": _date_schema(), "date_to": _date_schema(), "limit": _int_schema(1, MAX_LIMIT)})),
        ToolDefinition("get_wb_supply_details", "Use this for one cached WB supply. Never fetches WB upstream and never exposes raw payload blobs.", _schema({"supply_id": _string_schema(1, 120)}, required=["supply_id"])),
        ToolDefinition("rank_supplier_shipments_by_unit_cost", "Use this to rank supplier shipments by available quantity/cost evidence with completeness flags.", _schema({"limit": _int_schema(1, MAX_LIMIT), "status_filter": _string_schema(0, 80)}), scope=SCOPE_SUPPLY_READ),
        ToolDefinition("get_supplier_shipment_details", "Use this for supplier shipment metadata, totals, document statuses and expense summaries. No raw files or paths.", _schema({"shipment_id": _string_schema(1, 120)}, required=["shipment_id"]), scope=SCOPE_SUPPLY_READ),
        ToolDefinition("get_latest_factory_order_calculation", "Use this for the latest factory-order and WB regional calculation state. Does not recalculate.", _schema({}), scope=SCOPE_SUPPLY_READ),
        ToolDefinition("get_stock_report", "Use this for persisted ready-side stock metrics for a date/SKU. Does not refresh data.", _schema({"date": _date_schema(), "sku_or_nm_id": _string_schema(0, 120)})),
        ToolDefinition("get_sku_snapshot", "Use this for SKU identity plus persisted ready snapshot metrics and freshness flags.", _schema({"sku_or_nm_id": _string_schema(1, 120), "date": _date_schema()}, required=["sku_or_nm_id"])),
        ToolDefinition("get_revenue_by_date", "Use this for date/SKU revenue only after the user chooses a revenue_metric. Without one, returns explicit ambiguity and candidates.", _schema({"date": _date_schema(), "sku_or_nm_id": _string_schema(0, 120), "revenue_metric": _string_schema(0, 160)}, required=["date"]), scope=SCOPE_FINANCE_READ),
        ToolDefinition("get_revenue_range", "Use this for bounded revenue ranges only after the user chooses a revenue_metric. Without one, returns explicit ambiguity and candidates.", _schema({"date_from": _date_schema(), "date_to": _date_schema(), "group_by": _enum_schema(["date", "sku", "total"]), "revenue_metric": _string_schema(0, 160)}, required=["date_from", "date_to"]), scope=SCOPE_FINANCE_READ),
    ]


def tool_required_scope(name: str) -> str:
    for definition in _tool_definitions():
        if definition.name == name:
            return definition.scope
    return SCOPE_ANALYTICS_READ


def _schema(properties: dict[str, Any], *, required: Iterable[str] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


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


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)).fetchone()
    return row is not None


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
        return [_redact(item) for item in value[:200]]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _redact_string(value: str) -> str:
    lowered = value.lower()
    if any(marker in lowered for marker in ("storage_state", "authorization:", "bearer ", "password=", "token=")):
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
