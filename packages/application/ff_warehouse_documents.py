"""One lazy business read model over canonical FF document sources."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping


CONTRACT_NAME = "ff_warehouse_business_documents_v1"
ZERO = Decimal("0")
ALLOWED_EFFECTS = {"all", "incoming", "outgoing", "cost_only", "reservation"}
ALLOWED_REASONS = {
    "all",
    "supplier_receipt",
    "wb_shipment",
    "inventory",
    "return",
    "manual",
    "overhead",
    "correction",
    "opening",
}


class FfWarehouseDocumentsError(ValueError):
    pass


class FfWarehouseDocumentView:
    def __init__(self, *, db_path: Path) -> None:
        self.db_path = db_path

    def count_business_documents(self) -> int:
        with _connect(self.db_path) as conn:
            tables = _tables(conn)
            total = 0
            ledger_opening_exists = False
            if "sheet_vitrina_v1_ff_stock_operations" in tables:
                rows = conn.execute(
                    """
                    SELECT operation_type,source_type,source_key,source_object_id,
                           source_object_label,total_quantity_delta
                    FROM sheet_vitrina_v1_ff_stock_operations
                    """
                ).fetchall()
                for row in rows:
                    meta = _ledger_meta(dict(row))
                    ledger_opening_exists = ledger_opening_exists or meta["reason"] == "opening"
                    if not meta["technical"]:
                        total += 1
            if "sheet_vitrina_v1_ff_inventory_reconciliations" in tables:
                total += int(
                    conn.execute(
                        "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_inventory_reconciliations"
                    ).fetchone()[0]
                )
            if "sheet_vitrina_v1_ff_stock_reservation_operations" in tables:
                total += int(
                    conn.execute(
                        "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_reservation_operations"
                    ).fetchone()[0]
                )
            if "sheet_vitrina_v1_warehouse_documents" in tables and not ledger_opening_exists:
                total += int(
                    conn.execute(
                        "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_documents WHERE warehouse_key='ff'"
                    ).fetchone()[0]
                )
            return total

    def page(
        self,
        *,
        page: int = 1,
        limit: int = 25,
        effect: str = "all",
        reason: str = "all",
        business_date_from: str = "",
        business_date_to: str = "",
        search: str = "",
        include_technical: bool = False,
    ) -> dict[str, Any]:
        normalized_page = max(1, int(page))
        normalized_limit = min(100, max(1, int(limit)))
        normalized_effect = str(effect or "all").strip().lower()
        normalized_reason = str(reason or "all").strip().lower()
        if normalized_effect not in ALLOWED_EFFECTS:
            raise FfWarehouseDocumentsError("unsupported document effect filter")
        if normalized_reason not in ALLOWED_REASONS:
            raise FfWarehouseDocumentsError("unsupported document reason filter")
        date_from = _optional_date(business_date_from)
        date_to = _optional_date(business_date_to)
        if date_from and date_to and date_from > date_to:
            raise FfWarehouseDocumentsError("business date from is after date to")
        normalized_search = " ".join(str(search or "").split()).casefold()
        if len(normalized_search) > 120:
            raise FfWarehouseDocumentsError("document search is limited to 120 characters")
        with _connect(self.db_path) as conn:
            documents = _project_documents(conn)
        hidden_technical_count = sum(1 for item in documents if item["technical"])
        filtered = [
            item
            for item in documents
            if (include_technical or not item["technical"])
            and (normalized_effect == "all" or item["effect"] == normalized_effect)
            and (normalized_reason == "all" or item["reason"] == normalized_reason)
            and (not date_from or item["business_date"] >= date_from)
            and (not date_to or item["business_date"] <= date_to)
            and (not normalized_search or normalized_search in item["search_text"])
        ]
        filtered.sort(
            key=lambda item: (
                item["business_date"],
                item["created_at"],
                item["document_id"],
            ),
            reverse=True,
        )
        total = len(filtered)
        offset = (normalized_page - 1) * normalized_limit
        rows = [
            {key: value for key, value in item.items() if key not in {"technical", "search_text", "source_pk"}}
            for item in filtered[offset : offset + normalized_limit]
        ]
        payload = {
            "contract_name": CONTRACT_NAME,
            "status": "ready",
            "warehouse_key": "ff",
            "documents": rows,
            "filters": {
                "effect": normalized_effect,
                "reason": normalized_reason,
                "business_date_from": date_from,
                "business_date_to": date_to,
                "search": str(search or ""),
                "include_technical": bool(include_technical),
            },
            "filter_catalog": _filter_catalog(),
            "hidden_technical_count": hidden_technical_count,
            "page": {
                "page": normalized_page,
                "limit": normalized_limit,
                "total_count": total,
                "page_count": max(1, (total + normalized_limit - 1) // normalized_limit),
                "has_next": offset + normalized_limit < total,
            },
        }
        payload["etag"] = '"sha256:' + _digest(payload) + '"'
        payload["payload_bytes"] = len(_json(payload).encode("utf-8"))
        return payload

    def detail(self, document_id: str) -> dict[str, Any]:
        stable_id = str(document_id or "")
        with _connect(self.db_path) as conn:
            if stable_id.startswith("ffop:"):
                document = _ledger_detail(conn, stable_id.removeprefix("ffop:"))
            elif stable_id.startswith("ffinv:"):
                document = _inventory_detail(conn, stable_id.removeprefix("ffinv:"))
            elif stable_id.startswith("ffres:"):
                document = _reservation_detail(conn, stable_id.removeprefix("ffres:"))
            elif stable_id.startswith("ffopen:"):
                document = _opening_detail(conn, stable_id.removeprefix("ffopen:"))
            elif stable_id.startswith("fftech:"):
                raw = stable_id.removeprefix("fftech:")
                version_id, separator, source_id = raw.partition(":")
                if not separator:
                    raise FfWarehouseDocumentsError("invalid technical document identity")
                document = _technical_detail(conn, version_id=version_id, document_id=source_id)
            else:
                document = _opening_detail(conn, stable_id)
        return {
            "contract_name": CONTRACT_NAME,
            "status": "ready",
            "document": document,
            "etag": '"sha256:' + _digest(document) + '"',
        }

    def source_file(self, document_id: str) -> tuple[bytes, str, str]:
        stable_id = str(document_id or "")
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        with _connect(self.db_path) as conn:
            if stable_id.startswith("ffinv:"):
                row = conn.execute(
                    """
                    SELECT source_file_blob,source_filename,source_content_type
                    FROM sheet_vitrina_v1_ff_inventory_reconciliations
                    WHERE reconciliation_id=?
                    """,
                    (stable_id.removeprefix("ffinv:"),),
                ).fetchone()
            elif stable_id.startswith("ffop:"):
                row = conn.execute(
                    """
                    SELECT source_file_blob,source_filename,source_content_type
                    FROM sheet_vitrina_v1_ff_stock_operations WHERE operation_id=?
                    """,
                    (stable_id.removeprefix("ffop:"),),
                ).fetchone()
            else:
                row = None
        if row is None or row["source_file_blob"] is None:
            raise FfWarehouseDocumentsError("document source file not found")
        return (
            bytes(row["source_file_blob"]),
            str(row["source_filename"] or "ff-document.xlsx"),
            str(row["source_content_type"] or content_type),
        )


def _project_documents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    tables = _tables(conn)
    documents: list[dict[str, Any]] = []
    ledger_opening_exists = False
    if {
        "sheet_vitrina_v1_ff_stock_operations",
        "sheet_vitrina_v1_ff_stock_operation_lines",
    }.issubset(tables):
        rows = conn.execute(
            """
            SELECT operation.operation_id,operation.operation_type,
                   operation.source_type,operation.source_key,
                   operation.source_object_id,operation.source_object_label,
                   operation.created_at,operation.business_effective_date,
                   operation.created_by,operation.sku_count,
                   operation.total_quantity_delta,operation.diagnostics_json,
                   operation.source_filename,
                   CASE WHEN operation.source_file_blob IS NULL THEN 0 ELSE 1 END AS has_source_file,
                   line.line_no,line.nm_id,line.barcode,line.sku,
                   line.nomenclature_name,line.raw_json
            FROM sheet_vitrina_v1_ff_stock_operations operation
            LEFT JOIN sheet_vitrina_v1_ff_stock_operation_lines line
              ON line.operation_id=operation.operation_id
            ORDER BY operation.created_at,operation.operation_id,line.line_no
            """
        ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            operation_id = str(row["operation_id"])
            entry = grouped.setdefault(
                operation_id,
                {"header": dict(row), "lines": []},
            )
            if row["line_no"] is not None:
                entry["lines"].append(dict(row))
        for entry in grouped.values():
            item = _ledger_document(entry["header"], entry["lines"])
            ledger_opening_exists = ledger_opening_exists or item["reason"] == "opening"
            documents.append(item)

    if "sheet_vitrina_v1_ff_inventory_reconciliations" in tables:
        for row in conn.execute(
            """
            SELECT reconciliation_id,source_sha256,source_filename,business_date,
                   created_at,created_by,status,manifest_json,operation_ids_json
            FROM sheet_vitrina_v1_ff_inventory_reconciliations
            """
        ).fetchall():
            manifest = _loads(row["manifest_json"], {})
            linked = list(_loads(row["operation_ids_json"], []))
            source = str(row["source_filename"] or row["source_sha256"] or "")
            documents.append(
                _base_document(
                    document_id="ffinv:" + str(row["reconciliation_id"]),
                    source_pk=str(row["reconciliation_id"]),
                    prefix="ИНВ",
                    business_date=str(row["business_date"]),
                    created_at=str(row["created_at"]),
                    type_label="Инвентаризация склада FF",
                    effect="none",
                    reason="inventory",
                    warehouse_from="Склад FF",
                    warehouse_to="Склад FF",
                    quantity=ZERO,
                    capital=ZERO,
                    source_basis=source,
                    source_object_type="ff_inventory_reconciliation",
                    source_object_id=str(row["reconciliation_id"]),
                    actor=str(row["created_by"]),
                    status_label=_status_label(str(row["status"])),
                    sku_count=len(manifest.get("per_sku") or []),
                    technical=False,
                    search_parts=[source, row["source_sha256"], *linked],
                    linked_document_ids=["ffop:" + str(item) for item in linked],
                    has_source_file=True,
                )
            )

    if {
        "sheet_vitrina_v1_ff_stock_reservation_operations",
        "sheet_vitrina_v1_ff_stock_reservation_lines",
    }.issubset(tables):
        rows = conn.execute(
            """
            SELECT operation.*,line.line_no,line.nm_id,line.quantity_delta
            FROM sheet_vitrina_v1_ff_stock_reservation_operations operation
            LEFT JOIN sheet_vitrina_v1_ff_stock_reservation_lines line
              ON line.operation_id=operation.operation_id
            ORDER BY operation.created_at,operation.operation_id,line.line_no
            """
        ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = grouped.setdefault(
                str(row["operation_id"]),
                {"header": dict(row), "lines": []},
            )
            if row["line_no"] is not None:
                entry["lines"].append(dict(row))
        for entry in grouped.values():
            header = entry["header"]
            lines = entry["lines"]
            delta = sum((_decimal(item["quantity_delta"]) for item in lines), ZERO)
            subtype = str(header["operation_type"] or "reserve")
            label = {
                "reserve": "Резервирование под поставку WB",
                "adjust": "Корректировка резерва под поставку WB",
                "release": "Снятие резерва под поставку WB",
                "fulfill": "Исполнение резерва под поставку WB",
            }.get(subtype, "Резервирование под поставку WB")
            documents.append(
                _base_document(
                    document_id="ffres:" + str(header["operation_id"]),
                    source_pk=str(header["operation_id"]),
                    prefix="РЕЗ",
                    business_date=str(header["created_at"] or "")[:10],
                    created_at=str(header["created_at"]),
                    type_label=label,
                    effect="reservation",
                    reason="wb_shipment",
                    warehouse_from="Склад FF",
                    warehouse_to="Резерв под поставку WB",
                    quantity=ZERO,
                    capital=None,
                    source_basis="WB-поставка " + str(header["supply_id"] or ""),
                    source_object_type="wb_supply",
                    source_object_id=str(header["supply_id"] or ""),
                    actor="system",
                    status_label="Не меняет физический остаток",
                    sku_count=len({int(item["nm_id"]) for item in lines}),
                    technical=False,
                    search_parts=[header["supply_id"], header["source_key"], *(item["nm_id"] for item in lines)],
                    reservation_quantity=delta,
                )
            )

    if "sheet_vitrina_v1_warehouse_documents" in tables and not ledger_opening_exists:
        for row in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_warehouse_documents WHERE warehouse_key='ff'"
        ).fetchall():
            documents.append(
                _base_document(
                    document_id=str(row["document_id"]),
                    source_pk=str(row["document_id"]),
                    prefix="НАЧ",
                    business_date=str(row["occurred_at"] or "")[:10],
                    created_at=str(row["created_at"]),
                    type_label="Ввод начальных остатков",
                    effect="incoming",
                    reason="opening",
                    warehouse_from="Начальный баланс",
                    warehouse_to="Склад FF",
                    quantity=_decimal(row["total_quantity"]),
                    capital=None,
                    source_basis=str(row["source_basis"]),
                    actor="system",
                    status_label=str(row["status_label"] or "Проведено"),
                    sku_count=int(row["sku_count"] or 0),
                    technical=False,
                    search_parts=[row["document_number"], row["source_basis"]],
                )
            )

    if {
        "sheet_vitrina_v1_warehouse_functional_documents",
        "sheet_vitrina_v1_warehouse_functional_document_lines",
    }.issubset(tables):
        for row in conn.execute(
            """
            SELECT document.*,COUNT(line.line_id) AS line_count
            FROM sheet_vitrina_v1_warehouse_functional_documents document
            LEFT JOIN sheet_vitrina_v1_warehouse_functional_document_lines line
              ON line.document_id=document.document_id AND line.version_id=document.version_id
            WHERE document.warehouse_key='ff'
            GROUP BY document.document_id,document.version_id
            """
        ).fetchall():
            documents.append(
                _base_document(
                    document_id="fftech:" + str(row["version_id"]) + ":" + str(row["document_id"]),
                    source_pk=str(row["document_id"]),
                    prefix="ТЕХ",
                    business_date=str(row["occurred_at"] or "")[:10],
                    created_at=str(row["created_at"]),
                    type_label=_technical_label(str(row["document_type"])),
                    effect="none",
                    reason="technical",
                    warehouse_from="Технический источник",
                    warehouse_to="Склад FF",
                    quantity=_decimal(row["quantity"]),
                    capital=_decimal(row["capital_rub"]),
                    source_basis=str(row["source_id"]),
                    source_object_type="warehouse_functional_document",
                    source_object_id=str(row["source_id"]),
                    actor="system",
                    status_label="Технический документ",
                    sku_count=int(row["line_count"] or 0),
                    technical=True,
                    search_parts=[row["document_id"], row["source_id"], row["source_fingerprint"]],
                )
            )
    return documents


def _ledger_document(header: Mapping[str, Any], lines: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    normalized_lines = [dict(item) for item in lines]
    meta = _ledger_meta(header)
    capital_values = [_line_capital(item) for item in normalized_lines]
    capital = (
        sum((value for value in capital_values if value is not None), ZERO)
        if capital_values and all(value is not None for value in capital_values)
        else None
    )
    quantity = _decimal(header.get("total_quantity_delta"))
    search_parts: list[Any] = [
        header.get("operation_id"),
        header.get("source_key"),
        header.get("source_object_id"),
        header.get("source_object_label"),
        header.get("source_filename"),
    ]
    for line in normalized_lines:
        search_parts.extend(
            [line.get("nm_id"), line.get("barcode"), line.get("sku"), line.get("nomenclature_name")]
        )
    diagnostics = _loads(header.get("diagnostics_json"), {})
    parent = str(diagnostics.get("reconciliation_id") or "")
    original_operation = str(diagnostics.get("original_operation_id") or "")
    linked_document_ids = [
        *(["ffinv:" + parent] if parent else []),
        *(["ffop:" + original_operation] if original_operation else []),
    ]
    source_object_type = str(header.get("source_type") or "")
    source_object_id = str(header.get("source_object_id") or "")
    transfer_identity = (
        f"warehouse-transfer:{source_object_type}:{source_object_id}"
        if str(header.get("operation_type") or "")
        in {"auto_receipt", "auto_writeoff", "auto_return"}
        and source_object_type
        and source_object_id
        else ""
    )
    item = _base_document(
        document_id="ffop:" + str(header.get("operation_id") or ""),
        source_pk=str(header.get("operation_id") or ""),
        prefix=meta["prefix"],
        business_date=str(header.get("business_effective_date") or header.get("created_at") or "")[:10],
        created_at=str(header.get("created_at") or ""),
        type_label=meta["label"],
        effect=meta["effect"],
        reason=meta["reason"],
        warehouse_from=meta["from"],
        warehouse_to=meta["to"],
        quantity=quantity,
        capital=capital,
        source_basis=str(header.get("source_object_label") or header.get("source_object_id") or ""),
        source_object_type=source_object_type,
        source_object_id=source_object_id,
        transfer_identity=transfer_identity,
        actor=str(header.get("created_by") or "system"),
        status_label="Проведено",
        sku_count=int(header.get("sku_count") or len(normalized_lines)),
        technical=meta["technical"],
        search_parts=search_parts,
        linked_document_ids=linked_document_ids,
        has_source_file=bool(header.get("has_source_file")),
    )
    if meta["effect"] == "cost_only":
        item["total_expense_rub"] = item["total_capital_rub"]
    return item


def _ledger_meta(header: Mapping[str, Any]) -> dict[str, Any]:
    operation = str(header.get("operation_type") or "")
    source = str(header.get("source_type") or "")
    source_text = " ".join(
        str(header.get(key) or "")
        for key in ("source_key", "source_object_label", "source_object_id")
    ).casefold()
    if operation == "auto_receipt" and source == "supplier_shipment":
        return _meta("ПСТ", "Поступление на склад FF", "incoming", "supplier_receipt", "Китай", "Склад FF")
    if operation == "auto_writeoff" and source == "wb_supply":
        return _meta("ОТГ", "Отгрузка FF → WB", "outgoing", "wb_shipment", "Склад FF", "Поставка WB")
    if operation == "auto_return":
        return _meta("ВЗВ", "Возврат на склад FF из поставки WB", "incoming", "return", "Поставка WB", "Склад FF")
    if operation == "inventory_receipt":
        return _meta("ИЗЛ", "Оприходование излишков", "incoming", "inventory", "Инвентаризация FF", "Склад FF")
    if operation == "inventory_writeoff":
        return _meta("НЕД", "Списание недостач", "outgoing", "inventory", "Склад FF", "Инвентаризация FF")
    if operation == "ff_overhead_allocation":
        return _meta("НР", "Распределение накладных расходов FF", "cost_only", "overhead", "Склад FF", "Склад FF")
    if operation == "ff_overhead_reversal":
        return _meta("СТО", "Корректировка / сторно", "cost_only", "correction", "Склад FF", "Склад FF")
    if operation in {"inventory_rollback", "correction_receipt", "correction_writeoff", "box_correction"} or "rollback" in source:
        effect = "incoming" if _decimal(header.get("total_quantity_delta")) > ZERO else "outgoing"
        return _meta(
            "СТО",
            "Корректировка / сторно",
            effect,
            "correction",
            "Корректировка" if effect == "incoming" else "Склад FF",
            "Склад FF" if effect == "incoming" else "Корректировка",
        )
    if operation == "manual_receipt":
        if any(token in source_text for token in ("opening", "начальн", "открыт")):
            return _meta("НАЧ", "Ввод начальных остатков", "incoming", "opening", "Начальный баланс", "Склад FF")
        return _meta("РУЧ", "Ручное оприходование", "incoming", "manual", "Ручной документ", "Склад FF")
    if operation == "manual_writeoff":
        return _meta("РУЧ", "Ручное списание", "outgoing", "manual", "Склад FF", "Ручной документ")
    technical = source == "runtime_repair" or operation in {"warehouse_sync", "functional_cutover"}
    effect = "incoming" if _decimal(header.get("total_quantity_delta")) > ZERO else "outgoing"
    if technical:
        effect = "none"
    return _meta(
        "ТЕХ" if technical else "КОР",
        "Технический документ" if technical else "Корректировка / сторно",
        effect,
        "technical" if technical else "correction",
        "Источник",
        "Склад FF",
        technical=technical,
    )


def _meta(
    prefix: str,
    label: str,
    effect: str,
    reason: str,
    warehouse_from: str,
    warehouse_to: str,
    *,
    technical: bool = False,
) -> dict[str, Any]:
    return {
        "prefix": prefix,
        "label": label,
        "effect": effect,
        "reason": reason,
        "from": warehouse_from,
        "to": warehouse_to,
        "technical": technical,
    }


def _base_document(
    *,
    document_id: str,
    source_pk: str,
    prefix: str,
    business_date: str,
    created_at: str,
    type_label: str,
    effect: str,
    reason: str,
    warehouse_from: str,
    warehouse_to: str,
    quantity: Decimal,
    capital: Decimal | None,
    source_basis: str,
    source_object_type: str = "",
    source_object_id: str = "",
    transfer_identity: str = "",
    actor: str,
    status_label: str,
    sku_count: int,
    technical: bool,
    search_parts: Iterable[Any],
    linked_document_ids: Iterable[str] = (),
    has_source_file: bool = False,
    reservation_quantity: Decimal = ZERO,
) -> dict[str, Any]:
    normalized_date = str(business_date or "")[:10]
    number = f"{prefix}-FF-{normalized_date.replace('-', '')}-{_short(source_pk)}"
    search_values = [
        number,
        type_label,
        effect,
        reason,
        source_basis,
        source_object_type,
        source_object_id,
        transfer_identity,
        actor,
        warehouse_from,
        warehouse_to,
        *list(search_parts),
    ]
    return {
        "document_id": document_id,
        "source_pk": source_pk,
        "document_number": number,
        "business_date": normalized_date,
        "occurred_at": normalized_date,
        "created_at": created_at,
        "document_type_label": type_label,
        "effect": effect,
        "reason": reason,
        "warehouse_key": "ff",
        "warehouse_name": "Склад FF",
        "warehouse_from_key": _direction_key(warehouse_from),
        "warehouse_to_key": _direction_key(warehouse_to),
        "warehouse_from_label": warehouse_from,
        "warehouse_to_label": warehouse_to,
        "source_basis": source_basis,
        "source_object_type": source_object_type,
        "source_object_id": source_object_id,
        "transfer_identity": transfer_identity,
        "actor": actor,
        "sku_count": sku_count,
        "total_quantity": _text(quantity),
        "quantity": _text(quantity),
        "signed_quantity_effect": _text(quantity),
        "reservation_quantity_effect": _text(reservation_quantity),
        "total_cost_rub": None,
        "total_capital_rub": _text(capital) if capital is not None else None,
        "total_expense_rub": None,
        "status_label": status_label,
        "linked_document_ids": list(linked_document_ids),
        "has_source_file": bool(has_source_file),
        "detail_loaded": False,
        "lines": [],
        "technical": bool(technical),
        "search_text": " ".join(str(item or "") for item in search_values).casefold(),
    }


def _ledger_detail(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any]:
    header = conn.execute(
        """
        SELECT operation_id,operation_type,source_type,source_key,
               source_object_id,source_object_label,created_at,
               business_effective_date,created_by,sku_count,total_quantity_delta,
               diagnostics_json,source_filename,
               CASE WHEN source_file_blob IS NULL THEN 0 ELSE 1 END AS has_source_file
        FROM sheet_vitrina_v1_ff_stock_operations WHERE operation_id=?
        """,
        (operation_id,),
    ).fetchone()
    if header is None:
        raise FfWarehouseDocumentsError("warehouse document not found")
    lines = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=? ORDER BY line_no",
            (operation_id,),
        ).fetchall()
    ]
    document = _ledger_document(dict(header), lines)
    public_lines = [_public_ledger_line(item) for item in lines]
    if str(header["operation_type"] or "") in {
        "ff_overhead_allocation",
        "ff_overhead_reversal",
    } and "sheet_vitrina_v1_ff_overhead_documents" in _tables(conn):
        overhead = conn.execute(
            "SELECT allocations_json FROM sheet_vitrina_v1_ff_overhead_documents WHERE document_id=?",
            (str(header["source_object_id"] or ""),),
        ).fetchone()
        if overhead is not None:
            sign = Decimal("-1") if str(header["operation_type"]) == "ff_overhead_reversal" else Decimal("1")
            public_lines = [
                _public_overhead_line(item, sign=sign)
                for item in _loads(overhead["allocations_json"], [])
            ]
    diagnostics = _loads(header["diagnostics_json"], {})
    return {
        **{key: value for key, value in document.items() if key not in {"technical", "search_text", "source_pk"}},
        "created_at": str(header["created_at"]),
        "provenance": diagnostics,
        "human_evidence": _human_evidence(diagnostics),
        "detail_loaded": True,
        "source_file_path": (
            f"/v1/sheet-vitrina-v1/warehouses/ff/documents/{document['document_id']}/file"
            if document["has_source_file"]
            else None
        ),
        "lines": public_lines,
    }


def _inventory_detail(conn: sqlite3.Connection, reconciliation_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT reconciliation_id,source_sha256,source_filename,business_date,
               created_at,created_by,status,manifest_json,operation_ids_json,
               reconciliation_json,plan_fingerprint,approval_reference
        FROM sheet_vitrina_v1_ff_inventory_reconciliations WHERE reconciliation_id=?
        """,
        (reconciliation_id,),
    ).fetchone()
    if row is None:
        raise FfWarehouseDocumentsError("warehouse document not found")
    manifest = dict(_loads(row["manifest_json"], {}))
    operations = list(_loads(row["operation_ids_json"], []))
    summary = _base_document(
        document_id="ffinv:" + reconciliation_id,
        source_pk=reconciliation_id,
        prefix="ИНВ",
        business_date=str(row["business_date"]),
        created_at=str(row["created_at"]),
        type_label="Инвентаризация склада FF",
        effect="none",
        reason="inventory",
        warehouse_from="Склад FF",
        warehouse_to="Склад FF",
        quantity=ZERO,
        capital=ZERO,
        source_basis=str(row["source_filename"]),
        source_object_type="ff_inventory_reconciliation",
        source_object_id=reconciliation_id,
        actor=str(row["created_by"]),
        status_label=_status_label(str(row["status"])),
        sku_count=len(manifest.get("per_sku") or []),
        technical=False,
        search_parts=[row["source_sha256"]],
        linked_document_ids=["ffop:" + str(item) for item in operations],
        has_source_file=True,
    )
    lines = []
    for item in manifest.get("per_sku") or []:
        quantity = _decimal(item.get("inventory_delta")) + _decimal(item.get("return_quantity"))
        capital = _decimal(item.get("capital_delta_rub"))
        lines.append(
            {
                "nm_id": int(item["nm_id"]),
                "sku": str(item.get("sku_comment") or item["nm_id"]),
                "nomenclature_name": str(item.get("sku_comment") or ""),
                "barcode": "",
                "quantity": _text(quantity),
                "average_unit_cost_rub": item.get("unit_cost_rub"),
                "capital_rub": _text(capital),
                "physical_target_quantity": str(item.get("target_quantity") or "0"),
                "before_quantity": str(item.get("before_quantity") or "0"),
                "provenance": item.get("cost_basis") or {},
                "human_evidence": _human_evidence(item.get("cost_basis") or {}),
            }
        )
    audit = _loads(row["reconciliation_json"], {})
    return {
        **{key: value for key, value in summary.items() if key not in {"technical", "search_text", "source_pk"}},
        "created_at": str(row["created_at"]),
        "source_sha256": str(row["source_sha256"]),
        "source_file_path": f"/v1/sheet-vitrina-v1/warehouses/ff/documents/ffinv:{reconciliation_id}/file",
        "provenance": {
            "plan_fingerprint": str(row["plan_fingerprint"]),
            "source_sha256": str(row["source_sha256"]),
            "business_date": str(row["business_date"]),
            "approval_reference": str(row["approval_reference"]),
            "target_total": manifest.get("target_total"),
            "readback": audit,
        },
        "human_evidence": _human_evidence(audit),
        "detail_loaded": True,
        "lines": lines,
    }


