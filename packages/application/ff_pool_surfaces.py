"""Bounded Stage 3 read/API facade for the inert FF facility/pool contour.

The facade deliberately opens SQLite in query-only mode for every GET model.
It never initializes schema from a read.  Facility and document mutations are
thin calls into the Stage 1/2 contracts and remain fail closed until a writer
feature epoch has been configured by a later activation change.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from packages.application.ff_pool_documents import (
    ALIASES_TABLE,
    DOCUMENTS_TABLE,
    DOCUMENT_KINDS,
    DOCUMENT_LINES_TABLE,
    DOCUMENT_RELATIONS_TABLE,
    EXPENSE_LINES_TABLE,
    FfPoolDocumentError,
    FfPoolDocumentService,
    REQUESTS_TABLE,
    WORKFLOW_EVENTS_TABLE,
)
from packages.application.ff_pool_documents_xlsx import (
    FfPoolXlsxError,
    generate_china_acceptance_workbook,
    generate_inventory_workbook,
)
from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_CHANGES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
    LINES_TABLE,
    OPERATIONS_TABLE,
    PARITY_TABLE,
    POOLS,
    canonical_decimal_text,
    read_ff_pool_feature_state,
)
from packages.contracts.ff_pool_documents import DocumentIdentity


CONTRACT_NAME = "ff_facility_pool_surfaces_v1"
CONTRACT_VERSION = 1
MAX_PAGE_SIZE = 100
MAX_DETAIL_PAGE_SIZE = 200
MAX_GRAPH_NODES = 200
MAX_JSON_REQUEST_BYTES = 256 * 1024
FACILITY_CODE_PREFIX = "FF-"
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,119}")
SAFE_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
VISIBLE_DOCUMENT_KINDS = tuple(
    item for item in DOCUMENT_KINDS if item != "facility_pool_opening"
)
FBS_LIFECYCLE_CURRENT_TABLE = "sheet_vitrina_v1_ff_pool_fbs_lifecycle_current"
FBS_CUTOVER_MANIFESTS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_manifests"
DOCUMENT_LABELS_RU = {
    "china_acceptance": "Приёмка Китай → FF",
    "transfer_root": "Перемещение между складами",
    "transfer_shipment": "Отгрузка перемещения",
    "transfer_receipt": "Приёмка перемещения",
    "transfer_loss": "Потеря в перемещении",
    "transfer_discrepancy": "Расхождение / пересорт",
    "transfer_cancellation": "Отмена остатка перемещения",
    "pool_reallocation": "Перемещение FBO ↔ FBS",
    "pool_inventory": "Инвентаризация FBS/FBO",
    "inventory_surplus": "Излишек инвентаризации",
    "inventory_shortage": "Недостача инвентаризации",
    "pool_overhead": "Накладные расходы FBS/FBO",
    "correction": "Корректировка",
    "storno": "Сторно",
    "late_expense": "Поздний расход",
}
WORKFLOW_LABELS_RU = {
    "accepted": "Данные приняты",
    "processing": "Проверка",
    "blocked": "Проведение заблокировано",
    "ready": "Готово к проведению",
    "posted": "Документ проведён",
    "replay": "Распределение и пересчёт",
    "complete": "Завершено",
    "error": "Ошибка",
    "not_found": "Запрос не найден",
}
REQUIRED_TABLES = frozenset(
    {
        FACILITIES_TABLE,
        FACILITY_CHANGES_TABLE,
        FACILITY_PROFILES_TABLE,
        FEATURE_EPOCHS_TABLE,
        BALANCES_TABLE,
        PARITY_TABLE,
        OPERATIONS_TABLE,
        LINES_TABLE,
        REQUESTS_TABLE,
        ALIASES_TABLE,
        DOCUMENTS_TABLE,
        DOCUMENT_LINES_TABLE,
        EXPENSE_LINES_TABLE,
        DOCUMENT_RELATIONS_TABLE,
        WORKFLOW_EVENTS_TABLE,
    }
)


class FfPoolSurfaceError(ValueError):
    """Stable machine-readable boundary error."""

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


class FfPoolSurface:
    """Read models and guarded orchestration over Stage 1/2 services."""

    def __init__(
        self,
        *,
        db_path: Path,
        runtime_dir: Path,
        timestamp_factory: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.runtime_dir = Path(runtime_dir)
        self.timestamp_factory = timestamp_factory or _utc_now

    def capabilities(self, *, aggregate_revision: str = "") -> dict[str, Any]:
        with self._read() as conn:
            schema = self._schema(conn)
            feature = self._feature(conn, aggregate_revision=aggregate_revision, schema=schema)
        guided_activation = self._guided_acceptance_activation()
        payload = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready" if schema["available"] else "schema_absent",
            "feature": feature,
            "facility_management": {
                "available": bool(feature["writer_effective"]),
                "reason": "writer_epoch_enabled" if feature["writer_effective"] else "facility_pool_feature_off",
                "physical_delete_allowed": False,
                "server_owned_identity": True,
                "address_supported": False,
                "fixed_system_pools": list(POOLS),
                "multiple_facilities_per_city": True,
            },
            "reviewed_initial_setup": [
                {
                    "name": "FF Москва",
                    "city": "Москва",
                    "proposed_active": True,
                    "production_row_created": False,
                },
                {
                    "name": "FF Оренбург",
                    "city": "Оренбург",
                    "proposed_active": False,
                    "production_row_created": False,
                },
            ],
            "guided_acceptance": guided_activation,
            "document_actions": [
                {
                    "document_kind": kind,
                    "label_ru": DOCUMENT_LABELS_RU.get(kind, kind),
                    "enabled": bool(feature["writer_effective"]),
                }
                for kind in VISIBLE_DOCUMENT_KINDS
                if kind not in {"inventory_surplus", "inventory_shortage"}
            ],
            "document_kinds": [
                {
                    "document_kind": kind,
                    "label_ru": DOCUMENT_LABELS_RU.get(kind, kind),
                    "operator_action": kind not in {"inventory_surplus", "inventory_shortage"},
                }
                for kind in VISIBLE_DOCUMENT_KINDS
            ],
            "derived_document_kinds": ["inventory_surplus", "inventory_shortage"],
            "hidden_actions": ["facility_pool_opening"],
            "aggregate_contract": {
                "warehouse_key": "ff",
                "detail_is_explanatory": True,
                "detail_is_total_operand": False,
                "six_stages_unchanged": True,
                "disclaimer_ru": (
                    "FBS и FBO объясняют уже учтённый остаток склада FF и не прибавляются к итогу повторно."
                ),
            },
        }
        return _etagged(payload)

    def facilities_page(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        search: str = "",
        active: str = "all",
        aggregate_revision: str = "",
    ) -> dict[str, Any]:
        page, limit, offset = _page(page, limit)
        needle = _search(search)
        active_token = str(active or "all").strip().lower()
        if active_token not in {"all", "active", "inactive"}:
            raise FfPoolSurfaceError("invalid_active_filter", "active must be all, active or inactive")
        with self._read() as conn:
            schema = self._schema(conn)
            feature = self._feature(conn, aggregate_revision=aggregate_revision, schema=schema)
            if not schema["available"]:
                return _etagged(
                    self._empty_page("schema_absent", feature=feature, page=page, limit=limit)
                )
            clauses = ["1=1"]
            params: list[Any] = []
            if needle:
                clauses.append("(lower(f.name) LIKE ? ESCAPE '\\' OR lower(f.code) LIKE ? ESCAPE '\\' OR lower(COALESCE(profile.city,'')) LIKE ? ESCAPE '\\')")
                pattern = "%" + _like(needle.lower()) + "%"
                params.extend((pattern, pattern, pattern))
            if active_token != "all":
                clauses.append("f.active=?")
                params.append(1 if active_token == "active" else 0)
            where = " AND ".join(clauses)
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM {FACILITIES_TABLE} f WHERE {where}", params).fetchone()[0]
            )
            epoch = int(feature["epoch"])
            visible = 1 if feature["reader_effective"] else 0
            rows = conn.execute(
                f"""
                WITH pool_summary AS (
                    SELECT facility_id,
                           SUM(CASE WHEN ?=1 AND projection_epoch=? THEN quantity ELSE 0 END) AS quantity,
                           decimal_sum(CASE WHEN ?=1 AND projection_epoch=? THEN capital_rub ELSE '0' END) AS capital_rub,
                           COUNT(CASE WHEN ?=1 AND projection_epoch=? THEN 1 END) AS balance_count
                    FROM {BALANCES_TABLE} GROUP BY facility_id
                ), document_summary AS (
                    SELECT line.facility_id,COUNT(DISTINCT document.root_document_id) AS document_count
                    FROM {DOCUMENT_LINES_TABLE} line
                    JOIN {DOCUMENTS_TABLE} document ON document.document_id=line.document_id
                    WHERE line.facility_id IS NOT NULL GROUP BY line.facility_id
                )
                SELECT f.*,COALESCE(profile.city,'') AS city,COALESCE(pool.quantity,0) AS quantity,
                       COALESCE(pool.capital_rub,0) AS capital_rub,
                       COALESCE(pool.balance_count,0) AS balance_count,
                       COALESCE(document.document_count,0) AS document_count
                FROM {FACILITIES_TABLE} f
                LEFT JOIN {FACILITY_PROFILES_TABLE} profile ON profile.facility_id=f.facility_id
                LEFT JOIN pool_summary pool ON pool.facility_id=f.facility_id
                LEFT JOIN document_summary document ON document.facility_id=f.facility_id
                WHERE {where}
                ORDER BY f.active DESC,f.name COLLATE NOCASE,f.code,f.facility_id
                LIMIT ? OFFSET ?
                """,
                (visible, epoch, visible, epoch, visible, epoch, *params, limit, offset),
            ).fetchall()
        facilities = [_facility_public(row, detail_visible=bool(visible)) for row in rows]
        payload = {
            "contract_name": CONTRACT_NAME,
            "status": "ready",
            "feature": feature,
            "facilities": facilities,
            "page": _page_payload(page, limit, total),
            "filters": {"search": needle, "active": active_token},
            "aggregate_disclaimer_ru": (
                "Детализация FBS/FBO входит внутрь склада FF и не является дополнительным итогом."
            ),
        }
        return _etagged(payload)

    def facility_detail(self, facility_id: str, *, aggregate_revision: str = "") -> dict[str, Any]:
        selected = _identity_token(facility_id, field="facility_id")
        with self._read() as conn:
            schema = self._schema(conn)
            if not schema["available"]:
                raise FfPoolSurfaceError("schema_absent", "FF facility/pool schema is not available", http_status=503)
            feature = self._feature(conn, aggregate_revision=aggregate_revision, schema=schema)
            facility = conn.execute(
                f"""SELECT f.*,COALESCE(profile.city,'') AS city FROM {FACILITIES_TABLE} f
                    LEFT JOIN {FACILITY_PROFILES_TABLE} profile ON profile.facility_id=f.facility_id
                    WHERE f.facility_id=?""", (selected,)
            ).fetchone()
            if facility is None:
                raise FfPoolSurfaceError("facility_not_found", "Facility was not found", http_status=404)
            pools: list[dict[str, Any]] = []
            for pool in POOLS:
                if feature["reader_effective"]:
                    row = conn.execute(
                        f"""SELECT COUNT(*) AS balance_count,COALESCE(SUM(quantity),0) AS quantity,
                                   COALESCE(decimal_sum(capital_rub),'0') AS capital_rub
                            FROM {BALANCES_TABLE}
                            WHERE projection_epoch=? AND facility_id=? AND pool=?""",
                        (int(feature["epoch"]), selected, pool),
                    ).fetchone()
                    quantity = int(row["quantity"] or 0)
                    capital = _decimal(row["capital_rub"])
                    balance_count = int(row["balance_count"] or 0)
                else:
                    quantity, capital, balance_count = 0, Decimal("0"), 0
                reservation = (
                    _fbs_reservations(conn, facility_id=selected)
                    if pool == "FBS" and feature["reader_effective"]
                    else {"quantity": 0, "by_nm_id": {}, "updated_at": ""}
                )
                reserved_quantity = int(reservation["quantity"])
                available_quantity = (
                    quantity - reserved_quantity if feature["reader_effective"] else None
                )
                document_count = int(
                    conn.execute(
                        f"""SELECT COUNT(DISTINCT document.root_document_id)
                            FROM {DOCUMENT_LINES_TABLE} line
                            JOIN {DOCUMENTS_TABLE} document ON document.document_id=line.document_id
                            WHERE line.facility_id=? AND line.pool=?""",
                        (selected, pool),
                    ).fetchone()[0]
                )
                pools.append(
                    {
                        "pool": pool,
                        "quantity": quantity if feature["reader_effective"] else None,
                        "capital_rub": canonical_decimal_text(capital) if feature["reader_effective"] else None,
                        "wac_rub": _wac(capital, quantity) if feature["reader_effective"] else None,
                        "balance_count": balance_count,
                        "document_count": document_count,
                        "physical_quantity": quantity if feature["reader_effective"] else None,
                        "reservation_quantity": (
                            reserved_quantity if feature["reader_effective"] and pool == "FBS" else 0
                        ),
                        "available_quantity": available_quantity,
                        "available_is_signed": pool == "FBS",
                        "reservation_status": (
                            "exact_lifecycle" if pool == "FBS" else "not_applicable"
                        ),
                    }
                )
            audit_rows = conn.execute(
                f"""SELECT change_id,action,actor,changed_at
                    FROM {FACILITY_CHANGES_TABLE}
                    WHERE facility_id=? ORDER BY changed_at DESC,change_id DESC LIMIT 20""",
                (selected,),
            ).fetchall()
        payload = {
            "contract_name": CONTRACT_NAME,
            "status": "ready",
            "feature": feature,
            "facility": {
                "facility_id": str(facility["facility_id"]),
                "code": str(facility["code"]),
                "name": str(facility["name"]),
                "city": str(facility["city"] or ""),
                "active": bool(facility["active"]),
                "display_timezone": str(facility["display_timezone"]),
                "created_at": str(facility["created_at"]),
                "updated_at": str(facility["updated_at"]),
            },
            "pools": pools,
            "audit": [dict(row) for row in audit_rows],
            "document_context": {"facility_id": selected},
            "fbs_orders_context": {
                "facility_id": selected,
                "path": "/v1/sheet-vitrina-v1/warehouses/ff/facility-pools/fbs-orders",
            },
            "aggregate_disclaimer_ru": (
                "Сумма сегментов должна совпадать с агрегатом FF; сегменты не входят в TOTAL отдельно."
            ),
        }
        return _etagged(payload)

    def pool_detail(
        self,
        facility_id: str,
        pool: str,
        *,
        page: int = 1,
        limit: int = 50,
        search: str = "",
        aggregate_revision: str = "",
    ) -> dict[str, Any]:
        selected = _identity_token(facility_id, field="facility_id")
        selected_pool = _pool(pool)
        page, limit, offset = _page(page, limit, maximum=MAX_DETAIL_PAGE_SIZE)
        needle = _search(search)
        with self._read() as conn:
            schema = self._schema(conn)
            if not schema["available"]:
                raise FfPoolSurfaceError("schema_absent", "FF facility/pool schema is not available", http_status=503)
            feature = self._feature(conn, aggregate_revision=aggregate_revision, schema=schema)
            facility = conn.execute(
                f"SELECT facility_id,code,name,active,display_timezone FROM {FACILITIES_TABLE} WHERE facility_id=?",
                (selected,),
            ).fetchone()
            if facility is None:
                raise FfPoolSurfaceError("facility_not_found", "Facility was not found", http_status=404)
            if not feature["reader_effective"]:
                rows: Sequence[sqlite3.Row] = ()
                total = 0
            else:
                clauses = ["balance.projection_epoch=?", "balance.facility_id=?", "balance.pool=?"]
                params: list[Any] = [int(feature["epoch"]), selected, selected_pool]
                if needle:
                    pattern = "%" + _like(needle.lower()) + "%"
                    clauses.append(
                        "(CAST(balance.nm_id AS TEXT) LIKE ? OR lower(COALESCE(item.our_sku,'')) LIKE ? ESCAPE '\\' "
                        "OR lower(COALESCE(item.nomenclature_name,'')) LIKE ? ESCAPE '\\' "
                        "OR lower(COALESCE(item.barcode,'')) LIKE ? ESCAPE '\\')"
                    )
                    params.extend((pattern, pattern, pattern, pattern))
                where = " AND ".join(clauses)
                item_join = (
                    "LEFT JOIN sheet_vitrina_v1_nomenclature_items item ON item.nm_id=balance.nm_id"
                    if "sheet_vitrina_v1_nomenclature_items" in schema["tables"] else
                    "LEFT JOIN (SELECT NULL AS nm_id,NULL AS our_sku,NULL AS nomenclature_name,NULL AS barcode) item ON 0"
                )
                total = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {BALANCES_TABLE} balance {item_join} WHERE {where}", params
                    ).fetchone()[0]
                )
                rows = conn.execute(
                    f"""SELECT balance.nm_id,balance.quantity,balance.capital_rub,balance.wac_rub,
                               balance.source_watermark,balance.updated_at,
                               item.our_sku,item.nomenclature_name,item.barcode
                        FROM {BALANCES_TABLE} balance {item_join}
                        WHERE {where} ORDER BY balance.nm_id LIMIT ? OFFSET ?""",
                    (*params, limit, offset),
                ).fetchall()
            physical_total = (
                int(
                    conn.execute(
                        f"""SELECT COALESCE(SUM(quantity),0) FROM {BALANCES_TABLE}
                            WHERE projection_epoch=? AND facility_id=? AND pool=?""",
                        (int(feature["epoch"]), selected, selected_pool),
                    ).fetchone()[0]
                )
                if feature["reader_effective"]
                else None
            )
            document_count = int(
                conn.execute(
                    f"""SELECT COUNT(DISTINCT document.root_document_id)
                        FROM {DOCUMENT_LINES_TABLE} line
                        JOIN {DOCUMENTS_TABLE} document ON document.document_id=line.document_id
                        WHERE line.facility_id=? AND line.pool=?""",
                    (selected, selected_pool),
                ).fetchone()[0]
            )
            reservations = (
                _fbs_reservations(conn, facility_id=selected)
                if selected_pool == "FBS" and feature["reader_effective"]
                else {"quantity": 0, "by_nm_id": {}, "updated_at": ""}
            )
        balances = [
            {
                "nm_id": int(row["nm_id"]),
                "sku": str(row["our_sku"] or row["nm_id"]),
                "name": str(row["nomenclature_name"] or ""),
                "barcode": str(row["barcode"] or ""),
                "quantity": int(row["quantity"]),
                "physical_quantity": int(row["quantity"]),
                "reserved_quantity": int(
                    reservations["by_nm_id"].get(int(row["nm_id"]), 0)
                ),
                "available_quantity": int(row["quantity"])
                - int(reservations["by_nm_id"].get(int(row["nm_id"]), 0)),
                "available_is_signed": selected_pool == "FBS",
                "capital_rub": canonical_decimal_text(row["capital_rub"]),
                "wac_rub": str(row["wac_rub"] or "") or _wac(_decimal(row["capital_rub"]), int(row["quantity"])),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]
        payload = {
            "contract_name": CONTRACT_NAME,
            "status": "ready" if feature["reader_effective"] else "detail_unavailable",
            "feature": feature,
            "facility": dict(facility),
            "pool": selected_pool,
            "balances": balances,
            "page": _page_payload(page, limit, total),
            "document_count": document_count,
            "physical_quantity": (
                physical_total
            ),
            "reservation_quantity": (
                int(reservations["quantity"])
                if feature["reader_effective"] and selected_pool == "FBS"
                else 0
            ),
            "available_quantity": (
                int(physical_total) - int(reservations["quantity"])
                if physical_total is not None
                else None
            ),
            "available_is_signed": selected_pool == "FBS",
            "reservation_status": (
                "exact_lifecycle" if selected_pool == "FBS" else "not_applicable"
            ),
            "document_context": {"facility_id": selected, "pool": selected_pool},
            "fbs_orders_context": (
                {
                    "facility_id": selected,
                    "path": "/v1/sheet-vitrina-v1/warehouses/ff/facility-pools/fbs-orders",
                }
                if selected_pool == "FBS"
                else None
            ),
        }
        return _etagged(payload)

    def documents_page(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        facility_id: str = "",
        pool: str = "",
        document_kind: str = "all",
        workflow_state: str = "all",
        business_date_from: str = "",
        business_date_to: str = "",
        search: str = "",
    ) -> dict[str, Any]:
        page, limit, offset = _page(page, limit)
        selected_facility = str(facility_id or "").strip()
        selected_pool = str(pool or "").strip().upper()
        if selected_facility:
            _identity_token(selected_facility, field="facility_id")
        if selected_pool:
            selected_pool = _pool(selected_pool)
        kind = str(document_kind or "all").strip()
        if kind != "all" and kind not in VISIBLE_DOCUMENT_KINDS:
            raise FfPoolSurfaceError("invalid_document_kind", "Unsupported document kind filter")
        state = str(workflow_state or "all").strip()
        if state != "all" and state not in WORKFLOW_LABELS_RU:
            raise FfPoolSurfaceError("invalid_workflow_state", "Unsupported workflow state filter")
        date_from = _date(business_date_from, field="business_date_from", optional=True)
        date_to = _date(business_date_to, field="business_date_to", optional=True)
        if date_from and date_to and date_from > date_to:
            raise FfPoolSurfaceError("invalid_date_range", "business_date_from must not exceed business_date_to")
        needle = _search(search)
        with self._read() as conn:
            schema = self._schema(conn)
            feature = self._feature(conn, aggregate_revision="", schema=schema)
            if not schema["available"]:
                return _etagged(
                    {
                        "contract_name": CONTRACT_NAME,
                        "status": "schema_absent",
                        "feature": feature,
                        "documents": [],
                        "page": _page_payload(page, limit, 0),
                    }
                )
            clauses = ["root.document_kind <> 'facility_pool_opening'"]
            params: list[Any] = []
            if kind != "all":
                clauses.append(
                    f"EXISTS(SELECT 1 FROM {DOCUMENTS_TABLE} kd WHERE kd.root_document_id=root.root_document_id AND kd.document_kind=?)"
                )
                params.append(kind)
            if state != "all":
                clauses.append(
                    f"EXISTS(SELECT 1 FROM {DOCUMENTS_TABLE} sd JOIN {REQUESTS_TABLE} sr ON sr.request_id=sd.request_id "
                    "WHERE sd.root_document_id=root.root_document_id AND sr.state=?)"
                )
                params.append(state)
            if selected_facility or selected_pool:
                context = ["line.root_document_id=root.root_document_id"]
                if selected_facility:
                    context.append("line.facility_id=?")
                    params.append(selected_facility)
                if selected_pool:
                    context.append("line.pool=?")
                    params.append(selected_pool)
                clauses.append(
                    f"EXISTS(SELECT 1 FROM {DOCUMENT_LINES_TABLE} line WHERE {' AND '.join(context)})"
                )
            if date_from:
                clauses.append("root.business_date>=?")
                params.append(date_from)
            if date_to:
                clauses.append("root.business_date<=?")
                params.append(date_to)
            if needle:
                pattern = "%" + _like(needle.lower()) + "%"
                clauses.append(
                    "(lower(root.document_id) LIKE ? ESCAPE '\\' OR lower(root.source_id) LIKE ? ESCAPE '\\' "
                    "OR lower(root.source_type) LIKE ? ESCAPE '\\' "
                    f"OR EXISTS(SELECT 1 FROM {DOCUMENTS_TABLE} child WHERE child.root_document_id=root.root_document_id "
                    "AND lower(child.document_id) LIKE ? ESCAPE '\\') "
                    f"OR EXISTS(SELECT 1 FROM {DOCUMENT_LINES_TABLE} sl WHERE sl.root_document_id=root.root_document_id "
                    "AND CAST(sl.nm_id AS TEXT) LIKE ?))"
                )
                params.extend((pattern, pattern, pattern, pattern, pattern))
            where = " AND ".join(clauses)
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {DOCUMENTS_TABLE} root WHERE root.document_id=root.root_document_id AND {where}",
                    params,
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""
                WITH page_roots AS (
                    SELECT root.root_document_id,root.document_id,root.document_kind,root.business_date,
                           root.posted_at,root.source_type,root.source_id,root.actor,root.request_id
                    FROM {DOCUMENTS_TABLE} root
                    WHERE root.document_id=root.root_document_id AND {where}
                    ORDER BY root.business_date DESC,root.posted_at DESC,root.root_document_id DESC
                    LIMIT ? OFFSET ?
                ), document_stats AS (
                    SELECT document.root_document_id,COUNT(*) AS document_count
                    FROM {DOCUMENTS_TABLE} document JOIN page_roots page USING(root_document_id)
                    GROUP BY document.root_document_id
                ), line_stats AS (
                    SELECT line.root_document_id,COUNT(*) AS line_count,
                           COALESCE(SUM(line.quantity),0) AS quantity,
                           COALESCE(decimal_sum(line.capital_rub),'0') AS capital_rub
                    FROM {DOCUMENT_LINES_TABLE} line JOIN page_roots page USING(root_document_id)
                    GROUP BY line.root_document_id
                ), expense_stats AS (
                    SELECT document.root_document_id,COALESCE(decimal_sum(expense.amount_rub),'0') AS expense_rub
                    FROM {EXPENSE_LINES_TABLE} expense
                    JOIN {DOCUMENTS_TABLE} document ON document.document_id=expense.document_id
                    JOIN page_roots page ON page.root_document_id=document.root_document_id
                    GROUP BY document.root_document_id
                )
                SELECT page.*,request.state,
                       COALESCE(document_stats.document_count,0) AS document_count,
                       COALESCE(line_stats.line_count,0) AS line_count,
                       COALESCE(line_stats.quantity,0) AS quantity,
                       COALESCE(line_stats.capital_rub,0) AS capital_rub,
                       COALESCE(expense_stats.expense_rub,0) AS expense_rub
                FROM page_roots page
                JOIN {REQUESTS_TABLE} request ON request.request_id=page.request_id
                LEFT JOIN document_stats USING(root_document_id)
                LEFT JOIN line_stats USING(root_document_id)
                LEFT JOIN expense_stats USING(root_document_id)
                ORDER BY page.business_date DESC,page.posted_at DESC,page.root_document_id DESC
                """,
                (*params, limit, offset),
            ).fetchall()
        documents = [
            {
                "root_document_id": str(row["root_document_id"]),
                "document_kind": str(row["document_kind"]),
                "document_label_ru": DOCUMENT_LABELS_RU.get(str(row["document_kind"]), str(row["document_kind"])),
                "business_date": str(row["business_date"]),
                "posted_at": str(row["posted_at"]),
                "state": str(row["state"]),
                "state_label_ru": WORKFLOW_LABELS_RU.get(str(row["state"]), str(row["state"])),
                "source": {"type": str(row["source_type"]), "id": str(row["source_id"])},
                "document_count": int(row["document_count"]),
                "line_count": int(row["line_count"]),
                "evidence_quantity": int(row["quantity"]),
                "evidence_capital_rub": canonical_decimal_text(row["capital_rub"]),
                "expense_rub": canonical_decimal_text(row["expense_rub"]),
                "summary_semantics": "document_evidence_non_additive",
            }
            for row in rows
        ]
        payload = {
            "contract_name": CONTRACT_NAME,
            "status": "ready",
            "feature": feature,
            "documents": documents,
            "page": _page_payload(page, limit, total),
            "filters": {
                "facility_id": selected_facility,
                "pool": selected_pool,
                "document_kind": kind,
                "workflow_state": state,
                "business_date_from": date_from,
                "business_date_to": date_to,
                "search": needle,
            },
        }
        return _etagged(payload)

    def document_detail(self, document_id: str) -> dict[str, Any]:
        selected = _identity_token(document_id, field="document_id")
        with self._read() as conn:
            self._require_schema(conn)
            target = conn.execute(
                f"SELECT root_document_id FROM {DOCUMENTS_TABLE} WHERE document_id=?", (selected,)
            ).fetchone()
            if target is None:
                raise FfPoolSurfaceError("document_not_found", "Document was not found", http_status=404)
            root_id = str(target["root_document_id"])
            rows = conn.execute(
                f"""SELECT document.document_id,document.document_role,document.document_kind,
                           document.business_date,document.posted_at,document.source_type,
                           document.source_id,document.actor,request.state,
                           COUNT(DISTINCT line.line_no) AS line_count,
                           COUNT(DISTINCT expense.expense_line_no) AS expense_count
                    FROM {DOCUMENTS_TABLE} document
                    JOIN {REQUESTS_TABLE} request ON request.request_id=document.request_id
                    LEFT JOIN {DOCUMENT_LINES_TABLE} line ON line.document_id=document.document_id
                    LEFT JOIN {EXPENSE_LINES_TABLE} expense ON expense.document_id=document.document_id
                    WHERE document.root_document_id=?
                    GROUP BY document.document_id
                    ORDER BY document.posted_at,document.document_id LIMIT ?""",
                (root_id, MAX_GRAPH_NODES),
            ).fetchall()
        payload = {
            "contract_name": CONTRACT_NAME,
            "status": "ready",
            "root_document_id": root_id,
            "requested_document_id": selected,
            "documents": [
                {
                    **dict(row),
                    "document_label_ru": DOCUMENT_LABELS_RU.get(str(row["document_kind"]), str(row["document_kind"])),
                    "state_label_ru": WORKFLOW_LABELS_RU.get(str(row["state"]), str(row["state"])),
                    "detail_loaded": False,
                }
                for row in rows
            ],
            "lazy": {"lines": True, "expenses": True, "relations": True, "graph": True},
        }
        return _etagged(payload)

    def document_lines(self, document_id: str, *, page: int = 1, limit: int = 100) -> dict[str, Any]:
        page, limit, offset = _page(page, limit, maximum=MAX_DETAIL_PAGE_SIZE)
        selected = _identity_token(document_id, field="document_id")
        with self._read() as conn:
            self._require_schema(conn)
            if not conn.execute(f"SELECT 1 FROM {DOCUMENTS_TABLE} WHERE document_id=?", (selected,)).fetchone():
                raise FfPoolSurfaceError("document_not_found", "Document was not found", http_status=404)
            total = int(conn.execute(f"SELECT COUNT(*) FROM {DOCUMENT_LINES_TABLE} WHERE document_id=?", (selected,)).fetchone()[0])
            rows = conn.execute(
                f"""SELECT document_id,line_no,line_role,facility_id,pool,nm_id,quantity,
                           capital_rub,expense_rub
                    FROM {DOCUMENT_LINES_TABLE} WHERE document_id=?
                    ORDER BY line_no LIMIT ? OFFSET ?""",
                (selected, limit, offset),
            ).fetchall()
        return _etagged(
            {
                "contract_name": CONTRACT_NAME,
                "status": "ready",
                "document_id": selected,
                "lines": [dict(row) for row in rows],
                "page": _page_payload(page, limit, total),
            }
        )

    def document_expenses(self, document_id: str, *, page: int = 1, limit: int = 100) -> dict[str, Any]:
        page, limit, offset = _page(page, limit, maximum=MAX_DETAIL_PAGE_SIZE)
        selected = _identity_token(document_id, field="document_id")
        with self._read() as conn:
            self._require_schema(conn)
            if not conn.execute(f"SELECT 1 FROM {DOCUMENTS_TABLE} WHERE document_id=?", (selected,)).fetchone():
                raise FfPoolSurfaceError("document_not_found", "Document was not found", http_status=404)
            total = int(conn.execute(f"SELECT COUNT(*) FROM {EXPENSE_LINES_TABLE} WHERE document_id=?", (selected,)).fetchone()[0])
            rows = conn.execute(
                f"""SELECT document_id,expense_line_no,amount_rub,basis,
                           source_file_sha256,source_filename
                    FROM {EXPENSE_LINES_TABLE} WHERE document_id=?
                    ORDER BY expense_line_no LIMIT ? OFFSET ?""",
                (selected, limit, offset),
            ).fetchall()
        return _etagged(
            {
                "contract_name": CONTRACT_NAME,
                "status": "ready",
                "document_id": selected,
                "expenses": [dict(row) for row in rows],
                "page": _page_payload(page, limit, total),
            }
        )

    def document_relations(self, document_id: str) -> dict[str, Any]:
        selected = _identity_token(document_id, field="document_id")
        with self._read() as conn:
            self._require_schema(conn)
            target = conn.execute(f"SELECT root_document_id FROM {DOCUMENTS_TABLE} WHERE document_id=?", (selected,)).fetchone()
            if target is None:
                raise FfPoolSurfaceError("document_not_found", "Document was not found", http_status=404)
            root_id = str(target["root_document_id"])
            rows = conn.execute(
                f"""SELECT parent_document_id,child_document_id,relation_type,created_at
                    FROM {DOCUMENT_RELATIONS_TABLE} WHERE root_document_id=?
                    ORDER BY created_at,parent_document_id,child_document_id LIMIT ?""",
                (root_id, MAX_GRAPH_NODES),
            ).fetchall()
        return _etagged(
            {
                "contract_name": CONTRACT_NAME,
                "status": "ready",
                "root_document_id": root_id,
                "relations": [dict(row) for row in rows],
            }
        )

    def document_graph(self, document_id: str) -> dict[str, Any]:
        detail = self.document_detail(document_id)
        relations = self.document_relations(document_id)
        payload = {
            "contract_name": CONTRACT_NAME,
            "status": "ready",
            "root_document_id": detail["root_document_id"],
            "nodes": [
                {
                    "document_id": item["document_id"],
                    "document_kind": item["document_kind"],
                    "document_label_ru": item["document_label_ru"],
                    "business_date": item["business_date"],
                    "state": item["state"],
                }
                for item in detail["documents"]
            ],
            "edges": relations["relations"],
            "bounded": True,
            "max_nodes": MAX_GRAPH_NODES,
        }
        return _etagged(payload)

    def request_status(self, request_id: str) -> dict[str, Any]:
        selected = _identity_token(request_id, field="request_id")
        with self._read() as conn:
            schema = self._schema(conn)
            feature = self._feature(conn, aggregate_revision="", schema=schema)
            if not schema["available"]:
                return _etagged(self._not_found_request(selected, feature=feature))
            canonical = self._resolve_request(conn, selected)
            if not canonical:
                return _etagged(self._not_found_request(selected, feature=feature))
            row = conn.execute(f"SELECT * FROM {REQUESTS_TABLE} WHERE request_id=?", (canonical,)).fetchone()
            if row is None:
                return _etagged(self._not_found_request(selected, feature=feature))
            events = conn.execute(
                f"""SELECT stage,status,occurred_at,duration_ms
                    FROM {WORKFLOW_EVENTS_TABLE}
                    WHERE action_type='facility_pool_document' AND identity=?
                    ORDER BY occurred_at,event_id LIMIT 50""",
                (canonical,),
            ).fetchall()
        state = str(row["state"])
        preview = _json_object(row["preview_manifest_json"])
        preview_summary = {}
        if str(row["document_kind"]) == "china_acceptance":
            allocations = [dict(item) for item in preview.get("allocations") or []]
            preview_summary = {
                "expected_quantity": sum(int(item.get("expected_quantity") or 0) for item in allocations),
                "accepted_quantity": sum(int(item.get("accepted_quantity") or 0) for item in allocations),
                "quantity_fbs": sum(int(item.get("quantity_fbs") or 0) for item in allocations),
                "quantity_fbo": sum(int(item.get("quantity_fbo") or 0) for item in allocations),
                "discrepancy_quantity": sum(int(item.get("discrepancy_quantity") or 0) for item in allocations),
            }
        guided_activation = self._guided_acceptance_activation()
        payload = {
            "contract_name": CONTRACT_NAME,
            "workflow_contract": "ff_document_workflow_v1",
            "status": "ready",
            "feature": feature,
            "request_id": canonical,
            "client_request_id": str(row["client_request_id"]),
            "document_kind": str(row["document_kind"]),
            "document_label_ru": DOCUMENT_LABELS_RU.get(str(row["document_kind"]), str(row["document_kind"])),
            "state": state,
            "state_label_ru": WORKFLOW_LABELS_RU.get(state, state),
            "confirm_allowed": state == "ready" and bool(feature["writer_effective"])
            and (str(row["document_kind"]) != "china_acceptance" or guided_activation["effective"]),
            "feature_blocked": not bool(feature["writer_effective"]),
            "guided_acceptance_activation": guided_activation
            if str(row["document_kind"]) == "china_acceptance" else None,
            "business_date": str(row["business_date"]),
            "source": {
                "type": str(row["source_type"]),
                "id": str(row["source_id"]),
                "filename": str(row["source_filename"]),
                "sha256": str(row["source_sha256"]),
            },
            "preview": {
                "available": bool(preview),
                "collection_keys": [key for key, value in preview.items() if isinstance(value, list)],
                "collection_counts": {key: len(value) for key, value in preview.items() if isinstance(value, list)},
                "summary": preview_summary,
            },
            "document": {
                "document_id": str(row["posted_document_id"]),
                "manifest_sha256": str(row["posted_manifest_sha256"]),
            } if str(row["posted_document_id"] or "") else None,
            "recovery_operation_id": str(row["recovery_operation_id"]),
            "error": {"code": str(row["error_code"]), "details": _json_value(row["error_details_json"], None)},
            "accepted_at": str(row["accepted_at"]),
            "updated_at": str(row["updated_at"]),
            "events": [dict(item) for item in events],
            "steps": _workflow_steps(state),
        }
        return _etagged(payload)

    def request_preview(
        self,
        request_id: str,
        *,
        collection: str = "",
        page: int = 1,
        limit: int = 100,
    ) -> dict[str, Any]:
        selected = _identity_token(request_id, field="request_id")
        page, limit, offset = _page(page, limit, maximum=MAX_DETAIL_PAGE_SIZE)
        with self._read() as conn:
            self._require_schema(conn)
            canonical = self._resolve_request(conn, selected)
            if not canonical:
                raise FfPoolSurfaceError("request_not_found", "Document request was not found", http_status=404)
            row = conn.execute(
                f"SELECT document_kind,state,preview_manifest_json FROM {REQUESTS_TABLE} WHERE request_id=?",
                (canonical,),
            ).fetchone()
        manifest = _json_object(row["preview_manifest_json"])
        collections = {key: value for key, value in manifest.items() if isinstance(value, list)}
        selected_collection = str(collection or "").strip()
        if not selected_collection and collections:
            selected_collection = next(iter(collections))
        if selected_collection and selected_collection not in collections:
            raise FfPoolSurfaceError("preview_collection_not_found", "Preview collection was not found", http_status=404)
        rows = list(collections.get(selected_collection, []))
        scalar = {
            key: value
            for key, value in manifest.items()
            if not isinstance(value, (list, dict)) and key not in {"source_file_blob"}
        }
        payload = {
            "contract_name": CONTRACT_NAME,
            "status": "ready",
            "request_id": canonical,
            "document_kind": str(row["document_kind"]),
            "state": str(row["state"]),
            "summary": scalar,
            "collection": selected_collection,
            "rows": rows[offset : offset + limit],
            "page": _page_payload(page, limit, len(rows)),
            "available_collections": {key: len(value) for key, value in collections.items()},
        }
        return _etagged(payload)

    def source_file(self, document_id: str) -> tuple[bytes, str, str]:
        selected = _identity_token(document_id, field="document_id")
        with self._read() as conn:
            self._require_schema(conn)
            row = conn.execute(
                f"""SELECT request.source_file_blob,request.source_filename,request.source_content_type
                    FROM {DOCUMENTS_TABLE} document
                    JOIN {REQUESTS_TABLE} request ON request.request_id=document.request_id
                    WHERE document.document_id=?""",
                (selected,),
            ).fetchone()
        if row is None or row["source_file_blob"] is None:
            raise FfPoolSurfaceError("source_file_not_found", "Document source file was not found", http_status=404)
        return bytes(row["source_file_blob"]), str(row["source_filename"] or "document.xlsx"), str(row["source_content_type"] or "application/octet-stream")

    def create_facility(self, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        request_id = _request_id(payload.get("request_id"))
        name = _text(payload.get("name"), field="name", maximum=200)
        city_raw = str(payload.get("city") or "").strip()
        city = _text(city_raw, field="city", maximum=120) if city_raw else ""
        display_timezone = _timezone(payload.get("display_timezone") or "Asia/Yekaterinburg")
        active = _boolean(payload.get("active", True), field="active")
        request_identity = _fingerprint({"action": "create", "name": name, "city": city, "display_timezone": display_timezone, "active": active})
        self._require_writer()
        facility_digest = _fingerprint({"request_id": request_id, "purpose": "facility_identity"}).removeprefix("sha256:")
        facility_id = "fff_" + facility_digest[:28]
        code = FACILITY_CODE_PREFIX + facility_digest[:10].upper()
        now = self._now()
        with _connect_write(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing_change = conn.execute(
                f"SELECT request_identity,facility_id FROM {FACILITY_CHANGES_TABLE} WHERE request_id=? LIMIT 1",
                (request_id,),
            ).fetchone()
            if existing_change is not None:
                if str(existing_change["request_identity"]) != request_identity:
                    raise FfPoolSurfaceError("request_id_identity_conflict", "request_id was already used for another facility change", http_status=409)
                conn.rollback()
                return {**self.facility_detail(str(existing_change["facility_id"])), "idempotent": True}
            current = {
                "facility_id": facility_id,
                "code": code,
                "name": name,
                "city": city,
                "active": active,
                "display_timezone": display_timezone,
            }
            conn.execute(
                f"""INSERT INTO {FACILITIES_TABLE}(facility_id,code,name,active,display_timezone,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?)""",
                (facility_id, code, name, int(active), display_timezone, now, now),
            )
            conn.execute(
                f"""INSERT INTO {FACILITY_PROFILES_TABLE}(
                       facility_id,city,future_fields_json,created_at,updated_at
                   ) VALUES(?,?,'{{}}',?,?)""",
                (facility_id, city, now, now),
            )
            self._append_facility_change(
                conn,
                request_id=request_id,
                request_identity=request_identity,
                facility_id=facility_id,
                action="created",
                actor=actor,
                previous={},
                current=current,
                changed_at=now,
            )
            conn.commit()
        return {**self.facility_detail(facility_id), "idempotent": False}

    def update_facility(self, facility_id: str, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        selected = _identity_token(facility_id, field="facility_id")
        request_id = _request_id(payload.get("request_id"))
        expected = str(payload.get("expected_updated_at") or "").strip()
        if not expected:
            raise FfPoolSurfaceError("expected_updated_at_required", "expected_updated_at is required", http_status=409)
        allowed = {"name", "active", "display_timezone"}
        changes = {key: payload[key] for key in allowed if key in payload}
        if not changes:
            raise FfPoolSurfaceError("facility_change_required", "At least one facility field is required")
        normalized: dict[str, Any] = {}
        if "name" in changes:
            normalized["name"] = _text(changes["name"], field="name", maximum=200)
        if "active" in changes:
            normalized["active"] = _boolean(changes["active"], field="active")
        if "display_timezone" in changes:
            normalized["display_timezone"] = _timezone(changes["display_timezone"])
        request_identity = _fingerprint({"action": "update", "facility_id": selected, "expected": expected, "changes": normalized})
        self._require_writer()
        now = self._now()
        with _connect_write(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                f"SELECT request_identity,facility_id FROM {FACILITY_CHANGES_TABLE} WHERE request_id=? LIMIT 1",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["request_identity"]) != request_identity:
                    raise FfPoolSurfaceError("request_id_identity_conflict", "request_id was already used for another facility change", http_status=409)
                conn.rollback()
                return {**self.facility_detail(str(existing["facility_id"])), "idempotent": True}
            row = conn.execute(
                f"""SELECT f.*,COALESCE(profile.city,'') AS city FROM {FACILITIES_TABLE} f
                    LEFT JOIN {FACILITY_PROFILES_TABLE} profile ON profile.facility_id=f.facility_id
                    WHERE f.facility_id=?""", (selected,)
            ).fetchone()
            if row is None:
                raise FfPoolSurfaceError("facility_not_found", "Facility was not found", http_status=404)
            if str(row["updated_at"]) != expected:
                raise FfPoolSurfaceError(
                    "facility_version_changed",
                    "Facility changed after the form was opened",
                    details={"current_updated_at": str(row["updated_at"])},
                    http_status=409,
                )
            before = {
                "facility_id": str(row["facility_id"]),
                "code": str(row["code"]),
                "name": str(row["name"]),
                "city": str(row["city"] or ""),
                "active": bool(row["active"]),
                "display_timezone": str(row["display_timezone"]),
            }
            current = {**before, **normalized}
            if current == before:
                conn.rollback()
                return {**self.facility_detail(selected), "idempotent": True}
            if before["active"] and not current["active"]:
                blockers = self._facility_deactivation_blockers(conn, selected)
                if blockers["has_unfinished_dependencies"]:
                    raise FfPoolSurfaceError(
                        "facility_deactivation_blocked",
                        "Facility has unfinished dependencies or a non-zero pool balance",
                        details=blockers,
                        http_status=409,
                    )
            conn.execute(
                f"UPDATE {FACILITIES_TABLE} SET name=?,active=?,display_timezone=?,updated_at=? WHERE facility_id=? AND updated_at=?",
                (current["name"], int(current["active"]), current["display_timezone"], now, selected, expected),
            )
            action_rows: list[tuple[str, str]] = []
            if current["name"] != before["name"]:
                action_rows.append(("renamed", "name"))
            if current["active"] != before["active"]:
                action_rows.append(("activated" if current["active"] else "deactivated", "active"))
            if current["display_timezone"] != before["display_timezone"]:
                action_rows.append(("timezone_changed", "display_timezone"))
            for action, _field in action_rows:
                self._append_facility_change(
                    conn,
                    request_id=request_id,
                    request_identity=request_identity,
                    facility_id=selected,
                    action=action,
                    actor=actor,
                    previous=before,
                    current=current,
                    changed_at=now,
                )
            conn.commit()
        return {**self.facility_detail(selected), "idempotent": False}

    def _facility_deactivation_blockers(
        self, conn: sqlite3.Connection, facility_id: str
    ) -> dict[str, Any]:
        pending_states = ("accepted", "processing", "blocked", "ready", "posted", "replay")
        pending_requests = int(
            conn.execute(
                f"""SELECT COUNT(*) FROM {REQUESTS_TABLE}
                    WHERE state IN ({','.join('?' for _ in pending_states)})
                      AND instr(preview_manifest_json, ?) > 0""",
                (*pending_states, json.dumps(facility_id, ensure_ascii=False)),
            ).fetchone()[0]
        )
        nonzero_balances = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {BALANCES_TABLE} WHERE facility_id=? AND quantity<>0",
                (facility_id,),
            ).fetchone()[0]
        )
        return {
            "pending_request_count": pending_requests,
            "nonzero_balance_count": nonzero_balances,
            "has_unfinished_dependencies": bool(pending_requests or nonzero_balances),
        }

    def accept_document_preview(
        self,
        payload: Mapping[str, Any],
        *,
        actor: str,
    ) -> dict[str, Any]:
        self._require_writer()
        kind = str(payload.get("document_kind") or "").strip()
        if kind not in VISIBLE_DOCUMENT_KINDS or kind in {"inventory_surplus", "inventory_shortage"}:
            raise FfPoolSurfaceError("invalid_document_kind", "Document kind is not available in the operator flow")
        if kind == "china_acceptance":
            raise FfPoolSurfaceError(
                "guided_acceptance_required",
                "China acceptance is available only through the guided workbook workflow",
                http_status=409,
            )
        request_id = _request_id(payload.get("request_id"))
        business_date = _date(payload.get("business_date"), field="business_date")
        manifest = payload.get("manifest")
        if not isinstance(manifest, Mapping):
            raise FfPoolSurfaceError("manifest_required", "manifest must be an object")
        semantic = {"document_kind": kind, "business_date": business_date, "manifest": dict(manifest)}
        revision = _fingerprint(semantic)
        identity = DocumentIdentity(
            request_id=request_id,
            source_system="operator_http",
            source_type=f"ff_pool_document:{kind}",
            source_id=f"{kind}:{revision.removeprefix('sha256:')[:24]}",
            source_revision=revision,
            idempotency_epoch=self._writer_epoch(),
            actor=_actor(actor),
            business_date=business_date,
        )
        try:
            result = self._service().accept_preview(
                identity=identity,
                document_kind=kind,
                manifest=dict(manifest),
            )
        except FfPoolDocumentError as exc:
            raise _surface_from_document_error(exc) from exc
        return self.request_status(str(result.get("request_id") or request_id))

    def accept_china_workbook(
        self,
        *,
        request_id: str,
        business_date: str,
        shipment_id: str,
        workbook_bytes: bytes,
        filename: str,
        content_type: str,
        expenses: Iterable[Mapping[str, Any]] = (),
        actor: str,
    ) -> dict[str, Any]:
        selected_request = _request_id(request_id)
        selected_date = _date(business_date, field="business_date")
        shipment, lines, source_revision = self.supplier_shipment_source(shipment_id)
        revision = _fingerprint({"source_revision": source_revision, "source_sha256": _sha256(workbook_bytes)})
        identity = DocumentIdentity(
            request_id=selected_request,
            source_system="supplier_registry",
            source_type="china_acceptance_workbook",
            source_id=str(shipment["shipment_id"]),
            source_revision=revision,
            idempotency_epoch=self._preview_epoch(),
            actor=_actor(actor),
            business_date=selected_date,
        )
        try:
            result = self._service().preview_china_acceptance_workbook(
                identity=identity,
                source_bytes=bytes(workbook_bytes),
                source_filename=str(filename),
                source_content_type=str(content_type),
                shipment_lines=lines,
                expenses=list(expenses),
                template_source_revision=source_revision,
            )
        except FfPoolDocumentError as exc:
            raise _surface_from_document_error(exc) from exc
        return self.request_status(str(result.get("request_id") or selected_request))

    def accept_inventory_workbook(
        self,
        *,
        request_id: str,
        business_date: str,
        workbook_bytes: bytes,
        filename: str,
        content_type: str,
        actor: str,
    ) -> dict[str, Any]:
        self._require_writer()
        selected_request = _request_id(request_id)
        selected_date = _date(business_date, field="business_date")
        catalog, source_revision = self.inventory_catalog()
        cost_basis = self._pool_cost_basis()
        revision = _fingerprint({"catalog_revision": source_revision, "source_sha256": _sha256(workbook_bytes)})
        identity = DocumentIdentity(
            request_id=selected_request,
            source_system="operator_http",
            source_type="pool_inventory_workbook",
            source_id=f"inventory:{revision.removeprefix('sha256:')[:24]}",
            source_revision=revision,
            idempotency_epoch=self._writer_epoch(),
            actor=_actor(actor),
            business_date=selected_date,
        )
        try:
            result = self._service().preview_inventory_workbook(
                identity=identity,
                source_bytes=bytes(workbook_bytes),
                source_filename=str(filename),
                source_content_type=str(content_type),
                catalog=catalog,
                cost_basis_by_nm=cost_basis,
            )
        except FfPoolDocumentError as exc:
            raise _surface_from_document_error(exc) from exc
        return self.request_status(str(result.get("request_id") or selected_request))

    def confirm_document(self, request_id: str) -> dict[str, Any]:
        selected = _identity_token(request_id, field="request_id")
        status = self.request_status(selected)
        if status.get("document_kind") == "china_acceptance":
            activation = status.get("guided_acceptance_activation") or {}
            if not bool(activation.get("effective")):
                raise FfPoolSurfaceError(
                    "guided_acceptance_not_activated",
                    str(activation.get("reason_ru") or "Проведение приёмки ещё не активировано."),
                    details=activation,
                    http_status=409,
                )
        self._require_writer()
        try:
            self._service().post(selected)
        except FfPoolDocumentError as exc:
            raise _surface_from_document_error(exc) from exc
        return self.request_status(selected)

    def china_template(self, shipment_id: str, *, facility_id: str = "") -> tuple[bytes, str]:
        self._require_schema_readonly()
        _shipment, lines, source_revision = self.supplier_shipment_source(shipment_id)
        try:
            data = generate_china_acceptance_workbook(
                facilities=self._active_facilities_read(),
                shipment_lines=lines,
                source_revision=source_revision,
                selected_facility_id=str(facility_id or "").strip(),
            )
        except FfPoolXlsxError as exc:
            raise _surface_from_xlsx_error(exc) from exc
        return data, f"FF_приёмка_{_safe_filename(shipment_id)}.xlsx"

    def inventory_template(self, facility_id: str, scope: str) -> tuple[bytes, str]:
        self._require_schema_readonly()
        selected = _identity_token(facility_id, field="facility_id")
        selected_scope = _scope(scope)
        catalog, source_revision = self.inventory_catalog()
        targets = self._pool_targets(selected)
        try:
            data = generate_inventory_workbook(
                facilities=self._active_facilities_read(),
                facility_id=selected,
                scope=selected_scope,
                catalog=catalog,
                source_revision=source_revision,
                targets=targets,
            )
        except FfPoolXlsxError as exc:
            raise _surface_from_xlsx_error(exc) from exc
        return data, f"FF_инвентаризация_{_safe_filename(selected)}_{selected_scope}.xlsx"

    def supplier_shipment_source(self, shipment_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        selected = _identity_token(shipment_id, field="shipment_id")
        with self._read() as conn:
            tables = self._tables(conn)
            required = {"sheet_vitrina_v1_supplier_shipments", "sheet_vitrina_v1_supplier_shipment_lines"}
            if not required.issubset(tables):
                raise FfPoolSurfaceError("supplier_source_unavailable", "Supplier shipment source is unavailable", http_status=503)
            shipment = conn.execute(
                "SELECT shipment_id,updated_at,archived_at,order_status,actual_ff_acceptance_date FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
                (selected,),
            ).fetchone()
            if shipment is None or str(shipment["archived_at"] or ""):
                raise FfPoolSurfaceError("supplier_shipment_not_found", "Supplier shipment was not found", http_status=404)
            if str(shipment["actual_ff_acceptance_date"] or ""):
                raise FfPoolSurfaceError(
                    "supplier_shipment_already_accepted",
                    "Supplier shipment already has a factual FF acceptance",
                    http_status=409,
                )
            source_rows = conn.execute(
                """SELECT line_id,sort_order,barcode,internal_sku,internal_nm_id,internal_name,qty,amount
                   FROM sheet_vitrina_v1_supplier_shipment_lines
                   WHERE shipment_id=? AND line_type='product' ORDER BY sort_order,line_id""",
                (selected,),
            ).fetchall()
            if not source_rows:
                raise FfPoolSurfaceError(
                    "supplier_lines_unavailable",
                    "Supplier shipment has no exact matched product lines",
                )
            if "sheet_vitrina_v1_nomenclature_items" not in tables:
                raise FfPoolSurfaceError(
                    "exact_identity_evidence_missing",
                    "Supplier SKU requires canonical server-owned nomenclature evidence",
                    details={"reason": "nomenclature_unavailable"},
                    http_status=409,
                )
            nomenclature_rows = conn.execute(
                """SELECT item_id,nm_id,barcode,barcodes_json
                   FROM sheet_vitrina_v1_nomenclature_items
                   WHERE is_active=1 AND is_hidden=0
                   ORDER BY nm_id,item_id"""
            ).fetchall()
        lines = _resolve_supplier_lines_with_canonical_nomenclature(
            source_rows,
            nomenclature_rows,
        )
        try:
            from packages.application.our_wb_costs import OurWbCostBlock
            from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime

            preview = OurWbCostBlock(
                runtime=RegistryUploadDbBackedRuntime(runtime_dir=self.runtime_dir),
                timestamp_factory=self.timestamp_factory,
            ).preview_supplier_ff_cost_layer(selected)
        except (TypeError, ValueError) as exc:
            raise FfPoolSurfaceError(
                "supplier_capital_unavailable",
                "China acceptance requires a complete exact supplier cost preview",
                details={"reason": str(exc)[:240]},
                http_status=409,
            ) from exc
        cost_rows: dict[int, Decimal] = {}
        for item in preview.get("lines") or []:
            nm_id = int(item.get("nm_id") or 0)
            capital = _decimal(item.get("line_total_cost_rub") or 0)
            if nm_id > 0:
                cost_rows[nm_id] = cost_rows.get(nm_id, Decimal("0")) + capital
        for item in lines:
            nm_id = int(item["nm_id"])
            total_capital = cost_rows.get(nm_id, Decimal("0"))
            total_quantity = int(item["quantity"])
            if total_capital <= 0 or total_quantity <= 0:
                raise FfPoolSurfaceError(
                    "supplier_capital_unavailable",
                    "China acceptance requires positive exact supplier capital for every SKU",
                    details={"nm_id": nm_id},
                    http_status=409,
                )
            item["capital_rub"] = canonical_decimal_text(total_capital * Decimal(int(item["quantity"])) / Decimal(total_quantity))
        revision = _fingerprint({"shipment": dict(shipment), "lines": lines, "cost_inputs_hash": preview["inputs_hash"]})
        return dict(shipment), lines, revision

    def inventory_catalog(self) -> tuple[list[dict[str, Any]], str]:
        with self._read() as conn:
            if "sheet_vitrina_v1_nomenclature_items" not in self._tables(conn):
                raise FfPoolSurfaceError("nomenclature_unavailable", "Nomenclature catalog is unavailable", http_status=503)
            rows = conn.execute(
                """SELECT nm_id,our_sku,nomenclature_name,barcode,updated_at
                   FROM sheet_vitrina_v1_nomenclature_items
                   WHERE is_active=1 AND is_hidden=0 AND nm_id IS NOT NULL
                   ORDER BY nm_id,item_id"""
            ).fetchall()
        deduplicated: dict[int, dict[str, Any]] = {}
        for row in rows:
            nm_id = int(row["nm_id"])
            if nm_id in deduplicated:
                raise FfPoolSurfaceError("ambiguous_nomenclature", "Nomenclature contains duplicate active nmId", details={"nm_id": nm_id}, http_status=409)
            deduplicated[nm_id] = {
                "nm_id": nm_id,
                "sku": str(row["our_sku"] or row["nomenclature_name"] or nm_id),
                "barcode": str(row["barcode"] or ""),
            }
        catalog = list(deduplicated.values())
        if not catalog:
            raise FfPoolSurfaceError("nomenclature_empty", "Active nomenclature catalog is empty", http_status=409)
        return catalog, _fingerprint(catalog)

    def _pool_targets(self, facility_id: str) -> dict[tuple[int, str], int]:
        epoch = self._writer_epoch()
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT nm_id,pool,quantity FROM {BALANCES_TABLE} WHERE projection_epoch=? AND facility_id=? ORDER BY nm_id,pool",
                (epoch, facility_id),
            ).fetchall()
        return {(int(row["nm_id"]), str(row["pool"])): int(row["quantity"]) for row in rows}

    def _pool_cost_basis(self) -> dict[int, str]:
        epoch = self._writer_epoch()
        with self._read() as conn:
            rows = conn.execute(
                f"""SELECT nm_id,SUM(quantity) AS quantity,decimal_sum(capital_rub) AS capital_rub
                    FROM {BALANCES_TABLE} WHERE projection_epoch=? GROUP BY nm_id ORDER BY nm_id""",
                (epoch,),
            ).fetchall()
        result: dict[int, str] = {}
        for row in rows:
            quantity = int(row["quantity"] or 0)
            capital = _decimal(row["capital_rub"])
            if quantity > 0 and capital > 0:
                result[int(row["nm_id"])] = canonical_decimal_text(capital / Decimal(quantity))
        return result

    def _active_facilities_read(self) -> list[dict[str, Any]]:
        with self._read() as conn:
            self._require_schema(conn)
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT f.facility_id,f.code,f.name,COALESCE(profile.city,'') AS city,
                               f.active,f.display_timezone FROM {FACILITIES_TABLE} f
                        LEFT JOIN {FACILITY_PROFILES_TABLE} profile ON profile.facility_id=f.facility_id
                        WHERE f.active=1 ORDER BY f.code,f.facility_id"""
                ).fetchall()
            ]

    def _service(self, *, resume: bool = True) -> FfPoolDocumentService:
        return FfPoolDocumentService(
            db_path=self.db_path,
            runtime_dir=self.runtime_dir,
            timestamp_factory=self.timestamp_factory,
            resume=resume,
        )

    def _preview_epoch(self) -> int:
        with self._read() as conn:
            row = conn.execute(
                f"SELECT COALESCE(MAX(epoch),1) FROM {FEATURE_EPOCHS_TABLE}"
            ).fetchone()
        return max(1, int(row[0] or 1))

    def _guided_acceptance_activation(self) -> dict[str, Any]:
        try:
            with self._read() as conn:
                feature = self._feature(conn, aggregate_revision="", schema=self._schema(conn))
                from packages.application.ff_pool_cutover import read_ff_pool_cutover_status

                opening = read_ff_pool_cutover_status(conn)
        except Exception:
            feature = {"writer_effective": False, "epoch": 0}
            opening = {"status": "not_applied"}
        writer = bool(feature.get("writer_effective"))
        opening_ready = str(opening.get("status") or "") == "applied"
        effective = writer and opening_ready
        return {
            "effective": effective,
            "writer_epoch_active": writer,
            "opening_active": opening_ready,
            "reason": "active" if effective else (
                "writer_epoch_off" if not writer else "opening_not_applied"
            ),
            "reason_ru": "Проведение доступно." if effective else (
                "Проведение выключено: writer epoch не активирован. Шаблон и проверка доступны."
                if not writer else
                "Проведение выключено: opening/cutover не применён. Шаблон и проверка доступны."
            ),
        }

    def _writer_epoch(self) -> int:
        with self._read() as conn:
            schema = self._schema(conn)
            feature = self._feature(conn, aggregate_revision="", schema=schema)
        if not feature["writer_effective"]:
            raise FfPoolSurfaceError(
                "facility_pool_feature_off",
                "Facility/pool business mutations are disabled until a writer feature epoch is activated",
                details={"reason": feature["reason"]},
                http_status=409,
            )
        return int(feature["epoch"])

    def _require_writer(self) -> None:
        self._writer_epoch()

    def _require_schema_readonly(self) -> None:
        with self._read() as conn:
            self._require_schema(conn)

    def _require_schema(self, conn: sqlite3.Connection) -> None:
        schema = self._schema(conn)
        if not schema["available"]:
            raise FfPoolSurfaceError(
                "schema_absent",
                "FF facility/pool schema is not available",
                details={"missing_tables": schema["missing_tables"]},
                http_status=503,
            )

    def _schema(self, conn: sqlite3.Connection) -> dict[str, Any]:
        tables = self._tables(conn)
        missing = sorted(REQUIRED_TABLES - tables)
        return {"available": not missing, "missing_tables": missing, "tables": tables}

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    def _feature(
        self,
        conn: sqlite3.Connection,
        *,
        aggregate_revision: str,
        schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not schema["available"]:
            return {
                "epoch": 0,
                "writer_configured": False,
                "reader_configured": False,
                "writer_effective": False,
                "reader_effective": False,
                "parity_status": "not_evaluated",
                "reason": "schema_absent_default_off",
            }
        return asdict(read_ff_pool_feature_state(conn, aggregate_revision=str(aggregate_revision or "")))

    def _resolve_request(self, conn: sqlite3.Connection, request_id: str) -> str:
        row = conn.execute(f"SELECT request_id FROM {REQUESTS_TABLE} WHERE request_id=?", (request_id,)).fetchone()
        if row is not None:
            return str(row["request_id"])
        alias = conn.execute(f"SELECT request_id FROM {ALIASES_TABLE} WHERE client_request_id=?", (request_id,)).fetchone()
        return str(alias["request_id"]) if alias is not None else ""

    def _append_facility_change(
        self,
        conn: sqlite3.Connection,
        *,
        request_id: str,
        request_identity: str,
        facility_id: str,
        action: str,
        actor: str,
        previous: Mapping[str, Any],
        current: Mapping[str, Any],
        changed_at: str,
    ) -> None:
        change_id = "fffc_" + _fingerprint(
            {"request_id": request_id, "action": action, "facility_id": facility_id}
        ).removeprefix("sha256:")[:28]
        conn.execute(
            f"""INSERT INTO {FACILITY_CHANGES_TABLE}(
                   change_id,request_id,request_identity,facility_id,action,actor,
                   previous_json,current_json,changed_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                change_id,
                request_id,
                request_identity,
                facility_id,
                action,
                _actor(actor),
                _json(previous),
                _json(current),
                changed_at,
            ),
        )

    def _not_found_request(self, request_id: str, *, feature: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "contract_name": CONTRACT_NAME,
            "workflow_contract": "ff_document_workflow_v1",
            "status": "ready",
            "feature": dict(feature),
            "request_id": request_id,
            "state": "not_found",
            "state_label_ru": WORKFLOW_LABELS_RU["not_found"],
            "confirm_allowed": False,
            "steps": _workflow_steps("not_found"),
        }

    def _empty_page(self, status: str, *, feature: Mapping[str, Any], page: int, limit: int) -> dict[str, Any]:
        return {
            "contract_name": CONTRACT_NAME,
            "status": status,
            "feature": dict(feature),
            "facilities": [],
            "page": _page_payload(page, limit, 0),
        }

    def _read(self) -> sqlite3.Connection:
        return _connect_readonly(self.db_path)

    def _now(self) -> str:
        value = str(self.timestamp_factory())
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FfPoolSurfaceError("invalid_timestamp", "timestamp_factory must return UTC ISO") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise FfPoolSurfaceError("invalid_timestamp", "timestamp_factory must return UTC ISO")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _connect_readonly(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError as exc:
        raise FfPoolSurfaceError("runtime_store_absent", "Runtime store is not initialized", http_status=503) from exc
    conn.row_factory = sqlite3.Row
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _connect_write(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.create_aggregate("decimal_sum", 1, _DecimalSum)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class _DecimalSum:
    """SQLite aggregate that never coerces canonical decimal text through float."""

    def __init__(self) -> None:
        self.total = Decimal("0")

    def step(self, value: Any) -> None:
        if value is not None:
            self.total += _decimal(value)

    def finalize(self) -> str:
        return canonical_decimal_text(self.total)


def _canonical_nomenclature_identities(
    rows: Iterable[Mapping[str, Any]],
    *,
    target_nm_ids: set[int],
) -> dict[int, dict[str, Any]]:
    by_nm_id: dict[int, list[dict[str, Any]]] = {}
    barcode_owners: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        try:
            nm_id = int(row["nm_id"] or 0)
        except (TypeError, ValueError):
            nm_id = 0
        item_id = str(row["item_id"] or "").strip()
        target_row = nm_id in target_nm_ids
        try:
            primary = _canonical_barcode_text(
                row["barcode"],
                item_id=item_id,
                nm_id=nm_id,
            )
        except FfPoolSurfaceError:
            if target_row:
                raise
            primary = ""
        raw_extra = str(row["barcodes_json"] or "[]")
        try:
            decoded_extra = json.loads(raw_extra)
        except json.JSONDecodeError as exc:
            if target_row:
                raise FfPoolSurfaceError(
                    "invalid_nomenclature_identity_evidence",
                    "Canonical nomenclature barcode evidence is not valid JSON",
                    details={"item_id": item_id, "nm_id": nm_id},
                    http_status=409,
                ) from exc
            decoded_extra = []
        if not isinstance(decoded_extra, list):
            if target_row:
                raise FfPoolSurfaceError(
                    "invalid_nomenclature_identity_evidence",
                    "Canonical nomenclature barcode evidence must be a list",
                    details={"item_id": item_id, "nm_id": nm_id},
                    http_status=409,
                )
            decoded_extra = []
        extra: list[str] = []
        for value in decoded_extra:
            try:
                extra.append(
                    _canonical_barcode_text(value, item_id=item_id, nm_id=nm_id)
                )
            except FfPoolSurfaceError:
                if target_row:
                    raise
        barcodes = ([primary] if primary else []) + sorted(
            {value for value in extra if value and value != primary}
        )
        identity = {
            "item_id": item_id,
            "nm_id": nm_id,
            "barcode": primary,
            "barcodes": barcodes,
        }
        identity["identity_revision"] = _fingerprint(
            {
                "item_id": item_id,
                "nm_id": nm_id,
                "primary_barcode": primary,
                "barcodes": barcodes,
            }
        )
        if nm_id > 0:
            by_nm_id.setdefault(nm_id, []).append(identity)
        for barcode in barcodes:
            barcode_owners.setdefault(barcode, []).append(identity)

    result: dict[int, dict[str, Any]] = {}
    for nm_id in sorted(target_nm_ids):
        candidates = by_nm_id.get(nm_id) or []
        if not candidates:
            raise FfPoolSurfaceError(
                "exact_identity_evidence_missing",
                "Supplier SKU requires canonical server-owned nomenclature evidence",
                details={"nm_id": nm_id},
                http_status=409,
            )
        if len(candidates) != 1:
            raise FfPoolSurfaceError(
                "ambiguous_nomenclature",
                "Nomenclature contains duplicate active non-hidden nmId evidence",
                details={
                    "nm_id": nm_id,
                    "item_ids": sorted(str(item["item_id"]) for item in candidates),
                },
                http_status=409,
            )
        canonical = candidates[0]
        if not canonical["barcode"]:
            raise FfPoolSurfaceError(
                "exact_identity_evidence_missing",
                "Supplier SKU requires a canonical server-owned primary barcode",
                details={"nm_id": nm_id, "item_id": canonical["item_id"]},
                http_status=409,
            )
        for barcode in canonical["barcodes"]:
            owners = barcode_owners.get(str(barcode)) or []
            if len(owners) != 1:
                raise FfPoolSurfaceError(
                    "ambiguous_nomenclature_barcode",
                    "Canonical barcode is owned by more than one active non-hidden nomenclature item",
                    details={
                        "nm_id": nm_id,
                        "owner_nm_ids": sorted(
                            {int(item["nm_id"]) for item in owners if int(item["nm_id"]) > 0}
                        ),
                        "owner_item_ids": sorted(str(item["item_id"]) for item in owners),
                    },
                    http_status=409,
                )
        result[nm_id] = canonical
    return result


def _resolve_supplier_lines_with_canonical_nomenclature(
    source_rows: Iterable[Mapping[str, Any]],
    nomenclature_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    materialized_source = list(source_rows)
    target_nm_ids = {
        _positive_integer(row["internal_nm_id"], field="supplier nm_id")
        for row in materialized_source
    }
    canonical_identities = _canonical_nomenclature_identities(
        nomenclature_rows,
        target_nm_ids=target_nm_ids,
    )
    aggregated: dict[int, dict[str, Any]] = {}
    for row in materialized_source:
        nm_id = _positive_integer(row["internal_nm_id"], field="supplier nm_id")
        quantity = _whole_number(row["qty"], field="supplier quantity", positive=True)
        canonical = canonical_identities[nm_id]
        source_barcode = str(row["barcode"] or "").strip()
        if source_barcode and source_barcode not in canonical["barcodes"]:
            raise FfPoolSurfaceError(
                "supplier_identity_drift",
                "Supplier barcode conflicts with canonical server-owned nomenclature",
                details={"nm_id": nm_id, "line_id": str(row["line_id"] or "")},
                http_status=409,
            )
        identity = {
            "barcode": canonical["barcode"],
            "barcodes": list(canonical["barcodes"]),
            "identity_revision": canonical["identity_revision"],
            "sku": str(row["internal_sku"] or row["internal_name"] or nm_id),
        }
        current = aggregated.get(nm_id)
        if current is not None and any(current[key] != identity[key] for key in identity):
            raise FfPoolSurfaceError(
                "ambiguous_supplier_identity",
                "Supplier composition contains conflicting exact identity for one nmId",
                details={"nm_id": nm_id},
                http_status=409,
            )
        aggregated[nm_id] = {
            "nm_id": nm_id,
            **identity,
            "quantity": int((current or {}).get("quantity") or 0) + quantity,
            "capital_rub": "0",
        }
    return [aggregated[nm_id] for nm_id in sorted(aggregated)]


def _canonical_barcode_text(value: Any, *, item_id: str, nm_id: int) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise FfPoolSurfaceError(
            "invalid_nomenclature_identity_evidence",
            "Canonical nomenclature barcode evidence must be exact text",
            details={"item_id": item_id, "nm_id": nm_id},
            http_status=409,
        )
    token = value.strip()
    if re.search(r"[eE][+-]?[0-9]+$", token) or re.fullmatch(r"[0-9]+\.[0-9]+", token):
        raise FfPoolSurfaceError(
            "invalid_nomenclature_identity_evidence",
            "Canonical nomenclature barcode evidence must be lossless exact text",
            details={"item_id": item_id, "nm_id": nm_id},
            http_status=409,
        )
    return token


def _facility_public(row: Mapping[str, Any], *, detail_visible: bool) -> dict[str, Any]:
    quantity = int(row["quantity"] or 0)
    capital = _decimal(row["capital_rub"])
    return {
        "facility_id": str(row["facility_id"]),
        "code": str(row["code"]),
        "name": str(row["name"]),
        "city": str(row["city"] or ""),
        "active": bool(row["active"]),
        "display_timezone": str(row["display_timezone"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "quantity": quantity if detail_visible else None,
        "capital_rub": canonical_decimal_text(capital) if detail_visible else None,
        "wac_rub": _wac(capital, quantity) if detail_visible else None,
        "balance_count": int(row["balance_count"] or 0) if detail_visible else 0,
        "document_count": int(row["document_count"] or 0),
        "detail_visible": bool(detail_visible),
    }


def _page(page: Any, limit: Any, *, maximum: int = MAX_PAGE_SIZE) -> tuple[int, int, int]:
    try:
        selected_page = int(page)
        selected_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise FfPoolSurfaceError("invalid_pagination", "page and limit must be integers") from exc
    if selected_page < 1 or selected_limit < 1 or selected_limit > maximum:
        raise FfPoolSurfaceError(
            "invalid_pagination",
            f"page must be positive and limit must be between 1 and {maximum}",
        )
    return selected_page, selected_limit, (selected_page - 1) * selected_limit


def _page_payload(page: int, limit: int, total: int) -> dict[str, Any]:
    return {
        "page": int(page),
        "limit": int(limit),
        "total_count": int(total),
        "page_count": max(1, (int(total) + int(limit) - 1) // int(limit)),
        "has_next": int(page) * int(limit) < int(total),
    }


def _search(value: Any) -> str:
    token = str(value or "").strip()
    if len(token) > 160:
        raise FfPoolSurfaceError("search_too_long", "search must not exceed 160 characters")
    return token


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _identity_token(value: Any, *, field: str) -> str:
    token = str(value or "").strip()
    if not token or len(token) > 240 or SAFE_TEXT_RE.search(token):
        raise FfPoolSurfaceError(f"invalid_{field}", f"{field} is invalid")
    return token


def _request_id(value: Any) -> str:
    token = str(value or "").strip()
    if not REQUEST_ID_RE.fullmatch(token):
        raise FfPoolSurfaceError("invalid_request_id", "request_id has an invalid format")
    return token


def _text(value: Any, *, field: str, maximum: int) -> str:
    token = " ".join(str(value or "").split())
    if not token or len(token) > maximum or SAFE_TEXT_RE.search(token):
        raise FfPoolSurfaceError(f"invalid_{field}", f"{field} must contain 1..{maximum} safe characters")
    return token


def _actor(value: Any) -> str:
    return _text(value or "web_operator", field="actor", maximum=160)


def _timezone(value: Any) -> str:
    token = _text(value, field="display_timezone", maximum=100)
    try:
        ZoneInfo(token)
    except ZoneInfoNotFoundError as exc:
        raise FfPoolSurfaceError("invalid_display_timezone", "display_timezone must be a valid IANA timezone") from exc
    return token


def _boolean(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise FfPoolSurfaceError(f"invalid_{field}", f"{field} must be boolean")


def _pool(value: Any) -> str:
    token = str(value or "").strip().upper()
    if token not in POOLS:
        raise FfPoolSurfaceError("invalid_pool", "pool must be exact FBS or FBO")
    return token


def _scope(value: Any) -> str:
    token = str(value or "").strip()
    if token not in {"FBS", "FBO", "both"}:
        raise FfPoolSurfaceError("invalid_pool_scope", "scope must be FBS, FBO or both")
    return token


def _date(value: Any, *, field: str, optional: bool = False) -> str:
    token = str(value or "").strip()
    if not token and optional:
        return ""
    try:
        parsed = datetime.strptime(token, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise FfPoolSurfaceError(f"invalid_{field}", f"{field} must be YYYY-MM-DD") from exc
    if parsed != token:
        raise FfPoolSurfaceError(f"invalid_{field}", f"{field} must be YYYY-MM-DD")
    return token


def _fbs_reservations(conn: sqlite3.Connection, *, facility_id: str) -> dict[str, Any]:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if not {FBS_LIFECYCLE_CURRENT_TABLE, FBS_CUTOVER_MANIFESTS_TABLE}.issubset(tables):
        return {"quantity": 0, "by_nm_id": {}, "updated_at": ""}
    rows = conn.execute(
        f"""WITH active_cutover AS (
                SELECT cutover_id FROM {FBS_CUTOVER_MANIFESTS_TABLE}
                ORDER BY cutover_at DESC,cutover_id DESC LIMIT 1
            )
            SELECT current.nm_id,SUM(current.quantity) AS quantity,MAX(current.updated_at) AS updated_at
            FROM {FBS_LIFECYCLE_CURRENT_TABLE} current
            JOIN active_cutover cutover ON cutover.cutover_id=current.cutover_id
            WHERE current.facility_id=? AND current.pool='FBS' AND current.state='reserved'
            GROUP BY current.nm_id""",
        (facility_id,),
    ).fetchall()
    by_nm_id = {int(row["nm_id"]): int(row["quantity"] or 0) for row in rows}
    return {
        "quantity": sum(by_nm_id.values()),
        "by_nm_id": by_nm_id,
        "updated_at": max((str(row["updated_at"] or "") for row in rows), default=""),
    }


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value or "0"))
    except (InvalidOperation, ValueError) as exc:
        raise FfPoolSurfaceError("invalid_decimal", "Stored decimal value is invalid", http_status=500) from exc
    if not result.is_finite():
        raise FfPoolSurfaceError("invalid_decimal", "Stored decimal value is invalid", http_status=500)
    return result


def _wac(capital: Decimal, quantity: int) -> str | None:
    if int(quantity) <= 0:
        return None
    return canonical_decimal_text(capital / Decimal(int(quantity)))


def _whole_number(value: Any, *, field: str, positive: bool = False) -> int:
    amount = _decimal(value)
    if amount != amount.to_integral_value():
        raise FfPoolSurfaceError("fractional_quantity", f"{field} must be an integer")
    result = int(amount)
    if positive and result <= 0:
        raise FfPoolSurfaceError("nonpositive_quantity", f"{field} must be positive")
    return result


def _positive_integer(value: Any, *, field: str) -> int:
    result = _whole_number(value, field=field, positive=True)
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_value(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError):
        return default


def _json_object(value: Any) -> dict[str, Any]:
    parsed = _json_value(value, {})
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _etagged(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["etag"] = '"' + _fingerprint(result) + '"'
    result["payload_bytes"] = 0
    for _attempt in range(3):
        actual = len(_json(result).encode("utf-8"))
        if actual == result["payload_bytes"]:
            break
        result["payload_bytes"] = actual
    return result


def _workflow_steps(state: str) -> list[dict[str, str]]:
    sequence = [
        ("input", "Ввод / загрузка"),
        ("checking", "Проверка"),
        ("preview", "Предпросмотр"),
        ("posting", "Проведение"),
        ("replay", "Распределение / пересчёт"),
        ("complete", "Готово"),
    ]
    rank = {
        "not_found": -1,
        "accepted": 0,
        "processing": 1,
        "blocked": 2,
        "ready": 2,
        "posted": 3,
        "replay": 4,
        "complete": 5,
        "error": 3,
    }.get(state, -1)
    result = []
    for index, (key, label) in enumerate(sequence):
        status = "complete" if index < rank or state == "complete" else "pending"
        if index == rank and state not in {"complete", "not_found"}:
            status = "blocked" if state == "blocked" else "error" if state == "error" else "running"
        result.append({"key": key, "label_ru": label, "status": status})
    return result


def _surface_from_document_error(exc: FfPoolDocumentError) -> FfPoolSurfaceError:
    conflict_codes = {
        "request_id_identity_conflict",
        "source_revision_identity_conflict",
        "request_not_ready",
        "writer_epoch_required",
        "concurrent_pool_balance_drift",
    }
    return FfPoolSurfaceError(
        exc.code,
        str(exc),
        details=exc.details,
        http_status=409 if exc.code in conflict_codes else 422,
    )


def _surface_from_xlsx_error(exc: FfPoolXlsxError) -> FfPoolSurfaceError:
    return FfPoolSurfaceError(
        exc.code,
        str(exc),
        details=exc.details,
        http_status=413 if exc.code in {"request_too_large", "xlsx_file_too_large"} else 422,
    )


def _safe_filename(value: Any) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
    return token[:80] or uuid4().hex[:12]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
