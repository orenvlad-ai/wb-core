"""Server-owned supplier invoice shipment registry block."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Mapping
from uuid import uuid4

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.supplier_invoice_parser import (
    extract_iphone_model_keys,
    normalize_invoice_model,
    parse_supplier_invoice_xlsx,
)
from packages.contracts.supplier_shipments import (
    LINE_TYPE_EXTRA,
    LINE_TYPE_PRODUCT,
    MATCH_STATUS_AMBIGUOUS,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_MATCHED_BY_COMPATIBILITY,
    MATCH_STATUS_UNMATCHED,
    SHIPMENT_STATUS_ALL_MATCHED,
    SHIPMENT_STATUS_CHECKSUM_ERROR,
    SHIPMENT_STATUS_HAS_UNMATCHED,
    SHIPMENT_STATUS_MANUAL_OVERRIDE,
    SUPPLIER_INVOICE_CONTENT_TYPE,
    SUPPLIER_INVOICE_PARSER_VERSION,
)


DEFAULT_SUPPLIER_NAME = "HanShang Technology"


class SupplierShipmentsBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        timestamp_factory: callable | None = None,
    ) -> None:
        self.runtime = runtime
        self.timestamp_factory = timestamp_factory or _default_timestamp_factory

    def list_shipments(self) -> dict[str, Any]:
        rows = self.runtime.list_supplier_shipments()
        return {
            "contract_name": "sheet_vitrina_v1_supplier_shipments",
            "status": "ok",
            "shipments": [_with_invoice_download_path(row) for row in rows],
        }

    def parse_upload(
        self,
        workbook_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
    ) -> dict[str, Any]:
        filename = _safe_filename(uploaded_filename or "supplier-invoice.xlsx")
        if not filename.lower().endswith(".xlsx"):
            raise ValueError("supplier invoice upload must be an .xlsx file")
        parsed_payload = parse_supplier_invoice_xlsx(
            workbook_bytes,
            filename=filename,
            aliases=self._active_nomenclature_aliases(),
        )
        parsed_payload["metadata"] = _supplier_order_metadata(parsed_payload.get("metadata"))
        parsed_payload["lines"] = _apply_nomenclature_matches(
            [dict(item) for item in parsed_payload.get("lines") or []],
            self._active_nomenclature_items(),
        )
        upload_id = "upl_" + uuid4().hex
        created_at = self.timestamp_factory()
        sha256 = hashlib.sha256(workbook_bytes).hexdigest()
        relative_path = self._write_runtime_file(
            root_kind="uploads",
            entity_id=upload_id,
            filename=filename,
            body=workbook_bytes,
        )
        content_type = str(uploaded_content_type or "").strip() or SUPPLIER_INVOICE_CONTENT_TYPE
        self.runtime.save_supplier_shipment_upload(
            upload_id=upload_id,
            created_at=created_at,
            source_filename=filename,
            content_type=content_type,
            source_file_sha256=sha256,
            source_file_path=relative_path,
            parser_version=SUPPLIER_INVOICE_PARSER_VERSION,
            parsed_payload=parsed_payload,
        )
        payload = deepcopy(parsed_payload)
        payload.update(
            {
                "upload_id": upload_id,
                "created_at": created_at,
                "source_filename": filename,
                "source_file_sha256": sha256,
                "content_type": content_type,
            }
        )
        return payload

    def create_shipment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        upload_id = str(payload.get("upload_id") or "").strip()
        if not upload_id:
            raise ValueError("upload_id is required")
        upload = self.runtime.load_supplier_shipment_upload(upload_id)
        if upload is None:
            raise ValueError(f"supplier shipment upload not found: {upload_id}")
        edited_payload = _resolve_edited_payload(payload, fallback=upload["parsed_payload"])
        shipment_date = _validate_iso_date(str(payload.get("shipment_date") or edited_payload.get("shipment_date") or ""))
        metadata, lines, warnings, errors, summary, match_status = _normalize_edit_payload(
            edited_payload,
            shipment_date=shipment_date,
            force_manual_override=False,
        )
        lines = _apply_nomenclature_matches(lines, self._active_nomenclature_items())
        summary = _recalculate_summary(lines, declared_total=_optional_number(metadata.get("declared_invoice_total")))
        match_status = _shipment_match_status(lines, checksum_error=summary["checksum_error"])
        shipment_id = "sup_" + uuid4().hex
        now = self.timestamp_factory()
        source_filename = str(upload.get("source_filename") or "supplier-invoice.xlsx")
        source_path = self._copy_upload_to_shipment_file(
            upload_path=str(upload.get("source_file_path") or ""),
            shipment_id=shipment_id,
            filename=source_filename,
        )
        header = {
            "shipment_id": shipment_id,
            "created_at": now,
            "updated_at": now,
            "shipment_date": shipment_date,
            "invoice_no": metadata.get("invoice_no") or "",
            "invoice_date": metadata.get("invoice_date") or "",
            "contract_no": metadata.get("contract_no") or "",
            "contract_date": metadata.get("contract_date") or "",
            "supplier_name": metadata.get("supplier_name") or "",
            "customer_name": metadata.get("customer_name") or "",
            "currency": metadata.get("currency") or "",
            "product_qty_total": summary["product_qty_total"],
            "product_amount_total": summary["product_amount_total"],
            "extras_amount_total": summary["extras_amount_total"],
            "invoice_amount_total": summary["invoice_amount_total"],
            "declared_invoice_total": summary.get("declared_invoice_total"),
            "match_status": match_status,
            "source_filename": source_filename,
            "source_file_sha256": upload.get("source_file_sha256") or "",
            "source_file_path": source_path,
            "parser_version": upload.get("parser_version") or SUPPLIER_INVOICE_PARSER_VERSION,
            "warnings": warnings,
            "errors": errors,
        }
        self.runtime.save_supplier_shipment(header=header, lines=lines)
        return self.get_shipment(shipment_id)

    def get_shipment(self, shipment_id: str) -> dict[str, Any]:
        detail = self.runtime.load_supplier_shipment(shipment_id)
        if detail is None:
            raise ValueError(f"supplier shipment not found: {shipment_id}")
        return _detail_payload(detail)

    def update_shipment(self, shipment_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        existing = self.runtime.load_supplier_shipment(shipment_id)
        if existing is None:
            raise ValueError(f"supplier shipment not found: {shipment_id}")
        edited_payload = _resolve_edited_payload(payload, fallback=_detail_payload(existing))
        shipment_date = _validate_iso_date(
            str(payload.get("shipment_date") or edited_payload.get("shipment_date") or existing["header"].get("shipment_date") or "")
        )
        metadata, lines, warnings, errors, summary, match_status = _normalize_edit_payload(
            edited_payload,
            shipment_date=shipment_date,
            force_manual_override=False,
        )
        existing_header = dict(existing["header"])
        now = self.timestamp_factory()
        header = {
            **existing_header,
            "updated_at": now,
            "shipment_date": shipment_date,
            "invoice_no": metadata.get("invoice_no") or "",
            "invoice_date": metadata.get("invoice_date") or "",
            "contract_no": metadata.get("contract_no") or "",
            "contract_date": metadata.get("contract_date") or "",
            "supplier_name": metadata.get("supplier_name") or "",
            "customer_name": metadata.get("customer_name") or "",
            "currency": metadata.get("currency") or "",
            "product_qty_total": summary["product_qty_total"],
            "product_amount_total": summary["product_amount_total"],
            "extras_amount_total": summary["extras_amount_total"],
            "invoice_amount_total": summary["invoice_amount_total"],
            "declared_invoice_total": summary.get("declared_invoice_total"),
            "match_status": match_status,
            "warnings": warnings,
            "errors": errors,
        }
        self.runtime.save_supplier_shipment(header=header, lines=lines)
        return self.get_shipment(shipment_id)

    def delete_shipment(self, shipment_id: str) -> dict[str, Any]:
        detail = self.runtime.load_supplier_shipment(shipment_id)
        if detail is None:
            raise ValueError(f"supplier shipment not found: {shipment_id}")
        header = dict(detail.get("header") or {})
        source_file_path = str(header.get("source_file_path") or "")
        deleted = self.runtime.delete_supplier_shipment(shipment_id)
        if not deleted:
            raise ValueError(f"supplier shipment not found: {shipment_id}")
        self._delete_runtime_invoice_file(source_file_path)
        return {
            "contract_name": "sheet_vitrina_v1_supplier_shipments",
            "status": "ok",
            "deleted": True,
            "shipment_id": shipment_id,
        }

    def rematch_shipment(self, shipment_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        existing = self.runtime.load_supplier_shipment(shipment_id)
        if existing is None:
            raise ValueError(f"supplier shipment not found: {shipment_id}")
        detail_payload = _detail_payload(existing)
        overwrite_manual = bool((payload or {}).get("overwrite_manual"))
        detail_payload["lines"] = _apply_nomenclature_matches(
            detail_payload.get("lines") or [],
            self._active_nomenclature_items(),
            overwrite_manual=overwrite_manual,
        )
        shipment_date = _validate_iso_date(str(detail_payload.get("shipment_date") or ""))
        metadata, lines, warnings, errors, summary, match_status = _normalize_edit_payload(
            detail_payload,
            shipment_date=shipment_date,
            force_manual_override=False,
        )
        existing_header = dict(existing["header"])
        now = self.timestamp_factory()
        header = {
            **existing_header,
            "updated_at": now,
            "invoice_no": metadata.get("invoice_no") or "",
            "invoice_date": metadata.get("invoice_date") or "",
            "contract_no": metadata.get("contract_no") or "",
            "contract_date": metadata.get("contract_date") or "",
            "supplier_name": metadata.get("supplier_name") or "",
            "customer_name": metadata.get("customer_name") or "",
            "currency": metadata.get("currency") or "",
            "product_qty_total": summary["product_qty_total"],
            "product_amount_total": summary["product_amount_total"],
            "extras_amount_total": summary["extras_amount_total"],
            "invoice_amount_total": summary["invoice_amount_total"],
            "declared_invoice_total": summary.get("declared_invoice_total"),
            "match_status": match_status,
            "warnings": warnings,
            "errors": errors,
        }
        self.runtime.save_supplier_shipment(header=header, lines=lines)
        return self.get_shipment(shipment_id)

    def download_invoice(self, shipment_id: str) -> tuple[bytes, str, str]:
        detail = self.runtime.load_supplier_shipment(shipment_id)
        if detail is None:
            raise ValueError(f"supplier shipment not found: {shipment_id}")
        header = detail["header"]
        file_path = self._resolve_runtime_file(str(header.get("source_file_path") or ""))
        if not file_path.exists() or not file_path.is_file():
            raise ValueError(f"supplier invoice file is missing for shipment: {shipment_id}")
        content_type = SUPPLIER_INVOICE_CONTENT_TYPE
        return file_path.read_bytes(), str(header.get("source_filename") or "supplier-invoice.xlsx"), content_type

    def list_nomenclature(self) -> dict[str, Any]:
        self._ensure_nomenclature_ready()
        return {
            "contract_name": "sheet_vitrina_v1_nomenclature",
            "status": "ok",
            "items": self.runtime.list_nomenclature_items(),
        }

    def create_nomenclature_item(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        now = self.timestamp_factory()
        item = _normalize_nomenclature_payload(
            payload,
            item_id="nom_" + uuid4().hex,
            created_at=now,
            updated_at=now,
        )
        self._validate_nomenclature_unique(item)
        return {
            "contract_name": "sheet_vitrina_v1_nomenclature",
            "status": "ok",
            "item": self.runtime.save_nomenclature_item(item),
        }

    def update_nomenclature_item(self, item_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        existing = self.runtime.load_nomenclature_item(item_id)
        if existing is None:
            raise ValueError(f"nomenclature item not found: {item_id}")
        now = self.timestamp_factory()
        item = _normalize_nomenclature_payload(
            {**existing, **dict(payload)},
            item_id=item_id,
            created_at=str(existing.get("created_at") or now),
            updated_at=now,
        )
        self._validate_nomenclature_unique(item)
        return {
            "contract_name": "sheet_vitrina_v1_nomenclature",
            "status": "ok",
            "item": self.runtime.save_nomenclature_item(item),
        }

    def deactivate_nomenclature_item(self, item_id: str) -> dict[str, Any]:
        item = self.runtime.delete_nomenclature_item(item_id, updated_at=self.timestamp_factory())
        return {
            "contract_name": "sheet_vitrina_v1_nomenclature",
            "status": "ok",
            "item": item,
        }

    def _copy_upload_to_shipment_file(self, *, upload_path: str, shipment_id: str, filename: str) -> str:
        source_path = self._resolve_runtime_file(upload_path)
        if not source_path.exists() or not source_path.is_file():
            raise ValueError("staged supplier invoice upload file is missing")
        safe_filename = _safe_filename(filename)
        target_dir = self.runtime.runtime_dir / "supplier_invoices" / "files" / shipment_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / safe_filename
        shutil.copy2(source_path, target_path)
        return _relative_to_runtime(self.runtime.runtime_dir, target_path)

    def _write_runtime_file(self, *, root_kind: str, entity_id: str, filename: str, body: bytes) -> str:
        safe_filename = _safe_filename(filename)
        target_dir = self.runtime.runtime_dir / "supplier_invoices" / root_kind / entity_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / safe_filename
        target_path.write_bytes(body)
        return _relative_to_runtime(self.runtime.runtime_dir, target_path)

    def _resolve_runtime_file(self, relative_path: str) -> Path:
        normalized = str(relative_path or "").strip()
        if not normalized:
            raise ValueError("runtime file path is empty")
        root = self.runtime.runtime_dir.resolve()
        path = (root / normalized).resolve()
        if root != path and root not in path.parents:
            raise ValueError("runtime file path escapes runtime dir")
        return path

    def _delete_runtime_invoice_file(self, relative_path: str) -> None:
        if not str(relative_path or "").strip():
            return
        try:
            file_path = self._resolve_runtime_file(relative_path)
        except ValueError:
            return
        root = self.runtime.runtime_dir.resolve()
        if file_path.exists() and file_path.is_file():
            file_path.unlink()
        parent = file_path.parent
        if root != parent and root in parent.parents and parent.name.startswith("sup_"):
            shutil.rmtree(parent, ignore_errors=True)

    def _active_nomenclature_aliases(self) -> list[dict[str, Any]]:
        self._ensure_nomenclature_ready()
        aliases: list[dict[str, Any]] = []
        for item in self.runtime.list_nomenclature_items(active_only=True):
            aliases.extend(_nomenclature_item_aliases(item))
        return aliases

    def _active_nomenclature_items(self) -> list[dict[str, Any]]:
        self._ensure_nomenclature_ready()
        return self.runtime.list_nomenclature_items(active_only=True)

    def _ensure_nomenclature_ready(self) -> None:
        self._seed_nomenclature_from_current_config_if_empty()
        self._backfill_nomenclature_compatible_models()

    def _validate_nomenclature_unique(self, item: Mapping[str, Any]) -> None:
        if (
            bool(item.get("is_active"))
            and str(item.get("match_key") or "").strip()
            and self.runtime.active_nomenclature_match_key_exists(
                match_key=str(item.get("match_key") or "").strip(),
                exclude_item_id=str(item.get("item_id") or ""),
            )
        ):
            raise ValueError(f"duplicate active nomenclature match_key: {item.get('match_key')}")

    def _seed_nomenclature_from_current_config_if_empty(self) -> None:
        if self.runtime.list_nomenclature_items():
            return
        try:
            current_state = self.runtime.load_current_state()
        except Exception:
            return
        now = self.timestamp_factory()
        seen: set[str] = set()
        for config_item in getattr(current_state, "config_v2", []) or []:
            if not bool(getattr(config_item, "enabled", False)):
                continue
            display_name = str(getattr(config_item, "display_name", "") or "").strip()
            product_type = _product_type_from_config_item(display_name, str(getattr(config_item, "group", "") or ""))
            model_text = _model_text_from_nomenclature_name(display_name)
            normalized_model = normalize_invoice_model(model_text)
            compatible_model_keys = extract_iphone_model_keys(model_text)
            if not product_type or not normalized_model:
                continue
            match_key = f"{product_type}|{normalized_model}"
            if match_key in seen:
                continue
            seen.add(match_key)
            self.runtime.save_nomenclature_item(
                {
                    "item_id": f"nom_seed_{int(getattr(config_item, 'nm_id'))}",
                    "is_active": True,
                    "our_sku": "",
                    "nm_id": int(getattr(config_item, "nm_id")),
                    "nomenclature_name": display_name,
                    "product_type": product_type,
                    "match_key": match_key,
                    "aliases": [],
                    "compatible_models_text": model_text,
                    "compatible_model_keys": compatible_model_keys,
                    "comment": "seeded from current registry config_v2",
                    "created_at": now,
                    "updated_at": now,
                }
            )

    def _backfill_nomenclature_compatible_models(self) -> None:
        items = self.runtime.list_nomenclature_items()
        now = self.timestamp_factory()
        for item in items:
            if item.get("compatible_model_keys"):
                continue
            keys = _infer_compatible_model_keys(item)
            if not keys:
                continue
            text = str(item.get("compatible_models_text") or "").strip() or _compatible_models_text_from_keys(keys)
            updated = dict(item)
            updated["compatible_models_text"] = text
            updated["compatible_model_keys"] = keys
            updated["updated_at"] = now
            self.runtime.save_nomenclature_item(updated)


def _resolve_edited_payload(payload: Mapping[str, Any], *, fallback: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("payload")
    if raw is None:
        raw = payload.get("edited_payload")
    if isinstance(raw, Mapping):
        resolved = deepcopy(dict(raw))
    else:
        resolved = deepcopy(dict(fallback))
    for key in ("metadata", "lines", "summary", "warnings", "errors"):
        if key in payload and key not in resolved:
            resolved[key] = payload[key]
    if "shipment_date" in payload:
        resolved["shipment_date"] = payload["shipment_date"]
    return resolved


def _normalize_edit_payload(
    payload: Mapping[str, Any],
    *,
    shipment_date: str,
    force_manual_override: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[str], dict[str, Any], str]:
    metadata = _normalize_metadata(payload.get("metadata"))
    if not metadata.get("currency") and payload.get("currency"):
        metadata["currency"] = str(payload.get("currency") or "").strip()
    raw_lines = payload.get("lines") or []
    if not isinstance(raw_lines, list):
        raise ValueError("supplier shipment lines must be a list")
    lines = [
        _normalize_line(item, index=index, currency=str(metadata.get("currency") or ""), force_manual_override=force_manual_override)
        for index, item in enumerate(raw_lines, start=1)
        if isinstance(item, Mapping)
    ]
    if len(lines) != len(raw_lines):
        raise ValueError("supplier shipment lines must be JSON objects")
    summary = _recalculate_summary(lines, declared_total=_optional_number(metadata.get("declared_invoice_total")))
    warnings = _string_list(payload.get("warnings"))
    errors = _string_list(payload.get("errors"))
    if summary["checksum_error"] and not any("checksum" in item.lower() for item in errors):
        errors.append(
            "invoice total checksum mismatch: declared "
            f"{summary.get('declared_invoice_total')} vs parsed {summary.get('invoice_amount_total')}"
        )
    match_status = _shipment_match_status(lines, checksum_error=summary["checksum_error"])
    metadata["declared_invoice_total"] = summary.get("declared_invoice_total")
    return metadata, lines, warnings, errors, summary, match_status


def _normalize_metadata(raw: Any) -> dict[str, Any]:
    metadata = dict(raw) if isinstance(raw, Mapping) else {}
    return {
        "invoice_no": str(metadata.get("invoice_no") or "").strip(),
        "invoice_date": _optional_iso_date(metadata.get("invoice_date")),
        "contract_no": str(metadata.get("contract_no") or "").strip(),
        "contract_date": _optional_iso_date(metadata.get("contract_date")),
        "supplier_name": DEFAULT_SUPPLIER_NAME,
        "customer_name": "",
        "currency": str(metadata.get("currency") or "").strip().upper(),
        "declared_invoice_total": _optional_number(metadata.get("declared_invoice_total")),
    }


def _supplier_order_metadata(raw: Any) -> dict[str, Any]:
    metadata = _normalize_metadata(raw)
    metadata["supplier_name"] = DEFAULT_SUPPLIER_NAME
    metadata["customer_name"] = ""
    return metadata


def _normalize_line(
    raw: Mapping[str, Any],
    *,
    index: int,
    currency: str,
    force_manual_override: bool,
) -> dict[str, Any]:
    line_type = str(raw.get("line_type") or LINE_TYPE_PRODUCT).strip()
    if line_type not in {LINE_TYPE_PRODUCT, LINE_TYPE_EXTRA}:
        raise ValueError(f"line #{index}: line_type must be product or extra")
    qty = _optional_number(raw.get("qty"))
    unit_price = _optional_number(raw.get("unit_price"))
    amount = _optional_number(raw.get("amount"))
    if "qty" in raw and raw.get("qty") not in {None, ""} and qty is None:
        raise ValueError(f"line #{index}: qty must be numeric")
    if "unit_price" in raw and raw.get("unit_price") not in {None, ""} and unit_price is None:
        raise ValueError(f"line #{index}: unit_price must be numeric")
    if "amount" in raw and raw.get("amount") not in {None, ""} and amount is None:
        raise ValueError(f"line #{index}: amount must be numeric")
    internal_nm_id = _optional_int(raw.get("internal_nm_id"))
    product_type = str(raw.get("product_type") or "").strip()
    model_normalized = str(raw.get("model_normalized") or "").strip()
    match_key = str(raw.get("match_key") or "").strip()
    if line_type == LINE_TYPE_PRODUCT and product_type and model_normalized and not match_key:
        match_key = f"{product_type}|{model_normalized}"
    has_internal_match = bool(str(raw.get("internal_sku") or "").strip() or internal_nm_id or str(raw.get("internal_name") or "").strip())
    match_status = str(raw.get("match_status") or "").strip()
    if line_type == LINE_TYPE_EXTRA:
        match_status = "extra"
    elif match_status not in {
        MATCH_STATUS_MATCHED,
        MATCH_STATUS_MATCHED_BY_COMPATIBILITY,
        MATCH_STATUS_UNMATCHED,
        MATCH_STATUS_AMBIGUOUS,
    }:
        match_status = MATCH_STATUS_MATCHED if has_internal_match else MATCH_STATUS_UNMATCHED
    raw_payload = raw.get("raw") if isinstance(raw.get("raw"), Mapping) else {}
    return {
        "line_id": str(raw.get("line_id") or ("ln_" + uuid4().hex)).strip(),
        "line_type": line_type,
        "sort_order": _optional_int(raw.get("sort_order")) or index,
        "source_no": str(raw.get("source_no") or "").strip(),
        "product_type": product_type,
        "model_raw": str(raw.get("model_raw") or "").strip(),
        "model_normalized": model_normalized,
        "match_key": match_key,
        "internal_sku": str(raw.get("internal_sku") or "").strip(),
        "internal_nm_id": internal_nm_id,
        "internal_name": str(raw.get("internal_name") or "").strip(),
        "qty": qty,
        "unit_price": unit_price,
        "amount": amount,
        "currency": str(raw.get("currency") or currency or "").strip().upper(),
        "comment": str(raw.get("comment") or "").strip(),
        "match_status": match_status,
        "manual_override": bool(raw.get("manual_override")) or force_manual_override,
        "raw": dict(raw_payload),
    }


def _recalculate_summary(lines: list[Mapping[str, Any]], *, declared_total: float | None) -> dict[str, Any]:
    product_qty_total = _sum_numeric(item.get("qty") for item in lines if item.get("line_type") == LINE_TYPE_PRODUCT)
    product_amount_total = _sum_numeric(item.get("amount") for item in lines if item.get("line_type") == LINE_TYPE_PRODUCT)
    extras_amount_total = _sum_numeric(item.get("amount") for item in lines if item.get("line_type") == LINE_TYPE_EXTRA)
    invoice_amount_total = round(product_amount_total + extras_amount_total, 2)
    checksum_error = bool(declared_total is not None and abs(round(declared_total - invoice_amount_total, 2)) > 0.02)
    return {
        "product_qty_total": product_qty_total,
        "product_amount_total": product_amount_total,
        "extras_amount_total": extras_amount_total,
        "invoice_amount_total": invoice_amount_total,
        "declared_invoice_total": declared_total,
        "checksum_error": checksum_error,
    }


def _shipment_match_status(lines: list[Mapping[str, Any]], *, checksum_error: bool) -> str:
    if checksum_error:
        return SHIPMENT_STATUS_CHECKSUM_ERROR
    if any(bool(item.get("manual_override")) for item in lines):
        return SHIPMENT_STATUS_MANUAL_OVERRIDE
    if any(
        item.get("line_type") == LINE_TYPE_PRODUCT
        and item.get("match_status") not in {MATCH_STATUS_MATCHED, MATCH_STATUS_MATCHED_BY_COMPATIBILITY}
        for item in lines
    ):
        return SHIPMENT_STATUS_HAS_UNMATCHED
    return SHIPMENT_STATUS_ALL_MATCHED


def _detail_payload(detail: Mapping[str, Any]) -> dict[str, Any]:
    header = dict(detail.get("header") or {})
    header["supplier_name"] = DEFAULT_SUPPLIER_NAME
    header["customer_name"] = ""
    lines = [dict(item) for item in detail.get("lines") or []]
    summary = {
        "product_qty_total": header.get("product_qty_total"),
        "product_amount_total": header.get("product_amount_total"),
        "extras_amount_total": header.get("extras_amount_total"),
        "invoice_amount_total": header.get("invoice_amount_total"),
        "declared_invoice_total": header.get("declared_invoice_total"),
        "checksum_error": header.get("match_status") == SHIPMENT_STATUS_CHECKSUM_ERROR,
    }
    payload = {
        **header,
        "metadata": {
            "invoice_no": header.get("invoice_no") or "",
            "invoice_date": header.get("invoice_date") or "",
            "contract_no": header.get("contract_no") or "",
            "contract_date": header.get("contract_date") or "",
            "supplier_name": header.get("supplier_name") or "",
            "customer_name": header.get("customer_name") or "",
            "currency": header.get("currency") or "",
            "declared_invoice_total": header.get("declared_invoice_total"),
        },
        "summary": summary,
        "lines": lines,
        "product_lines": [item for item in lines if item.get("line_type") == LINE_TYPE_PRODUCT],
        "extra_lines": [item for item in lines if item.get("line_type") == LINE_TYPE_EXTRA],
        "invoice_download_path": _invoice_download_path(str(header.get("shipment_id") or "")),
    }
    return payload


def _with_invoice_download_path(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["supplier_name"] = DEFAULT_SUPPLIER_NAME
    payload["customer_name"] = ""
    payload["invoice_download_path"] = _invoice_download_path(str(payload.get("shipment_id") or ""))
    return payload


def _apply_nomenclature_matches(
    lines: list[Mapping[str, Any]],
    nomenclature_items: list[Mapping[str, Any]],
    *,
    overwrite_manual: bool = False,
) -> list[dict[str, Any]]:
    index = _build_nomenclature_match_index(nomenclature_items)
    matched_lines: list[dict[str, Any]] = []
    for raw_line in lines:
        line = dict(raw_line)
        if line.get("line_type") != LINE_TYPE_PRODUCT:
            matched_lines.append(line)
            continue
        product_type = str(line.get("product_type") or "").strip()
        normalized_model = str(line.get("model_normalized") or "").strip()
        match_key = str(line.get("match_key") or "").strip()
        if not match_key and product_type and normalized_model:
            match_key = f"{product_type}|{normalized_model}"
            line["match_key"] = match_key
        if bool(line.get("manual_override")) and not overwrite_manual:
            matched_lines.append(line)
            continue
        resolution = _resolve_nomenclature_match(line, index)
        _apply_match_resolution(line, resolution)
        matched_lines.append(line)
    return matched_lines


def _build_nomenclature_match_index(items: list[Mapping[str, Any]]) -> dict[str, Any]:
    exact_by_key: dict[str, list[dict[str, Any]]] = {}
    alias_by_key: dict[str, list[dict[str, Any]]] = {}
    compatible: list[dict[str, Any]] = []
    for item in items:
        if not bool(item.get("is_active")):
            continue
        item_payload = _nomenclature_item_match_payload(item)
        base_match_key = str(item.get("match_key") or "").strip()
        if base_match_key:
            exact_by_key.setdefault(base_match_key, []).append(item_payload)
        for alias in _nomenclature_item_aliases(item):
            alias_key = str(alias.get("match_key") or "").strip()
            if alias_key and alias_key != base_match_key:
                alias_by_key.setdefault(alias_key, []).append(_nomenclature_item_match_payload({**item, **alias}))
        compatible_keys = _infer_compatible_model_keys(item)
        if compatible_keys and str(item.get("product_type") or "") in {"clear", "anti_spy", "matte"}:
            compatible.append({**item_payload, "compatible_model_keys": compatible_keys})
    return {
        "exact_by_key": exact_by_key,
        "alias_by_key": alias_by_key,
        "compatible": compatible,
    }


def _resolve_nomenclature_match(line: Mapping[str, Any], index: Mapping[str, Any]) -> dict[str, Any] | None:
    product_type = str(line.get("product_type") or "").strip()
    match_key = str(line.get("match_key") or "").strip()
    exact_candidates = list((index.get("exact_by_key") or {}).get(match_key) or [])
    if len(exact_candidates) == 1:
        return {**exact_candidates[0], "match_status": MATCH_STATUS_MATCHED}
    if len(exact_candidates) > 1:
        return {"match_status": MATCH_STATUS_AMBIGUOUS}

    alias_candidates = list((index.get("alias_by_key") or {}).get(match_key) or [])
    if len(alias_candidates) == 1:
        return {**alias_candidates[0], "match_status": MATCH_STATUS_MATCHED}
    if len(alias_candidates) > 1:
        return {"match_status": MATCH_STATUS_AMBIGUOUS}

    invoice_keys = _line_compatible_model_keys(line)
    if product_type not in {"clear", "anti_spy", "matte"} or not invoice_keys:
        return None
    invoice_key_set = set(invoice_keys)
    scored: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    for candidate in index.get("compatible") or []:
        if str(candidate.get("product_type") or "") != product_type:
            continue
        candidate_keys = [str(item) for item in candidate.get("compatible_model_keys") or [] if str(item or "").strip()]
        if not candidate_keys:
            continue
        candidate_key_set = set(candidate_keys)
        intersection = sorted(invoice_key_set & candidate_key_set)
        if not intersection:
            continue
        subset_bonus = 1 if candidate_key_set.issubset(invoice_key_set) or invoice_key_set.issubset(candidate_key_set) else 0
        exact_size_bonus = 1 if candidate_key_set == invoice_key_set else 0
        score = (subset_bonus, len(intersection), exact_size_bonus)
        scored.append((score, {**candidate, "matched_model_keys": intersection}))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    top_score, top_candidate = scored[0]
    if len(scored) > 1 and scored[1][0] == top_score:
        return {"match_status": MATCH_STATUS_AMBIGUOUS}
    return {**top_candidate, "match_status": MATCH_STATUS_MATCHED_BY_COMPATIBILITY}


def _apply_match_resolution(line: dict[str, Any], resolution: Mapping[str, Any] | None) -> None:
    if not resolution:
        line["internal_sku"] = ""
        line["internal_nm_id"] = None
        line["internal_name"] = ""
        line["match_status"] = MATCH_STATUS_UNMATCHED
        line["manual_override"] = False
        return
    if str(resolution.get("match_status") or "") == MATCH_STATUS_AMBIGUOUS:
        line["internal_sku"] = ""
        line["internal_nm_id"] = None
        line["internal_name"] = ""
        line["match_status"] = MATCH_STATUS_AMBIGUOUS
        line["manual_override"] = False
        return
    line["internal_sku"] = str(resolution.get("internal_sku") or resolution.get("our_sku") or "")
    line["internal_nm_id"] = _optional_int(resolution.get("internal_nm_id") or resolution.get("nm_id"))
    line["internal_name"] = str(resolution.get("internal_name") or resolution.get("nomenclature_name") or "")
    line["match_status"] = str(resolution.get("match_status") or MATCH_STATUS_MATCHED)
    line["manual_override"] = False


def _normalize_nomenclature_payload(
    payload: Mapping[str, Any],
    *,
    item_id: str,
    created_at: str,
    updated_at: str,
) -> dict[str, Any]:
    product_type = str(payload.get("product_type") or "").strip()
    if product_type not in {"clear", "anti_spy", "matte", "extra", "other"}:
        raise ValueError("nomenclature product_type must be clear, anti_spy, matte, extra or other")
    is_active = bool(payload.get("is_active", True))
    nomenclature_name = str(payload.get("nomenclature_name") or "").strip()
    match_key = _normalize_match_key(payload.get("match_key"))
    compatible_models_text = str(payload.get("compatible_models_text") or "").strip()
    compatible_model_keys = _normalize_compatible_model_keys(
        payload.get("compatible_model_keys"),
        fallback_text=compatible_models_text,
        item_hint={**dict(payload), "match_key": match_key, "nomenclature_name": nomenclature_name},
    )
    if not compatible_models_text and compatible_model_keys:
        compatible_models_text = _compatible_models_text_from_keys(compatible_model_keys)
    if is_active and product_type in {"clear", "anti_spy", "matte"}:
        if not match_key:
            raise ValueError("active product nomenclature item requires match_key")
        if not nomenclature_name:
            raise ValueError("active product nomenclature item requires nomenclature_name")
    return {
        "item_id": item_id,
        "is_active": is_active,
        "our_sku": str(payload.get("our_sku") or "").strip(),
        "nm_id": _optional_int(payload.get("nm_id")),
        "nomenclature_name": nomenclature_name,
        "product_type": product_type,
        "match_key": match_key,
        "aliases": _normalize_alias_list(payload.get("aliases")),
        "compatible_models_text": compatible_models_text,
        "compatible_model_keys": compatible_model_keys,
        "comment": str(payload.get("comment") or "").strip(),
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _normalize_match_key(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    normalized = normalized.replace(" ", "_")
    normalized = re.sub(r"_+", "_", normalized)
    normalized = normalized.strip("_")
    if normalized and "|" not in normalized:
        raise ValueError("nomenclature match_key must use product_type|normalized_model")
    return normalized


def _normalize_alias_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = re.split(r"[\n,;]+", value)
    elif isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        raw_items = []
    aliases: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        alias = str(item or "").strip()
        if not alias or alias in seen:
            continue
        seen.add(alias)
        aliases.append(alias)
    return aliases


def _normalize_compatible_model_keys(
    value: Any,
    *,
    fallback_text: str = "",
    item_hint: Mapping[str, Any] | None = None,
) -> list[str]:
    raw_items: list[str]
    if isinstance(value, str):
        raw_items = re.split(r"[\n,;]+", value)
    elif isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        raw_items = []
    keys: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        for key in extract_iphone_model_keys(raw_item):
            if key not in seen:
                seen.add(key)
                keys.append(key)
    for source in [fallback_text]:
        for key in extract_iphone_model_keys(source):
            if key not in seen:
                seen.add(key)
                keys.append(key)
    if not keys and item_hint:
        for key in _infer_compatible_model_keys(item_hint):
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def _line_compatible_model_keys(line: Mapping[str, Any]) -> list[str]:
    parts = [
        line.get("model_raw"),
        line.get("model_normalized"),
        str(line.get("match_key") or "").split("|", 1)[1] if "|" in str(line.get("match_key") or "") else "",
    ]
    keys: list[str] = []
    seen: set[str] = set()
    for part in parts:
        for key in extract_iphone_model_keys(part):
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def _infer_compatible_model_keys(item: Mapping[str, Any]) -> list[str]:
    raw_keys = item.get("compatible_model_keys")
    if isinstance(raw_keys, list):
        keys = [str(key).strip() for key in raw_keys if str(key or "").strip()]
        if keys:
            return _dedupe(keys)
    keys: list[str] = []
    seen: set[str] = set()
    sources: list[Any] = [
        item.get("compatible_models_text"),
        str(item.get("match_key") or "").split("|", 1)[1] if "|" in str(item.get("match_key") or "") else "",
        item.get("nomenclature_name"),
    ]
    sources.extend(item.get("aliases") or [])
    for source in sources:
        for key in extract_iphone_model_keys(source):
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def _nomenclature_item_match_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "active": True,
        "item_id": str(item.get("item_id") or ""),
        "product_type": str(item.get("product_type") or ""),
        "factory_type": str(item.get("product_type") or ""),
        "internal_sku": str(item.get("our_sku") or item.get("internal_sku") or ""),
        "internal_nm_id": _optional_int(item.get("nm_id") or item.get("internal_nm_id")),
        "internal_name": str(item.get("nomenclature_name") or item.get("internal_name") or ""),
        "nomenclature_name": str(item.get("nomenclature_name") or item.get("internal_name") or ""),
        "match_key": str(item.get("match_key") or ""),
        "compatible_model_keys": _infer_compatible_model_keys(item),
        "group": "nomenclature",
    }


def _compatible_models_text_from_keys(keys: list[str]) -> str:
    return ", ".join(_model_key_to_label(key) for key in keys)


def _model_key_to_label(key: str) -> str:
    normalized = str(key or "").strip()
    if not normalized.startswith("iphone_"):
        return normalized
    parts = normalized.removeprefix("iphone_").split("_")
    if not parts:
        return normalized
    number = parts[0]
    suffix = " ".join(part.capitalize() if part != "e" else "e" for part in parts[1:])
    return ("iPhone " + number + (" " + suffix if suffix else "")).strip()


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = str(item or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _nomenclature_item_aliases(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not bool(item.get("is_active")):
        return []
    base_match_key = str(item.get("match_key") or "").strip()
    payload_base = {
        "active": True,
        "product_type": str(item.get("product_type") or ""),
        "factory_type": str(item.get("product_type") or ""),
        "internal_sku": str(item.get("our_sku") or ""),
        "internal_nm_id": _optional_int(item.get("nm_id")),
        "internal_name": str(item.get("nomenclature_name") or ""),
        "nomenclature_name": str(item.get("nomenclature_name") or ""),
        "group": "nomenclature",
    }
    aliases: list[dict[str, Any]] = []
    if base_match_key:
        aliases.append({**payload_base, "match_key": base_match_key})
    product_type = str(item.get("product_type") or "").strip()
    for raw_alias in item.get("aliases") or []:
        alias_text = str(raw_alias or "").strip()
        if not alias_text:
            continue
        if "|" in alias_text:
            aliases.append({**payload_base, "match_key": _normalize_match_key(alias_text)})
            continue
        normalized_model = normalize_invoice_model(alias_text)
        if product_type and normalized_model:
            aliases.append(
                {
                    **payload_base,
                    "normalized_model": normalized_model,
                    "match_key": f"{product_type}|{normalized_model}",
                }
            )
    return aliases


def _product_type_from_config_item(display_name: str, group: str) -> str:
    text = f"{group} {display_name}".lower()
    if "anti" in text and "spy" in text:
        return "anti_spy"
    if "matte" in text:
        return "matte"
    if "clean" in text or "clear" in text:
        return "clear"
    return ""


def _model_text_from_nomenclature_name(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\s*(clean|clear|matte|anti[-\s]?spy)\s+", "", text, flags=re.IGNORECASE)
    return text.strip()


def _invoice_download_path(shipment_id: str) -> str:
    if not shipment_id:
        return ""
    return f"/v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/invoice"


def _safe_filename(value: str) -> str:
    name = Path(str(value or "")).name.strip()
    name = name.replace("\x00", "").replace("/", "_").replace("\\", "_")
    name = re.sub(r"[\r\n\t]+", " ", name).strip()
    if not name:
        name = "supplier-invoice.xlsx"
    if len(name) > 180:
        stem = Path(name).stem[:150] or "supplier-invoice"
        suffix = Path(name).suffix[:16] or ".xlsx"
        name = stem + suffix
    return name


def _relative_to_runtime(runtime_dir: Path, path: Path) -> str:
    root = runtime_dir.resolve()
    resolved = path.resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError("runtime file path escapes runtime dir")
    return resolved.relative_to(root).as_posix()


def _validate_iso_date(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("shipment_date is required")
    try:
        date.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("shipment_date must be an ISO date YYYY-MM-DD") from exc
    return normalized


def _optional_iso_date(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    try:
        date.fromisoformat(normalized)
    except ValueError:
        return normalized
    return normalized


def _optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\u00a0", " ")
    if not text:
        return None
    text = re.sub(r"[^0-9,.\-]", "", text)
    if not text or text in {"-", ".", ","}:
        return None
    if "," in text and "." in text:
        text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sum_numeric(values: Any) -> float:
    total = 0.0
    for value in values:
        number = _optional_number(value)
        if number is not None:
            total += number
    return round(total, 2)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _default_timestamp_factory() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