def _reservation_detail(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any]:
    header = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_ff_stock_reservation_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if header is None:
        raise FfWarehouseDocumentsError("warehouse document not found")
    lines = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ff_stock_reservation_lines WHERE operation_id=? ORDER BY line_no",
            (operation_id,),
        ).fetchall()
    ]
    projected = next(
        item for item in _project_documents(conn) if item["document_id"] == "ffres:" + operation_id
    )
    diagnostics = _loads(header["diagnostics_json"], {})
    return {
        **{key: value for key, value in projected.items() if key not in {"technical", "search_text", "source_pk"}},
        "created_at": str(header["created_at"]),
        "provenance": diagnostics,
        "human_evidence": _human_evidence(diagnostics),
        "detail_loaded": True,
        "lines": [
            {
                "nm_id": int(item["nm_id"]),
                "sku": str(item["nm_id"]),
                "nomenclature_name": "",
                "barcode": "",
                "quantity": "0",
                "reservation_quantity_effect": _text(_decimal(item["quantity_delta"])),
                "average_unit_cost_rub": None,
                "capital_rub": None,
                "provenance": {"physical_quantity_effect": "0"},
                "human_evidence": [
                    {"label": "Резерв", "value": _text(_decimal(item["quantity_delta"]))},
                    {"label": "Физический остаток", "value": "не изменяется"},
                ],
            }
            for item in lines
        ],
    }


def _opening_detail(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_warehouse_documents WHERE document_id=? AND warehouse_key='ff'",
        (document_id,),
    ).fetchone()
    if row is None:
        raise FfWarehouseDocumentsError("warehouse document not found")
    lines = [
        dict(item)
        for item in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_warehouse_document_lines WHERE document_id=? ORDER BY line_no",
            (document_id,),
        ).fetchall()
    ]
    summary = _base_document(
        document_id=document_id,
        source_pk=document_id,
        prefix="НАЧ",
        business_date=str(row["occurred_at"] or "")[:10],
        created_at=str(row["created_at"]),
        type_label="Ввод начальных остатков",
        effect="incoming",
        reason="opening",
        warehouse_from="Начальный баланс",
        warehouse_to="Склад FF",
        quantity=_decimal(row["total_quantity"]),
        capital=None,
        source_basis=str(row["source_basis"]),
        actor="system",
        status_label=str(row["status_label"]),
        sku_count=len(lines),
        technical=False,
        search_parts=[row["document_number"]],
    )
    return {
        **{key: value for key, value in summary.items() if key not in {"technical", "search_text", "source_pk"}},
        "provenance": _loads(row["provenance_json"], {}),
        "human_evidence": _human_evidence(_loads(row["provenance_json"], {})),
        "detail_loaded": True,
        "lines": [
            {
                **item,
                "provenance": _loads(item["provenance_json"], {}),
                "human_evidence": _human_evidence(_loads(item["provenance_json"], {})),
            }
            for item in lines
        ],
    }


def _technical_detail(conn: sqlite3.Connection, *, version_id: str, document_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT * FROM sheet_vitrina_v1_warehouse_functional_documents
        WHERE version_id=? AND document_id=? AND warehouse_key='ff'
        """,
        (version_id, document_id),
    ).fetchone()
    if row is None:
        raise FfWarehouseDocumentsError("warehouse document not found")
    lines = [
        dict(item)
        for item in conn.execute(
            """
            SELECT * FROM sheet_vitrina_v1_warehouse_functional_document_lines
            WHERE version_id=? AND document_id=? ORDER BY nm_id,line_id
            """,
            (version_id, document_id),
        ).fetchall()
    ]
    summary = _base_document(
        document_id=f"fftech:{version_id}:{document_id}",
        source_pk=document_id,
        prefix="ТЕХ",
        business_date=str(row["occurred_at"] or "")[:10],
        created_at=str(row["created_at"]),
        type_label=_technical_label(str(row["document_type"])),
        effect="none",
        reason="technical",
        warehouse_from="Технический источник",
        warehouse_to="Склад FF",
        quantity=_decimal(row["quantity"]),
        capital=_decimal(row["capital_rub"]),
        source_basis=str(row["source_id"]),
        source_object_type="warehouse_functional_document",
        source_object_id=str(row["source_id"]),
        actor="system",
        status_label="Технический документ",
        sku_count=len(lines),
        technical=True,
        search_parts=[row["source_fingerprint"]],
    )
    return {
        **{key: value for key, value in summary.items() if key not in {"technical", "search_text", "source_pk"}},
        "provenance": _loads(row["provenance_json"], {}),
        "human_evidence": {
            "items": [
                {
                    "document": str(row["source_id"]),
                    "date": str(row["occurred_at"] or "")[:10],
                    "confirmation_status": "Технический аудит",
                }
            ]
        },
        "detail_loaded": True,
        "lines": [
            {
                "nm_id": int(item["nm_id"]),
                "sku": str(item["nm_id"]),
                "nomenclature_name": "",
                "barcode": "",
                "quantity": str(item["quantity"]),
                "average_unit_cost_rub": item["wac_rub"],
                "capital_rub": str(item["capital_rub"]),
                "provenance": _loads(item["provenance_json"], {}),
                "human_evidence": {
                    "items": [
                        {
                            "document": str(row["source_id"]),
                            "date": str(row["occurred_at"] or "")[:10],
                            "quantity_contribution": str(item["quantity"]),
                        }
                    ]
                },
            }
            for item in lines
        ],
    }


def _public_ledger_line(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(_loads(row.get("raw_json"), {}))
    capital = _line_capital(row)
    quantity = _decimal(row.get("quantity_delta"))
    unit_cost = None
    snapshot = raw.get("cost_snapshot") if isinstance(raw, Mapping) else None
    adjustment = raw.get("cost_adjustment") if isinstance(raw, Mapping) else None
    if isinstance(snapshot, Mapping):
        unit_cost = snapshot.get("unit_cost_rub")
    elif isinstance(adjustment, Mapping):
        unit_cost = adjustment.get("allocation_per_unit_rub")
    provenance = snapshot or adjustment or raw
    return {
        "nm_id": int(row.get("nm_id") or 0),
        "sku": str(row.get("sku") or row.get("nm_id") or ""),
        "nomenclature_name": str(row.get("nomenclature_name") or ""),
        "barcode": str(row.get("barcode") or ""),
        "quantity": _text(quantity),
        "average_unit_cost_rub": unit_cost,
        "capital_rub": _text(capital) if capital is not None else None,
        "provenance": provenance,
        "human_evidence": _human_evidence(provenance),
    }


def _public_overhead_line(row: Mapping[str, Any], *, sign: Decimal) -> dict[str, Any]:
    allocation = sign * _decimal(row.get("allocation_rub"))
    per_unit = sign * _decimal(row.get("allocation_per_unit_rub"))
    return {
        "nm_id": int(row.get("nm_id") or 0),
        "sku": str(row.get("sku") or row.get("nm_id") or ""),
        "nomenclature_name": str(row.get("nomenclature_name") or ""),
        "barcode": str(row.get("barcode") or ""),
        "quantity": "0",
        "average_unit_cost_rub": _text(per_unit),
        "capital_rub": _text(allocation),
        "provenance": {
            "allocation_basis_quantity": str(row.get("physical_quantity") or "0"),
            "exact_original_allocation_reversal": sign < ZERO,
        },
        "human_evidence": [
            {"label": "Физический остаток на дату", "value": str(row.get("physical_quantity") or "0")},
            {"label": "Распределение", "value": _text(allocation) + " RUB"},
        ],
    }


def _line_capital(row: Mapping[str, Any]) -> Decimal | None:
    raw = _loads(row.get("raw_json"), {})
    if not isinstance(raw, Mapping):
        return None
    for key in ("cost_snapshot", "cost_adjustment"):
        value = raw.get(key)
        if isinstance(value, Mapping) and value.get("capital_delta_rub") not in (None, ""):
            return _decimal(value.get("capital_delta_rub"))
    return None


def _filter_catalog() -> dict[str, Any]:
    return {
        "effects": [
            {"key": "all", "label": "Все эффекты"},
            {"key": "incoming", "label": "Приход"},
            {"key": "outgoing", "label": "Расход"},
            {"key": "cost_only", "label": "Только стоимость"},
            {"key": "reservation", "label": "Резерв"},
        ],
        "reasons": [
            {"key": "all", "label": "Все основания"},
            {"key": "supplier_receipt", "label": "Поступление от поставщика"},
            {"key": "wb_shipment", "label": "Поставка WB"},
            {"key": "inventory", "label": "Инвентаризация"},
            {"key": "return", "label": "Возврат"},
            {"key": "manual", "label": "Ручная операция"},
            {"key": "overhead", "label": "Накладные расходы"},
            {"key": "correction", "label": "Корректировка / сторно"},
            {"key": "opening", "label": "Начальные остатки"},
        ],
    }


def _technical_label(document_type: str) -> str:
    return {
        "functional_cutover": "Технический переход функционального контура",
        "warehouse_sync": "Техническая синхронизация склада",
        "repair": "Техническое восстановление",
        "archive": "Технический архив",
    }.get(document_type, "Технический документ")


def _status_label(status: str) -> str:
    return {
        "applied": "Проведено",
        "rolled_back": "Сторнировано",
        "reversed": "Сторнировано",
        "applying": "Проводится",
    }.get(status, status or "Проведено")


def _human_evidence(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, Mapping):
        return []
    labels = {
        "reason": "Основание",
        "reason_text": "Комментарий",
        "business_date": "Business date",
        "amount_rub": "Сумма, RUB",
        "source_sha256": "SHA-256 исходного файла",
        "source_revision": "Revision источника",
        "source_fingerprint": "Fingerprint источника",
        "original_operation_id": "Исходный документ",
        "denominator_quantity": "Распределено на единиц",
        "target_total": "Физический target",
    }
    return [
        {"label": labels[key], "value": str(value[key])}
        for key in labels
        if value.get(key) not in (None, "", [], {})
    ]


def _optional_date(value: Any) -> str:
    normalized = str(value or "").strip()[:10]
    if not normalized:
        return ""
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise FfWarehouseDocumentsError("invalid business date filter") from exc


def _short(value: Any) -> str:
    normalized = "".join(character for character in str(value or "").upper() if character.isalnum())
    if not normalized:
        return "000001"
    return normalized[-6:]


def _direction_key(label: str) -> str:
    return {
        "Склад FF": "ff",
        "Китай": "china",
        "Поставка WB": "wb_supply",
        "Резерв под поставку WB": "wb_supply_reservation",
        "Инвентаризация FF": "ff_inventory",
        "Ручной документ": "manual_document",
        "Корректировка": "correction",
        "Начальный баланс": "opening_balance",
        "Источник": "source",
        "Технический источник": "technical_source",
    }.get(str(label), "other")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO


def _text(value: Decimal) -> str:
    if value == ZERO:
        return "0"
    return format(value.normalize(), "f")


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
