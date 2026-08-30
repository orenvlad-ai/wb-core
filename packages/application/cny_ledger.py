"""Server-owned CNY account ledger for supplier payments."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.supplier_financial_documents import (
    extract_pdf_text_layer,
    parse_financial_document_text,
)
from packages.contracts.cny_ledger import (
    CNY_CALC_STATUS_AMBIGUOUS_ALLOCATION,
    CNY_CALC_STATUS_DOCUMENT_DATE_MISSING,
    CNY_CALC_STATUS_INSUFFICIENT_BALANCE,
    CNY_CALC_STATUS_MISSING_OPENING_BALANCE,
    CNY_CALC_STATUS_NO_SUPPLIER_PAYMENT,
    CNY_CALC_STATUS_OK,
    CNY_CALC_STATUS_PARSE_ERROR,
    CNY_CALC_STATUS_PAYMENT_NOT_LINKED,
    CNY_DOCUMENT_SOURCE_CNY_ACCOUNT,
    CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER,
    CNY_DOCUMENT_STATUS_EXCLUDED,
    CNY_DOCUMENT_STATUS_NEEDS_REVIEW,
    CNY_DOCUMENT_STATUS_PARSE_ERROR,
    CNY_DOCUMENT_STATUS_POSTED,
    CNY_DOCUMENT_TYPE_ADJUSTMENT,
    CNY_DOCUMENT_TYPE_BANK_FEE,
    CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE,
    CNY_DOCUMENT_TYPE_OPENING_BALANCE,
    CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT,
    CNY_LEDGER_ALLOWED_EXTENSIONS,
    CNY_LEDGER_CONTENT_TYPE,
    CNY_LEDGER_CONTRACT_NAME,
    CNY_LEDGER_OPERATION_ADJUSTMENT,
    CNY_LEDGER_OPERATION_CONVERSION_FEE,
    CNY_LEDGER_OPERATION_CONVERSION_IN,
    CNY_LEDGER_OPERATION_OPENING_BALANCE,
    CNY_LEDGER_OPERATION_STATUS_BLOCKED,
    CNY_LEDGER_OPERATION_STATUS_NEEDS_REVIEW,
    CNY_LEDGER_OPERATION_STATUS_POSTED,
    CNY_LEDGER_OPERATION_STATUS_SKIPPED,
    CNY_LEDGER_OPERATION_SUPPLIER_PAYMENT_OUT,
    CNY_LEDGER_OPERATION_TRANSFER_FEE,
    CNY_LEDGER_PARSER_VERSION,
)
from packages.contracts.supplier_financial_documents import (
    FINANCIAL_DOCUMENT_PARSE_STATUS_CONFIRMED,
)
from packages.contracts.supplier_financial_documents import FINANCIAL_DOCUMENT_TYPE_BANK_TRANSFER_APPLICATION


MONEY_QUANT = Decimal("0.01")
RATE_QUANT = Decimal("0.000001")
PUBLIC_CNY_DOCUMENT_FILE_PREFIX = "/v1/sheet-vitrina-v1/supply/cny-account/documents"
CNY_WRITE_PENDING_CONTRACT_NAME = "sheet_vitrina_v1_cny_write_pending_v1"
DATE_ONLY_OPERATION_PRIORITY = {
    CNY_LEDGER_OPERATION_OPENING_BALANCE: 0,
    CNY_LEDGER_OPERATION_CONVERSION_IN: 1,
    CNY_LEDGER_OPERATION_CONVERSION_FEE: 2,
    CNY_LEDGER_OPERATION_TRANSFER_FEE: 3,
    "bank_fee": 3,
    CNY_LEDGER_OPERATION_SUPPLIER_PAYMENT_OUT: 4,
    CNY_LEDGER_OPERATION_ADJUSTMENT: 5,
}
DATE_ONLY_OPERATION_START_OF_DAY_TYPES = {
    CNY_LEDGER_OPERATION_OPENING_BALANCE,
    CNY_LEDGER_OPERATION_CONVERSION_IN,
    CNY_LEDGER_OPERATION_CONVERSION_FEE,
    CNY_LEDGER_OPERATION_TRANSFER_FEE,
    "bank_fee",
}

TextExtractor = Callable[[bytes, str], tuple[str, dict[str, Any], list[str]]]


class CnyLedgerBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        timestamp_factory: Callable[[], str] | None = None,
        pdf_text_extractor: TextExtractor | None = None,
    ) -> None:
        self.runtime = runtime
        self.timestamp_factory = timestamp_factory or _default_timestamp_factory
        self.pdf_text_extractor = pdf_text_extractor or extract_pdf_text_layer

    def get_status(self) -> dict[str, Any]:
        documents = [self._with_download_path(item) for item in self.runtime.list_cny_documents()]
        operations = self.runtime.list_cny_ledger_operations()
        replay_state = self.runtime.load_cny_ledger_replay_state() or {}
        return {
            "contract_name": CNY_LEDGER_CONTRACT_NAME,
            "status": "ok",
            "summary": _ledger_summary(documents, operations, replay_state),
            "documents": documents,
            "conversions": [
                _conversion_row(item)
                for item in documents
                if item.get("document_type") == CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE
                and str(item.get("status") or "") != CNY_DOCUMENT_STATUS_EXCLUDED
            ],
            "ledger_operations": operations,
            "replay": replay_state,
            "diagnostics": _ledger_diagnostics(operations, replay_state),
            "empty_state": "Загрузите документы конвертации или задайте opening balance" if not documents else "",
        }

    def list_conversions(self) -> dict[str, Any]:
        payload = self.get_status()
        return {
            "contract_name": CNY_LEDGER_CONTRACT_NAME,
            "status": "ok",
            "conversions": payload["conversions"],
            "summary": payload["summary"],
            "replay": payload["replay"],
        }

    def list_ledger_operations(self) -> dict[str, Any]:
        payload = self.get_status()
        return {
            "contract_name": CNY_LEDGER_CONTRACT_NAME,
            "status": "ok",
            "operations": payload["ledger_operations"],
            "summary": payload["summary"],
            "replay": payload["replay"],
        }

    def parse_document_preview(
        self,
        file_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
    ) -> dict[str, Any]:
        filename = _safe_filename(uploaded_filename or "cny-document.pdf")
        return parse_cny_document_pdf(file_bytes, filename=filename, text_extractor=self.pdf_text_extractor)

    def upload_document(
        self,
        *,
        file_bytes: bytes,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
        source: str = CNY_DOCUMENT_SOURCE_CNY_ACCOUNT,
        source_order_id: str | None = None,
        context_order_id: str | None = None,
        stored_file_path: str | None = None,
        linked_financial_document_id: str | None = None,
        reject_unsupported: bool = True,
        manual_payment_date: str | None = None,
        manual_payment_date_actor: str | None = None,
    ) -> dict[str, Any]:
        if not file_bytes:
            raise ValueError("CNY document upload file is empty")
        filename = _safe_filename(uploaded_filename or "cny-document.pdf")
        if Path(filename).suffix.lower() not in CNY_LEDGER_ALLOWED_EXTENSIONS:
            raise ValueError("CNY document upload must be a PDF file")
        content_type = str(uploaded_content_type or "").split(";", 1)[0].strip().lower() or CNY_LEDGER_CONTENT_TYPE
        parsed = parse_cny_document_pdf(file_bytes, filename=filename, text_extractor=self.pdf_text_extractor)
        normalized = dict(parsed.get("normalized_parse") or {})
        document_type = str(normalized.get("document_type") or "")
        if not document_type and reject_unsupported:
            raise ValueError("uploaded PDF was not recognized as a CNY conversion or supplier CNY payment document")
        if not document_type:
            document_type = CNY_DOCUMENT_TYPE_ADJUSTMENT
        if document_type in {CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE, CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT}:
            required_non_date = {
                CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE: ("rub_amount", "cny_amount", "currency"),
                CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT: ("cny_amount", "currency"),
            }[document_type]
            missing_non_date = [field for field in required_non_date if not normalized.get(field)]
            if missing_non_date:
                raise ValueError(
                    "payment document rejected before durable save; parser did not recognize: "
                    + ", ".join(missing_non_date)
                )
            parsed_date = str(normalized.get("document_date") or "").strip()
            manual_date = str(manual_payment_date or "").strip()
            if not parsed_date and not manual_date:
                return {
                    "contract_name": CNY_LEDGER_CONTRACT_NAME,
                    "status": "payment_date_required",
                    "preview_required": True,
                    "durable_saved": False,
                    "manual_field": "payment_date",
                    "message": "Дата платежа не распознана. Укажите и подтвердите дату платежа.",
                    "normalized_parse": normalized,
                    "warnings": _dedupe_strings(_string_list(parsed.get("warnings"))),
                }
            if manual_date:
                normalized["document_date"] = _optional_iso_date(manual_date)
                if not normalized["document_date"]:
                    raise ValueError("manual payment_date must be YYYY-MM-DD")
                normalized["payment_date_provenance"] = {
                    "source": "manual_operator_confirmation",
                    "actor": str(manual_payment_date_actor or "").strip(),
                    "confirmed_at": self.timestamp_factory(),
                }
            else:
                normalized["payment_date_provenance"] = {"source": "parsed_document"}
        now = self.timestamp_factory()
        file_sha256 = hashlib.sha256(file_bytes).hexdigest()
        document_id = "cnydoc_" + uuid4().hex
        natural_key = _document_natural_key(document_type=document_type, file_sha256=file_sha256, normalized=normalized)
        existing = self.runtime.load_cny_document_by_natural_key(natural_key)
        if existing is not None:
            existing_id = str(existing.get("document_id") or "")
            if source_order_id and not str(existing.get("source_order_id") or "").strip():
                existing = self.runtime.update_cny_document_context(
                    document_id=existing_id,
                    source_order_id=str(source_order_id or "").strip(),
                    context_order_id=str(context_order_id or source_order_id or "").strip(),
                    updated_at=now,
                )
            replay = self.replay_ledger(reason="idempotent_document_upload")
            payload = {
                **self._with_download_path(existing),
                "idempotent": True,
                "replay": replay.get("replay") or replay,
            }
            queue = self._enqueue_functional_recalculation(existing)
            return self._with_truthful_write_outcome(
                payload,
                replay=replay,
                queue=queue,
                success_message="Документ CNY уже сохранён; ledger подтверждён.",
            )
        if stored_file_path:
            relative_path = str(stored_file_path)
        else:
            relative_path = self._write_document_file(document_id=document_id, filename=filename, body=file_bytes)
        warnings = _string_list(parsed.get("warnings"))
        errors = _string_list(parsed.get("errors"))
        status = _document_status_for_parse(document_type, normalized, warnings, errors)
        operation_datetime, operation_date = _operation_time_fields(normalized)
        document = {
            "document_id": document_id,
            "document_type": document_type,
            "source": source if source in {CNY_DOCUMENT_SOURCE_CNY_ACCOUNT, CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER} else CNY_DOCUMENT_SOURCE_CNY_ACCOUNT,
            "source_order_id": str(source_order_id or "").strip(),
            "context_order_id": str(context_order_id or source_order_id or "").strip(),
            "linked_financial_document_id": str(linked_financial_document_id or "").strip(),
            "original_filename": filename,
            "stored_file_path": relative_path,
            "file_content_type": content_type,
            "file_sha256": file_sha256,
            "natural_key": natural_key,
            "uploaded_at": now,
            "created_at": now,
            "updated_at": now,
            "operation_date": operation_date,
            "operation_datetime": operation_datetime,
            "status": status,
            "document_number": normalized.get("document_number") or "",
            "currency": normalized.get("currency") or "CNY",
            "rub_amount": _decimal_to_storage(_parse_decimal(normalized.get("rub_amount"))),
            "cny_amount": _decimal_to_storage(
                _parse_decimal(normalized.get("cny_amount") or normalized.get("transfer_amount"))
            ),
            "bank_rate": _decimal_to_storage(_parse_decimal(normalized.get("bank_rate"))),
            "parsed_payload": normalized,
            "raw_parse": dict(parsed.get("raw_parse") or {}),
            "parser_version": parsed.get("parser_version") or CNY_LEDGER_PARSER_VERSION,
            "warnings": _dedupe_strings(warnings),
            "errors": _dedupe_strings(errors),
        }
        saved = self.runtime.save_cny_document(document)
        replay = self.replay_ledger(reason="document_upload")
        payload = {
            **self._with_download_path(saved),
            "idempotent": False,
            "replay": replay.get("replay") or replay,
        }
        queue = self._enqueue_functional_recalculation(saved)
        payload["warehouse_targeted_recalculation"] = queue
        return self._with_truthful_write_outcome(
            payload,
            replay=replay,
            queue=queue,
            success_message="Документ CNY сохранён и ledger пересчитан.",
        )

    def create_opening_balance(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        operation_date = _optional_iso_date(payload.get("operation_date") or payload.get("effective_date"))
        if not operation_date:
            raise ValueError("operation_date is required for opening balance")
        cny_amount = _parse_decimal(payload.get("cny_amount") or payload.get("opening_cny_balance"))
        rub_value = _parse_decimal(payload.get("rub_value") or payload.get("opening_rub_value"))
        average_rate = _parse_decimal(payload.get("average_rate") or payload.get("opening_average_rate"))
        if cny_amount is None or cny_amount <= 0:
            raise ValueError("opening CNY balance must be > 0")
        if rub_value is None:
            if average_rate is None or average_rate <= 0:
                raise ValueError("opening RUB value or average_rate is required")
            rub_value = cny_amount * average_rate
        if rub_value <= 0:
            raise ValueError("opening RUB value must be > 0")
        now = self.timestamp_factory()
        normalized = {
            "document_type": CNY_DOCUMENT_TYPE_OPENING_BALANCE,
            "document_number": str(payload.get("document_number") or "opening_balance"),
            "document_date": operation_date,
            "operation_date": operation_date,
            "currency": "CNY",
            "cny_amount": _decimal_to_storage(cny_amount),
            "rub_value": _decimal_to_storage(rub_value),
            "average_rate": _decimal_to_storage(_safe_div(rub_value, cny_amount)),
            "comment": str(payload.get("comment") or "").strip(),
        }
        natural_key = "opening_balance:" + operation_date
        existing = self.runtime.load_cny_document_by_natural_key(natural_key)
        document_id = str((existing or {}).get("document_id") or "cnydoc_" + uuid4().hex)
        document = {
            "document_id": document_id,
            "document_type": CNY_DOCUMENT_TYPE_OPENING_BALANCE,
            "source": CNY_DOCUMENT_SOURCE_CNY_ACCOUNT,
            "source_order_id": "",
            "context_order_id": "",
            "linked_financial_document_id": "",
            "original_filename": "",
            "stored_file_path": "",
            "file_content_type": "",
            "file_sha256": "",
            "natural_key": natural_key,
            "uploaded_at": (existing or {}).get("uploaded_at") or now,
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
            "operation_date": operation_date,
            "operation_datetime": operation_date + "T00:00:00Z",
            "status": CNY_DOCUMENT_STATUS_POSTED,
            "document_number": normalized["document_number"],
            "currency": "CNY",
            "rub_amount": _decimal_to_storage(rub_value),
            "cny_amount": _decimal_to_storage(cny_amount),
            "bank_rate": _decimal_to_storage(_safe_div(rub_value, cny_amount)),
            "parsed_payload": normalized,
            "raw_parse": {},
            "parser_version": CNY_LEDGER_PARSER_VERSION,
            "warnings": [],
            "errors": [],
        }
        saved = self.runtime.save_cny_document(document)
        replay = self.replay_ledger(reason="opening_balance")
        result = {
            **self._with_download_path(saved),
            "idempotent": existing is not None,
            "replay": replay.get("replay") or replay,
        }
        queue = self._enqueue_functional_recalculation(saved)
        result["warehouse_targeted_recalculation"] = queue
        return self._with_truthful_write_outcome(
            result,
            replay=replay,
            queue=queue,
            success_message="Opening balance сохранён, ledger пересчитан.",
        )

    def save_bank_fee_document(
        self,
        *,
        source_order_id: str,
        linked_financial_document_id: str,
        natural_key: str,
        fee_row: Mapping[str, Any],
        original_filename: str = "",
        stored_file_path: str = "",
        file_content_type: str = "",
        replay: bool = True,
    ) -> dict[str, Any]:
        normalized_order_id = str(source_order_id or "").strip()
        normalized_natural_key = str(natural_key or "").strip()
        if not normalized_order_id:
            raise ValueError("source_order_id is required for CNY bank fee")
        if not normalized_natural_key:
            raise ValueError("natural_key is required for CNY bank fee")
        existing = self.runtime.load_cny_document_by_natural_key(normalized_natural_key)
        if existing is not None:
            if not replay:
                return {
                    **self._with_download_path(existing),
                    "idempotent": True,
                }
            replay_result = self.replay_ledger(reason="idempotent_bank_fee_import")
            queue = self._enqueue_functional_recalculation(existing)
            return self._with_truthful_write_outcome(
                {
                    **self._with_download_path(existing),
                    "idempotent": True,
                    "replay": replay_result.get("replay") or replay_result,
                    "warehouse_targeted_recalculation": queue,
                },
                replay=replay_result,
                queue=queue,
                success_message="Комиссия CNY уже сохранена; ledger подтверждён.",
            )
        document = self.build_bank_fee_document(
            source_order_id=normalized_order_id,
            linked_financial_document_id=linked_financial_document_id,
            natural_key=normalized_natural_key,
            fee_row=fee_row,
            original_filename=original_filename,
            stored_file_path=stored_file_path,
            file_content_type=file_content_type,
        )
        saved = self.runtime.save_cny_document(document)
        if not replay:
            return {
                **self._with_download_path(saved),
                "idempotent": False,
            }
        replay_result = self.replay_ledger(reason="bank_fee_import")
        queue = self._enqueue_functional_recalculation(saved)
        return self._with_truthful_write_outcome(
            {
                **self._with_download_path(saved),
                "idempotent": False,
                "replay": replay_result.get("replay") or replay_result,
                "warehouse_targeted_recalculation": queue,
            },
            replay=replay_result,
            queue=queue,
            success_message="Комиссия CNY сохранена; ledger подтверждён.",
        )

    def build_bank_fee_document(
        self,
        *,
        source_order_id: str,
        linked_financial_document_id: str,
        natural_key: str,
        fee_row: Mapping[str, Any],
        original_filename: str = "",
        stored_file_path: str = "",
        file_content_type: str = "",
    ) -> dict[str, Any]:
        """Build a deterministic CNY projection for an atomic parent commit."""

        normalized_order_id = str(source_order_id or "").strip()
        normalized_natural_key = str(natural_key or "").strip()
        if not normalized_order_id:
            raise ValueError("source_order_id is required for CNY bank fee")
        if not normalized_natural_key:
            raise ValueError("natural_key is required for CNY bank fee")
        amount = _parse_decimal(fee_row.get("amount") or fee_row.get("debit_cny"))
        if amount is None or amount <= 0:
            raise ValueError("CNY bank fee amount must be > 0")
        now = self.timestamp_factory()
        operation_datetime = _normalize_datetime(fee_row.get("operation_datetime"))
        operation_date = (
            _optional_iso_date(operation_datetime)
            or _optional_iso_date(fee_row.get("operation_date"))
            or _optional_iso_date(fee_row.get("document_date"))
        )
        row_id = str(fee_row.get("row_id") or "").strip()
        document_id = (
            "cnydoc_bankfee_"
            + hashlib.sha256(
                normalized_natural_key.encode("utf-8")
            ).hexdigest()[:24]
        )
        normalized = {
            "document_type": CNY_DOCUMENT_TYPE_BANK_FEE,
            "document_number": str(fee_row.get("bank_document_number") or row_id or "bank_fee"),
            "document_date": operation_date,
            "operation_date": operation_date,
            "operation_datetime": operation_datetime,
            "currency": "CNY",
            "fee_cny": _decimal_to_storage(amount),
            "fee_category": str(fee_row.get("fee_category") or ""),
            "fee_category_label": str(fee_row.get("fee_category_label") or ""),
            "source_statement_row_id": row_id,
            "source_statement_document_id": str(linked_financial_document_id or "").strip(),
            "matched_anchor_document_id": str(fee_row.get("matched_anchor_document_id") or ""),
            "matched_anchor_operation_number": str(fee_row.get("matched_anchor_operation_number") or ""),
            "match_confidence": str(fee_row.get("confidence") or ""),
            "match_reasons": _string_list(fee_row.get("match_reasons")),
            "payment_purpose": str(fee_row.get("payment_purpose") or ""),
        }
        document = {
            "document_id": document_id,
            "document_type": CNY_DOCUMENT_TYPE_BANK_FEE,
            "source": CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER,
            "source_order_id": normalized_order_id,
            "context_order_id": normalized_order_id,
            "linked_financial_document_id": str(linked_financial_document_id or "").strip(),
            "original_filename": original_filename,
            "stored_file_path": stored_file_path,
            "file_content_type": file_content_type,
            "file_sha256": "",
            "natural_key": normalized_natural_key,
            "uploaded_at": now,
            "created_at": now,
            "updated_at": now,
            "operation_date": operation_date,
            "operation_datetime": operation_datetime,
            "status": CNY_DOCUMENT_STATUS_POSTED if operation_date else CNY_DOCUMENT_STATUS_NEEDS_REVIEW,
            "document_number": normalized["document_number"],
            "currency": "CNY",
            "rub_amount": "",
            "cny_amount": _decimal_to_storage(amount),
            "bank_rate": "",
            "parsed_payload": normalized,
            "raw_parse": {"source": "bank_fee_statement", "row": dict(fee_row)},
            "parser_version": CNY_LEDGER_PARSER_VERSION,
            "warnings": [] if operation_date else ["Missing bank fee operation date"],
            "errors": [],
        }
        return document

    def replay_ledger(self, *, reason: str = "manual") -> dict[str, Any]:
        now = self.timestamp_factory()
        self._sync_supplier_payment_documents_from_financial_documents(now=now)
        from packages.application.own_product_capital import OwnProductCapitalBlock

        capital = OwnProductCapitalBlock(
            runtime=self.runtime,
            timestamp_factory=self.timestamp_factory,
        )
        for item in self.runtime.list_cny_documents():
            if (
                str(item.get("document_type") or "") == CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT
                and str(item.get("status") or "") == CNY_DOCUMENT_STATUS_EXCLUDED
            ):
                capital.remove_supplier_payment(
                    str(item.get("document_id") or ""),
                    recalculate=False,
                )
        existing_operations = {
            str(item.get("operation_id") or ""): dict(item)
            for item in self.runtime.list_cny_ledger_operations()
        }
        documents = [
            dict(item)
            for item in self.runtime.list_cny_documents()
            if str(item.get("status") or "") != "excluded"
        ]
        planned_operations = _build_planned_operations(documents, created_at=now)
        planned_operations.sort(key=_operation_sort_key)

        balance_cny = Decimal("0")
        balance_rub = Decimal("0")
        posted_operations: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        order_accumulator: dict[str, dict[str, Decimal | str | list[str]]] = {}
        previous_positive_operation_seen = False

        for index, operation in enumerate(planned_operations, start=1):
            op = dict(operation)
            op["sequence_key"] = f"{index:08d}:{_operation_sort_key(op)}"
            op["effective_rate_before"] = _decimal_to_storage(_safe_div(balance_rub, balance_cny))
            op["balance_cny_after"] = _decimal_to_storage(balance_cny)
            op["balance_rub_value_after"] = _decimal_to_storage(balance_rub)
            op["average_rate_after"] = _decimal_to_storage(_safe_div(balance_rub, balance_cny))
            op.setdefault("status", CNY_LEDGER_OPERATION_STATUS_POSTED)
            op.setdefault("error_reason", "")
            if not op.get("operation_date"):
                _block_operation(op, CNY_CALC_STATUS_DOCUMENT_DATE_MISSING)
            elif op.get("status") == CNY_LEDGER_OPERATION_STATUS_SKIPPED:
                pass
            elif op["operation_type"] == CNY_LEDGER_OPERATION_OPENING_BALANCE:
                cny_delta = _parse_decimal(op.get("cny_delta")) or Decimal("0")
                rub_delta = _parse_decimal(op.get("rub_value_delta")) or Decimal("0")
                balance_cny += cny_delta
                balance_rub += rub_delta
                previous_positive_operation_seen = previous_positive_operation_seen or cny_delta > 0
            elif op["operation_type"] == CNY_LEDGER_OPERATION_CONVERSION_IN:
                cny_delta = _parse_decimal(op.get("cny_delta")) or Decimal("0")
                rub_delta = _parse_decimal(op.get("rub_value_delta")) or Decimal("0")
                if cny_delta <= 0 or rub_delta <= 0:
                    _block_operation(op, CNY_CALC_STATUS_PARSE_ERROR)
                else:
                    balance_cny += cny_delta
                    balance_rub += rub_delta
                    previous_positive_operation_seen = True
            elif op["operation_type"] == CNY_LEDGER_OPERATION_CONVERSION_FEE:
                rub_delta = _parse_decimal(op.get("rub_value_delta")) or Decimal("0")
                if rub_delta > 0:
                    balance_rub += rub_delta
                else:
                    _block_operation(op, CNY_CALC_STATUS_PARSE_ERROR)
            elif op["operation_type"] == CNY_LEDGER_OPERATION_SUPPLIER_PAYMENT_OUT:
                paid_cny = abs(_parse_decimal(op.get("cny_delta")) or Decimal("0"))
                order_id = str(op.get("source_order_id") or "").strip()
                if not order_id:
                    _block_operation(op, CNY_CALC_STATUS_PAYMENT_NOT_LINKED)
                elif paid_cny <= 0:
                    _block_operation(op, CNY_CALC_STATUS_PARSE_ERROR)
                elif balance_cny < paid_cny:
                    _block_operation(
                        op,
                        CNY_CALC_STATUS_MISSING_OPENING_BALANCE
                        if not previous_positive_operation_seen
                        else CNY_CALC_STATUS_INSUFFICIENT_BALANCE,
                    )
                else:
                    rate_before = _safe_div(balance_rub, balance_cny)
                    payment_cost = _quantize_money(paid_cny * (rate_before or Decimal("0")))
                    balance_cny -= paid_cny
                    balance_rub -= payment_cost
                    op["rub_value_delta"] = _decimal_to_storage(-payment_cost)
                    op["effective_rate_before"] = _decimal_to_storage(rate_before)
                    bucket = order_accumulator.setdefault(
                        order_id,
                        {
                            "paid_cny": Decimal("0"),
                            "payment_currency_rub_cost": Decimal("0"),
                            "bank_fee_rub": Decimal("0"),
                            "status": CNY_CALC_STATUS_OK,
                            "errors": [],
                        },
                    )
                    bucket["paid_cny"] = _as_decimal(bucket["paid_cny"]) + paid_cny
                    bucket["payment_currency_rub_cost"] = _as_decimal(bucket["payment_currency_rub_cost"]) + payment_cost
            elif op["operation_type"] == CNY_LEDGER_OPERATION_TRANSFER_FEE:
                order_id = str(op.get("source_order_id") or "").strip()
                fee_rub = abs(_parse_decimal(op.get("rub_value_delta")) or Decimal("0"))
                fee_cny = abs(_parse_decimal(op.get("cny_delta")) or Decimal("0"))
                if fee_cny > 0 and balance_cny >= fee_cny:
                    rate_before = _safe_div(balance_rub, balance_cny) or Decimal("0")
                    fee_rub = _quantize_money(fee_cny * rate_before)
                    balance_cny -= fee_cny
                    balance_rub -= fee_rub
                    op["effective_rate_before"] = _decimal_to_storage(rate_before)
                    op["rub_value_delta"] = _decimal_to_storage(-fee_rub)
                elif fee_cny > 0:
                    _block_operation(op, CNY_CALC_STATUS_INSUFFICIENT_BALANCE)
                if order_id and fee_rub > 0 and op.get("status") != CNY_LEDGER_OPERATION_STATUS_BLOCKED:
                    bucket = order_accumulator.setdefault(
                        order_id,
                        {
                            "paid_cny": Decimal("0"),
                            "payment_currency_rub_cost": Decimal("0"),
                            "bank_fee_rub": Decimal("0"),
                            "status": CNY_CALC_STATUS_OK,
                            "errors": [],
                        },
                    )
                    bucket["bank_fee_rub"] = _as_decimal(bucket["bank_fee_rub"]) + fee_rub
            elif op["operation_type"] == CNY_LEDGER_OPERATION_ADJUSTMENT:
                balance_cny += _parse_decimal(op.get("cny_delta")) or Decimal("0")
                balance_rub += _parse_decimal(op.get("rub_value_delta")) or Decimal("0")
            else:
                _block_operation(op, CNY_CALC_STATUS_PARSE_ERROR)

            op["balance_cny_after"] = _decimal_to_storage(balance_cny)
            op["balance_rub_value_after"] = _decimal_to_storage(balance_rub)
            op["average_rate_after"] = _decimal_to_storage(_safe_div(balance_rub, balance_cny))
            if op.get("status") == CNY_LEDGER_OPERATION_STATUS_BLOCKED:
                diagnostics.append(
                    {
                        "document_id": op.get("source_document_id") or "",
                        "operation_type": op.get("operation_type") or "",
                        "status": op.get("error_reason") or "blocked",
                        "source_order_id": op.get("source_order_id") or "",
                    }
                )
                order_id = str(op.get("source_order_id") or "").strip()
                if order_id:
                    bucket = order_accumulator.setdefault(
                        order_id,
                        {
                            "paid_cny": Decimal("0"),
                            "payment_currency_rub_cost": Decimal("0"),
                            "bank_fee_rub": Decimal("0"),
                            "status": str(op.get("error_reason") or CNY_CALC_STATUS_PARSE_ERROR),
                            "errors": [],
                        },
                    )
                    bucket["status"] = str(op.get("error_reason") or CNY_CALC_STATUS_PARSE_ERROR)
                    errors = bucket.setdefault("errors", [])
                    if isinstance(errors, list):
                        errors.append(str(op.get("error_reason") or "blocked"))
            existing = existing_operations.get(str(op.get("operation_id") or ""))
            if existing is not None and _cny_operation_revision_payload(
                existing
            ) == _cny_operation_revision_payload(op):
                # ``updated_at`` is a semantic source revision used by guarded
                # downstream certification.  A full deterministic ledger
                # replay must not make every unchanged operation look newer.
                op["created_at"] = str(existing.get("created_at") or op.get("created_at") or "")
                op["updated_at"] = str(existing.get("updated_at") or op.get("updated_at") or "")
            posted_operations.append(op)

        self.runtime.replace_cny_ledger_operations(posted_operations)
        order_updates = self._build_order_updates(order_accumulator, calculated_at=now)
        self.runtime.update_supplier_shipments_cny_calculations(order_updates)
        replay_status = CNY_CALC_STATUS_OK if not diagnostics else "blocked"
        replay_state = {
            "status": replay_status,
            "reason": reason,
            "replayed_at": now,
            "operation_count": len(posted_operations),
            "document_count": len(documents),
            "balance_cny": _decimal_to_storage(balance_cny),
            "balance_rub_value": _decimal_to_storage(balance_rub),
            "average_rate": _decimal_to_storage(_safe_div(balance_rub, balance_cny)),
            "diagnostics": diagnostics,
            "own_product_capital_diagnostics": [],
        }
        self.runtime.save_cny_ledger_replay_state(replay_state)
        core_readback = self._confirm_ledger_replay_readback(
            documents=documents,
            posted_operations=posted_operations,
            replay_state=replay_state,
        )
        derived_error = ""
        own_capital_diagnostics: list[dict[str, Any]] = []
        try:
            own_capital_diagnostics = self._sync_own_product_capital_payments(
                posted_operations,
                recalculate=False,
            )
            if posted_operations:
                # CNY owns one preparation rebuild.  Projection outbox drain is
                # deferred to the durable targeted enqueue below, so a mixed
                # shipment scope cannot be drained shipment-by-shipment.
                capital.recalculate(drain_projection=False)
            replay_state["own_product_capital_diagnostics"] = (
                own_capital_diagnostics
            )
            self.runtime.save_cny_ledger_replay_state(replay_state)
        except Exception as exc:  # noqa: BLE001 - core ledger is already readback-confirmed.
            derived_error = str(exc).replace("\n", " ")[:1000]
        replay_fingerprint = "sha256:" + hashlib.sha256(
            json.dumps(
                [_cny_operation_revision_payload(item) for item in posted_operations],
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        result = {
            "contract_name": CNY_LEDGER_CONTRACT_NAME,
            "status": "pending" if derived_error else "ok",
            "replay": replay_state,
            "summary": _ledger_summary(documents, posted_operations, replay_state),
            "operation_applied": True,
            "readback_confirmed": bool(core_readback.get("confirmed")),
            "durable_retry_identity": {
                "stable_source_id": "cny_ledger:account",
                "source_revision": replay_fingerprint,
            },
        }
        if derived_error:
            result.update(
                {
                    "contract_name": CNY_WRITE_PENDING_CONTRACT_NAME,
                    "retryable": True,
                    "pending_phase": "derived_replay",
                    "message": (
                        "Операция CNY и ledger сохранены и подтверждены. "
                        "Связанный пересчёт будет безопасно повторён."
                    ),
                    "derived_replay_error": derived_error,
                    "http_status": 202,
                }
            )
        return result

    def replay_account(self, *, reason: str = "manual") -> dict[str, Any]:
        """Replay the durable ledger and enqueue one coalesced account revision."""

        replay = self.replay_ledger(reason=reason)
        documents = [
            dict(item)
            for item in self.runtime.list_cny_documents()
            if str(item.get("status") or "") != CNY_DOCUMENT_STATUS_EXCLUDED
        ]
        if documents:
            replay_identity = dict(replay.get("durable_retry_identity") or {})
            source_revision = str(replay_identity.get("source_revision") or "")
            operation_dates = sorted(
                {
                    str(item.get("operation_date") or "")[:10]
                    for item in documents
                    if len(str(item.get("operation_date") or "")) >= 10
                }
            )
            source = {
                "document_id": "account_replay",
                "document_type": "cny_account_replay",
                "natural_key": "cny_account_replay",
                "source_order_id": "",
                "context_order_id": "",
                "operation_date": operation_dates[0]
                if operation_dates
                else self.timestamp_factory()[:10],
                "status": "posted",
                "rub_amount": str(
                    (replay.get("replay") or {}).get("balance_rub_value") or ""
                ),
                "cny_amount": str((replay.get("replay") or {}).get("balance_cny") or ""),
                "bank_rate": str((replay.get("replay") or {}).get("average_rate") or ""),
                "file_sha256": source_revision,
                "updated_at": max(
                    str(item.get("updated_at") or "") for item in documents
                ),
            }
            queue = self._enqueue_functional_recalculation(source)
        else:
            queue = {
                "status": "no_op",
                "terminal_no_op": True,
                "diagnostic_code": "cny_account_has_no_active_documents",
            }
        payload = {
            **replay,
            "warehouse_targeted_recalculation": queue,
        }
        return self._with_truthful_write_outcome(
            payload,
            replay=replay,
            queue=queue,
            success_message="CNY ledger пересчитан.",
        )

    def reconcile_document(
        self,
        document_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Idempotently confirm one durable document and its derived replay."""

        document = self.runtime.load_cny_document(str(document_id or "").strip())
        if document is None:
            raise ValueError(f"CNY document not found: {document_id}")
        replay = self.replay_ledger(reason=reason)
        queue = self._enqueue_functional_recalculation(document)
        return self._with_truthful_write_outcome(
            {
                **self._with_download_path(document),
                "idempotent": True,
                "replay": replay.get("replay") or replay,
                "warehouse_targeted_recalculation": queue,
            },
            replay=replay,
            queue=queue,
            success_message="Документ CNY уже сохранён; ledger подтверждён.",
        )

    def _confirm_ledger_replay_readback(
        self,
        *,
        documents: Iterable[Mapping[str, Any]],
        posted_operations: Iterable[Mapping[str, Any]],
        replay_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        document_fields = (
            "document_id",
            "natural_key",
            "status",
            "source_order_id",
            "context_order_id",
            "file_sha256",
            "operation_date",
            "rub_amount",
            "cny_amount",
            "bank_rate",
            "updated_at",
        )
        expected_documents = {
            str(item.get("document_id") or ""): {
                field: str(item.get(field) or "") for field in document_fields
            }
            for item in documents
        }
        persisted_documents = {
            str(item.get("document_id") or ""): {
                field: str(item.get(field) or "") for field in document_fields
            }
            for item in self.runtime.list_cny_documents()
            if str(item.get("status") or "") != CNY_DOCUMENT_STATUS_EXCLUDED
        }
        expected_operations = {
            str(item.get("operation_id") or ""): _cny_operation_revision_payload(item)
            for item in posted_operations
        }
        persisted_operations = {
            str(item.get("operation_id") or ""): _cny_operation_revision_payload(item)
            for item in self.runtime.list_cny_ledger_operations()
        }
        persisted_state = self.runtime.load_cny_ledger_replay_state() or {}
        state_fields = (
            "status",
            "reason",
            "replayed_at",
            "operation_count",
            "document_count",
            "balance_cny",
            "balance_rub_value",
            "average_rate",
        )
        state_confirmed = all(
            str(persisted_state.get(field) or "")
            == str(replay_state.get(field) or "")
            for field in state_fields
        )
        if (
            expected_documents != persisted_documents
            or expected_operations != persisted_operations
            or not state_confirmed
        ):
            raise ValueError(
                "CNY ledger durable readback did not confirm the replay outcome"
            )
        return {
            "confirmed": True,
            "document_count": len(persisted_documents),
            "operation_count": len(persisted_operations),
            "replayed_at": str(persisted_state.get("replayed_at") or ""),
        }

    def _with_truthful_write_outcome(
        self,
        payload: Mapping[str, Any],
        *,
        replay: Mapping[str, Any],
        queue: Mapping[str, Any],
        success_message: str,
    ) -> dict[str, Any]:
        result = dict(payload)
        business_projection = dict(queue.get("business_projection") or {})
        queue_failed = str(queue.get("status") or "") in {
            "error",
            "replay_error",
        } or str(business_projection.get("status") or "") == "error"
        replay_pending = str(replay.get("status") or "") == "pending"
        readback_confirmed = bool(replay.get("readback_confirmed"))
        result.update(
            {
                "operation_applied": True,
                "readback_confirmed": readback_confirmed,
            }
        )
        if not readback_confirmed:
            raise ValueError("CNY write outcome is not readback-confirmed")
        if not (queue_failed or replay_pending):
            result.setdefault("message", success_message)
            return result

        document_status = str(result.get("status") or "")
        queue_identity = {
            key: str(queue.get(key) or "")
            for key in ("queue_id", "stable_source_id", "source_revision")
            if str(queue.get(key) or "")
        }
        retry_identity = queue_identity or dict(
            replay.get("durable_retry_identity") or {}
        )
        queue_error = " ".join(
            str(value or "")
            for value in (
                queue.get("error"),
                business_projection.get("error"),
            )
        ).lower()
        data_quality_blocked = (
            str(queue.get("diagnostic_code") or "")
            == "cny_replay_shipment_scope_missing"
            or "data-quality blocker" in queue_error
            or "certification no-op is not proven" in queue_error
        )
        result.update(
            {
                "contract_name": CNY_WRITE_PENDING_CONTRACT_NAME,
                "status": "pending",
                "document_status": document_status,
                "retryable": not data_quality_blocked,
                "pending_phase": "derived_replay",
                "message": (
                    "Операция CNY сохранена, ledger и баланс подтверждены. "
                    + (
                        "Связанный пересчёт ожидает безопасного повтора."
                        if not data_quality_blocked
                        else (
                            "Связанный пересчёт заблокирован качеством данных; "
                            "исправьте указанную shipment/SKU-привязку."
                        )
                    )
                ),
                "durable_retry_identity": retry_identity,
                "http_status": 202,
            }
        )
        return result

    def _sync_own_product_capital_payments(
        self,
        operations: Iterable[Mapping[str, Any]],
        *,
        recalculate: bool = True,
    ) -> list[dict[str, Any]]:
        from packages.application.own_product_capital import OwnProductCapitalBlock

        capital = OwnProductCapitalBlock(
            runtime=self.runtime,
            timestamp_factory=self.timestamp_factory,
        )
        documents = {
            str(item.get("document_id") or ""): dict(item)
            for item in self.runtime.list_cny_documents()
        }
        diagnostics: list[dict[str, Any]] = []
        for operation in operations:
            operation_type = str(operation.get("operation_type") or "")
            if str(operation.get("status") or "") != CNY_LEDGER_OPERATION_STATUS_POSTED:
                continue
            if operation_type not in {
                CNY_LEDGER_OPERATION_SUPPLIER_PAYMENT_OUT,
                CNY_LEDGER_OPERATION_TRANSFER_FEE,
            }:
                continue
            document_id = str(operation.get("source_document_id") or "").strip()
            shipment_id = str(operation.get("source_order_id") or "").strip()
            document = documents.get(document_id) or {}
            detail = self.runtime.load_supplier_shipment(shipment_id) if shipment_id else None
            try:
                if detail is None:
                    raise ValueError("payment→shipment link is missing")
                header = dict(detail.get("header") or {})
                product_lines = [
                    {
                        "line_id": line.get("line_id"),
                        "nm_id": line.get("internal_nm_id"),
                        "qty": line.get("qty"),
                        "unit_price": line.get("unit_price"),
                        "amount": line.get("amount"),
                        "match_status": line.get("match_status"),
                    }
                    for line in detail.get("lines") or []
                    if str(line.get("line_type") or "") == "product"
                ]
                provenance = {
                    "source": "cny_ledger_operation",
                    "source_document_id": document_id,
                    "payment_date_provenance": (
                        (document.get("parsed_payload") or {}).get("payment_date_provenance")
                        if isinstance(document.get("parsed_payload"), Mapping)
                        else {}
                    ),
                }
                if operation_type == CNY_LEDGER_OPERATION_SUPPLIER_PAYMENT_OUT:
                    capital.record_supplier_payment(
                        payment_id=document_id,
                        shipment_id=shipment_id,
                        effective_date=str(operation.get("operation_date") or ""),
                        invoice_total_cny=header.get("invoice_amount_total"),
                        paid_cny=abs(_parse_decimal(operation.get("cny_delta")) or Decimal("0")),
                        paid_rub=abs(_parse_decimal(operation.get("rub_value_delta")) or Decimal("0")),
                        product_lines=product_lines,
                        actual_shipment_date=str(header.get("actual_shipment_date") or "") or None,
                        actual_ff_acceptance_date=str(header.get("actual_ff_acceptance_date") or "") or None,
                        expenses_complete=bool(header.get("expenses_complete")),
                        provenance=provenance,
                        recalculate=False,
                    )
                    capital.materialize_supplier_boundaries(
                        shipment_id=shipment_id,
                        actual_shipment_date=str(header.get("actual_shipment_date") or "") or None,
                        actual_ff_acceptance_date=str(header.get("actual_ff_acceptance_date") or "") or None,
                        expenses_complete=bool(header.get("expenses_complete")),
                        recalculate=False,
                    )
                else:
                    capital.record_order_level_cost_payment(
                        document_id=f"{document_id}:transfer_fee:{operation.get('operation_id') or ''}",
                        shipment_id=shipment_id,
                        effective_date=str(operation.get("operation_date") or ""),
                        capital_rub=abs(_parse_decimal(operation.get("rub_value_delta")) or Decimal("0")),
                        product_lines=product_lines,
                        component="bank_fee",
                        actual_shipment_date=str(header.get("actual_shipment_date") or "") or None,
                        actual_ff_acceptance_date=str(header.get("actual_ff_acceptance_date") or "") or None,
                        expenses_complete=bool(header.get("expenses_complete")),
                        provenance=provenance,
                    )
                capital.resolve_blockers(source_identity=document_id)
            except ValueError as exc:
                capital._record_blocker(  # noqa: SLF001 - same bounded application contour
                    code="payment_capital_allocation_blocked",
                    source_identity=document_id,
                    details={"shipment_id": shipment_id, "reason": str(exc)},
                )
                diagnostics.append(
                    {
                        "document_id": document_id,
                        "operation_type": operation_type,
                        "status": "own_product_capital_allocation_blocked",
                        "source_order_id": shipment_id,
                        "reason": str(exc),
                    }
                )
        if operations and recalculate:
            capital.recalculate()
        return diagnostics

    def materialize_own_product_capital_history(
        self,
        *,
        date_to: str,
        recalculate: bool = True,
    ) -> dict[str, Any]:
        """Materialize capital from persisted posted operations without replaying CNY state."""
        bounded_operations = [
            operation
            for operation in self.runtime.list_cny_ledger_operations()
            if not str(operation.get("operation_date") or "")
            or str(operation.get("operation_date") or "") <= str(date_to)
        ]
        diagnostics = self._sync_own_product_capital_payments(
            bounded_operations,
            recalculate=recalculate,
        )
        return {
            "status": "blocked" if diagnostics else "ok",
            "persisted_operation_count": len(bounded_operations),
            "blocker_count": len(diagnostics),
            "blockers": diagnostics,
        }

    def download_document_file(self, document_id: str) -> tuple[bytes, str, str]:
        document = self.runtime.load_cny_document(str(document_id or "").strip())
        if document is None:
            raise ValueError(f"CNY document not found: {document_id}")
        stored_file_path = str(document.get("stored_file_path") or "").strip()
        if not stored_file_path:
            raise ValueError(f"CNY document has no stored file: {document_id}")
        file_path = self._resolve_runtime_file(stored_file_path)
        if not file_path.exists() or not file_path.is_file():
            raise ValueError(f"CNY document file is missing: {document_id}")
        return (
            file_path.read_bytes(),
            str(document.get("original_filename") or "cny-document.pdf"),
            str(document.get("file_content_type") or CNY_LEDGER_CONTENT_TYPE),
        )

    def delete_document(self, document_id: str) -> dict[str, Any]:
        document = self.runtime.load_cny_document(str(document_id or "").strip())
        if document is None:
            raise ValueError(f"CNY document not found: {document_id}")
        if str(document.get("linked_financial_document_id") or "").strip():
            raise ValueError("CNY document is linked to a supplier financial document; delete the source document instead")
        archived = self.runtime.save_cny_document(
            {
                **document,
                "status": CNY_DOCUMENT_STATUS_EXCLUDED,
                "updated_at": self.timestamp_factory(),
            }
        )
        replay = self.replay_ledger(reason="document_archive")
        result = {
            "contract_name": CNY_LEDGER_CONTRACT_NAME,
            "status": "ok",
            "document_id": str(archived.get("document_id") or ""),
            "deleted": False,
            "archived": True,
            "audit_record_retained": True,
            "file_deleted": False,
            "replay": replay.get("replay") or replay,
        }
        queue = self._enqueue_functional_recalculation(archived)
        result["warehouse_targeted_recalculation"] = queue
        return self._with_truthful_write_outcome(
            result,
            replay=replay,
            queue=queue,
            success_message="Документ CNY исключён, ledger пересчитан.",
        )

    def restore_document(
        self,
        document_id: str,
        *,
        target_shipment_id: str,
    ) -> dict[str, Any]:
        document = self.runtime.load_cny_document(str(document_id or "").strip())
        if document is None:
            raise ValueError(f"CNY document not found: {document_id}")
        if str(document.get("status") or "") != CNY_DOCUMENT_STATUS_EXCLUDED:
            raise ValueError("CNY document is not archived")
        old_shipment_id = str(document.get("source_order_id") or "").strip()
        now = self.timestamp_factory()
        restored_status = _document_status_for_parse(
            str(document.get("document_type") or ""),
            dict(document.get("parsed_payload") or {}),
            _string_list(document.get("warnings")),
            _string_list(document.get("errors")),
        )
        restored = self.runtime.save_cny_document(
            {
                **document,
                "source": CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER,
                "source_order_id": str(target_shipment_id or "").strip(),
                "context_order_id": str(target_shipment_id or "").strip(),
                "status": restored_status,
                "updated_at": now,
            }
        )
        replay = self.replay_ledger(reason="document_restore")
        queue = self._enqueue_functional_recalculation(
            restored,
            additional_shipment_ids=[old_shipment_id],
        )
        result = {
            **self._with_download_path(restored),
            "outcome": (
                "restored"
                if old_shipment_id == str(target_shipment_id or "").strip()
                else "relinked"
            ),
            "old_supplier_order_id": old_shipment_id,
            "supplier_order_id": str(target_shipment_id or "").strip(),
            "replay": replay.get("replay") or replay,
            "warehouse_targeted_recalculation": queue,
        }
        return self._with_truthful_write_outcome(
            result,
            replay=replay,
            queue=queue,
            success_message="Документ CNY восстановлен, ledger пересчитан.",
        )

    def relink_document(
        self,
        document_id: str,
        *,
        target_shipment_id: str,
    ) -> dict[str, Any]:
        document = self.runtime.load_cny_document(str(document_id or "").strip())
        if document is None:
            raise ValueError(f"CNY document not found: {document_id}")
        if str(document.get("status") or "") == CNY_DOCUMENT_STATUS_EXCLUDED:
            return self.restore_document(
                document_id,
                target_shipment_id=target_shipment_id,
            )
        if str(document.get("linked_financial_document_id") or "").strip():
            raise ValueError(
                "CNY document is linked to a supplier financial document; relink the source document instead"
            )
        normalized_target = str(target_shipment_id or "").strip()
        old_shipment_id = str(document.get("source_order_id") or "").strip()
        if old_shipment_id == normalized_target:
            reconciled = self.reconcile_document(
                str(document.get("document_id") or ""),
                reason="idempotent_document_relink",
            )
            return {
                **reconciled,
                "outcome": "already_present",
                "old_supplier_order_id": old_shipment_id,
                "supplier_order_id": normalized_target,
                "idempotent": True,
            }
        from packages.application.own_product_capital import OwnProductCapitalBlock

        OwnProductCapitalBlock(
            runtime=self.runtime,
            timestamp_factory=self.timestamp_factory,
        ).remove_supplier_payment(
            str(document.get("document_id") or ""),
            recalculate=False,
        )
        now = self.timestamp_factory()
        moved = self.runtime.update_cny_document_context(
            document_id=str(document.get("document_id") or ""),
            source_order_id=normalized_target,
            context_order_id=normalized_target,
            updated_at=now,
        )
        replay = self.replay_ledger(reason="document_relink")
        queue = self._enqueue_functional_recalculation(
            moved,
            additional_shipment_ids=[old_shipment_id],
        )
        result = {
            **self._with_download_path(moved),
            "outcome": "relinked",
            "old_supplier_order_id": old_shipment_id,
            "supplier_order_id": normalized_target,
            "replay": replay.get("replay") or replay,
            "warehouse_targeted_recalculation": queue,
        }
        return self._with_truthful_write_outcome(
            result,
            replay=replay,
            queue=queue,
            success_message="Документ CNY перенесён, ledger пересчитан.",
        )

    def _enqueue_functional_recalculation(
        self,
        document: Mapping[str, Any],
        *,
        additional_shipment_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Reset affected certifications and coalesce a replay by CNY source revision."""

        from packages.application.own_product_capital import OwnProductCapitalBlock
        from packages.application.warehouse_functional import enqueue_warehouse_targeted_recalculation

        source_order_id = str(document.get("source_order_id") or "").strip()
        shipment_summaries = self.runtime.list_supplier_shipments()
        requested_ids = {
            str(item or "").strip()
            for item in [source_order_id, *additional_shipment_ids]
            if str(item or "").strip()
        }
        # An account-level CNY document is valid accounting evidence, but it is
        # not warehouse scope.  Empty scope must therefore stay an explicit
        # warehouse no-op.  The former ``else True`` branch selected every
        # shipment and could replay closed warehouse history globally.
        affected_ids = [
            str(item.get("shipment_id") or "")
            for item in shipment_summaries
            if str(item.get("shipment_id") or "")
            and str(item.get("shipment_id") or "") in requested_ids
        ]
        missing_requested_ids = sorted(requested_ids - set(affected_ids))
        nm_ids: set[int] = set()
        now = self.timestamp_factory()
        for shipment_id in affected_ids:
            shipment = self.runtime.load_supplier_shipment(shipment_id) or {}
            for line in shipment.get("lines") or []:
                if str(line.get("line_type") or "") != "product":
                    continue
                nm_id = int(line.get("internal_nm_id") or 0)
                if nm_id > 0:
                    nm_ids.add(nm_id)
        revision_payload = {
            key: document.get(key)
            for key in (
                "document_id",
                "document_type",
                "natural_key",
                "source_order_id",
                "context_order_id",
                "operation_date",
                "status",
                "rub_amount",
                "cny_amount",
                "bank_rate",
                "file_sha256",
                "updated_at",
            )
        }
        revision = "sha256:" + hashlib.sha256(
            json.dumps(revision_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        stable_source_id = f"cny_document:{document.get('document_id')}"
        if not requested_ids:
            return {
                "status": "no_op",
                "terminal_no_op": True,
                "presentation_status": "Документ учтён без складского пересчёта",
                "stable_source_id": stable_source_id,
                "source_revision": revision,
                "affected_nm_ids": [],
                "affected_shipment_ids": [],
                "warehouse_mutation_count": 0,
                "functional_queue_count": 0,
                "diagnostic_code": "cny_replay_shipment_scope_unbound",
            }
        if missing_requested_ids:
            return {
                "status": "replay_error",
                "presentation_status": "Ошибка данных пересчёта",
                "stable_source_id": stable_source_id,
                "source_revision": revision,
                "affected_nm_ids": sorted(nm_ids),
                "error": (
                    "CNY replay shipment scope is missing: "
                    + ", ".join(missing_requested_ids)
                ),
                "diagnostic_code": "cny_replay_shipment_scope_missing",
            }
        try:
            if affected_ids:
                capital = OwnProductCapitalBlock(
                    runtime=self.runtime,
                    timestamp_factory=self.timestamp_factory,
                )
                capital.set_expenses_certifications(
                    shipment_ids=affected_ids,
                    expenses_complete=False,
                    recalculate=False,
                    update_shipment_headers=True,
                    preserve_unchanged=True,
                )
            if not nm_ids:
                if affected_ids:
                    from packages.application.warehouse_business_projection import (
                        terminalize_supplier_certification_noop,
                    )

                    business_projection = terminalize_supplier_certification_noop(
                        self.runtime,
                        shipment_ids=affected_ids,
                        finished_at=now,
                    )
                else:
                    business_projection = {
                        "status": "no_op",
                        "request_count": 0,
                    }
                return {
                    "status": "no_op",
                    "terminal_no_op": True,
                    "diagnostic_code": "cny_replay_has_no_mapped_sku_scope",
                    "stable_source_id": stable_source_id,
                    "source_revision": revision,
                    "affected_nm_ids": [],
                    "affected_shipment_ids": affected_ids,
                    "business_projection": business_projection,
                }
            queued = enqueue_warehouse_targeted_recalculation(
                runtime=self.runtime,
                stable_source_id=stable_source_id,
                source_revision=revision,
                effective_date=str(document.get("operation_date") or now)[:10],
                affected_nm_ids=nm_ids,
                requested_at=now,
            )
            return {
                **queued,
                "affected_nm_ids": sorted(nm_ids),
                "affected_shipment_ids": affected_ids,
            }
        except Exception as exc:  # noqa: BLE001 - source mutation remains independently durable.
            return {
                "status": "replay_error",
                "presentation_status": "Ошибка пересчёта",
                "stable_source_id": stable_source_id,
                "source_revision": revision,
                "affected_nm_ids": sorted(nm_ids),
                "error": str(exc).replace("\n", " ")[:500],
            }

    def _sync_supplier_payment_documents_from_financial_documents(self, *, now: str) -> None:
        if not hasattr(self.runtime, "list_supplier_financial_documents_all"):
            return
        for financial_document in self.runtime.list_supplier_financial_documents_all():
            if str(financial_document.get("document_type") or "") != FINANCIAL_DOCUMENT_TYPE_BANK_TRANSFER_APPLICATION:
                continue
            normalized = dict(financial_document.get("normalized_parse") or {})
            if str(normalized.get("currency") or financial_document.get("currency") or "").upper() != "CNY":
                continue
            source_order_id = str(financial_document.get("supplier_order_id") or "").strip()
            document_id = str(financial_document.get("document_id") or "").strip()
            natural_key = f"{CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT}:financial:{document_id}"
            if self.runtime.load_cny_document_by_natural_key(natural_key) is not None:
                continue
            operation_datetime, operation_date = _operation_time_fields(normalized)
            document = {
                "document_id": "cnydoc_" + uuid4().hex,
                "document_type": CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT,
                "source": CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER,
                "source_order_id": source_order_id,
                "context_order_id": source_order_id,
                "linked_financial_document_id": document_id,
                "original_filename": financial_document.get("original_filename") or "",
                "stored_file_path": financial_document.get("stored_file_path") or "",
                "file_content_type": financial_document.get("file_content_type") or CNY_LEDGER_CONTENT_TYPE,
                "file_sha256": financial_document.get("file_sha256") or "",
                "natural_key": natural_key,
                "uploaded_at": financial_document.get("uploaded_at") or now,
                "created_at": financial_document.get("uploaded_at") or now,
                "updated_at": now,
                "operation_date": operation_date,
                "operation_datetime": operation_datetime,
                "status": (
                    CNY_DOCUMENT_STATUS_POSTED
                    if str(financial_document.get("parse_status") or "")
                    == FINANCIAL_DOCUMENT_PARSE_STATUS_CONFIRMED
                    else CNY_DOCUMENT_STATUS_EXCLUDED
                ),
                "document_number": normalized.get("document_number") or financial_document.get("document_number") or "",
                "currency": "CNY",
                "rub_amount": "",
                "cny_amount": _decimal_to_storage(_parse_decimal(normalized.get("transfer_amount") or financial_document.get("total_amount"))),
                "bank_rate": "",
                "parsed_payload": {**normalized, "document_type": CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT},
                "raw_parse": dict(financial_document.get("raw_parse") or {}),
                "parser_version": CNY_LEDGER_PARSER_VERSION,
                "warnings": _string_list(financial_document.get("warnings")),
                "errors": _string_list(financial_document.get("errors")),
            }
            self.runtime.save_cny_document(document)

    def _build_order_updates(
        self,
        accumulator: Mapping[str, Mapping[str, Any]],
        *,
        calculated_at: str,
    ) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        known_order_ids = {
            str(item.get("shipment_id") or "").strip()
            for item in self.runtime.list_supplier_shipments()
            if str(item.get("shipment_id") or "").strip()
        }
        for order_id in sorted(known_order_ids):
            bucket = accumulator.get(order_id)
            if not bucket:
                updates.append(
                    {
                        "shipment_id": order_id,
                        "cny_calculation_status": CNY_CALC_STATUS_NO_SUPPLIER_PAYMENT,
                        "cny_calculation_error": "",
                        "cny_ledger_effective_rate": "",
                        "cny_payment_currency_rub_cost": "",
                        "cny_paid_amount": "",
                        "cny_bank_fee_rub": "",
                        "cny_calculated_at": calculated_at,
                    }
                )
                continue
            paid_cny = _as_decimal(bucket.get("paid_cny"))
            payment_cost = _as_decimal(bucket.get("payment_currency_rub_cost"))
            bank_fee = _as_decimal(bucket.get("bank_fee_rub"))
            status = str(bucket.get("status") or CNY_CALC_STATUS_OK)
            errors = _dedupe_strings(_string_list(bucket.get("errors")))
            effective_rate = _safe_div(payment_cost, paid_cny) if paid_cny > 0 else None
            if paid_cny <= 0 and status == CNY_CALC_STATUS_OK:
                status = CNY_CALC_STATUS_AMBIGUOUS_ALLOCATION
            updates.append(
                {
                    "shipment_id": order_id,
                    "cny_calculation_status": status,
                    "cny_calculation_error": "; ".join(errors),
                    "cny_ledger_effective_rate": _decimal_to_storage(effective_rate),
                    "cny_payment_currency_rub_cost": _decimal_to_storage(payment_cost if payment_cost > 0 else None),
                    "cny_paid_amount": _decimal_to_storage(paid_cny if paid_cny > 0 else None),
                    "cny_bank_fee_rub": _decimal_to_storage(bank_fee if bank_fee > 0 else Decimal("0")),
                    "cny_calculated_at": calculated_at,
                }
            )
        return updates

    def _write_document_file(self, *, document_id: str, filename: str, body: bytes) -> str:
        safe_filename = _safe_filename(filename)
        target_dir = self.runtime.runtime_dir / "cny_documents" / "files" / document_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / safe_filename
        target_path.write_bytes(body)
        return _relative_to_runtime(self.runtime.runtime_dir, target_path)

    def _resolve_runtime_file(self, relative_path: str) -> Path:
        normalized = str(relative_path or "").strip()
        if not normalized:
            raise ValueError("runtime file path is empty")
        root = self.runtime.runtime_dir.resolve()
        target = (root / normalized).resolve()
        if not _path_is_relative_to(target, root):
            raise ValueError("runtime file path escapes runtime dir")
        return target

    def _delete_document_file_if_owned(self, document: Mapping[str, Any]) -> None:
        stored_file_path = str(document.get("stored_file_path") or "").strip()
        document_id = str(document.get("document_id") or "").strip()
        if not stored_file_path or not document_id:
            return
        try:
            file_path = self._resolve_runtime_file(stored_file_path)
        except ValueError:
            return
        owned_dir = (self.runtime.runtime_dir / "cny_documents" / "files" / document_id).resolve()
        if not _path_is_relative_to(file_path, owned_dir):
            return
        if file_path.exists() and file_path.is_file():
            file_path.unlink()
        current = file_path.parent
        runtime_root = self.runtime.runtime_dir.resolve()
        while _path_is_relative_to(current, runtime_root) and current != runtime_root:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def _with_download_path(self, document: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(document)
        document_id = str(payload.get("document_id") or "")
        if document_id and payload.get("stored_file_path"):
            payload["download_path"] = f"{PUBLIC_CNY_DOCUMENT_FILE_PREFIX}/{document_id}/file"
        else:
            payload["download_path"] = ""
        return payload


def parse_cny_document_pdf(
    file_bytes: bytes,
    *,
    filename: str = "cny-document.pdf",
    text_extractor: TextExtractor | None = None,
) -> dict[str, Any]:
    extractor = text_extractor or extract_pdf_text_layer
    text, diagnostics, warnings = extractor(file_bytes, filename)
    parsed = parse_cny_document_text(text, filename=filename, extraction_diagnostics=diagnostics)
    parsed["warnings"] = _dedupe_strings([*warnings, *_string_list(parsed.get("warnings"))])
    return parsed


def parse_cny_document_text(
    text: str,
    *,
    filename: str = "",
    extraction_diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_text = _normalize_text(text)
    diagnostics = dict(extraction_diagnostics or {})
    raw_parse = {
        "filename": filename,
        "text_char_count": len(normalized_text),
        "text_line_count": len(_text_to_lines(normalized_text)),
        "extraction": diagnostics,
    }
    warnings: list[str] = []
    errors: list[str] = []
    if len(normalized_text.strip()) < 30:
        errors.append("CNY document parser found no readable text layer")
        return _parsed_payload({}, raw_parse=raw_parse, warnings=warnings, errors=errors)
    if _looks_like_cny_conversion_purchase(normalized_text, filename=filename):
        normalized, parser_warnings = _parse_cny_conversion_purchase_text(normalized_text)
        warnings.extend(parser_warnings)
        return _parsed_payload(normalized, raw_parse=raw_parse, warnings=warnings, errors=errors)
    financial = parse_financial_document_text(
        normalized_text,
        filename=filename,
        extraction_diagnostics=diagnostics,
    )
    financial_normalized = dict(financial.get("normalized_parse") or {})
    if (
        financial_normalized.get("document_type") == FINANCIAL_DOCUMENT_TYPE_BANK_TRANSFER_APPLICATION
        and str(financial_normalized.get("currency") or "").upper() == "CNY"
    ):
        normalized = {
            **financial_normalized,
            "document_type": CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT,
            "cny_amount": financial_normalized.get("transfer_amount"),
            "operation_datetime": _normalize_datetime(financial_normalized.get("execution_time")),
            "operation_date": financial_normalized.get("document_date") or _optional_iso_date(
                financial_normalized.get("execution_time")
            ),
        }
        warnings.extend(_string_list(financial.get("warnings")))
        return _parsed_payload(normalized, raw_parse=raw_parse, warnings=warnings, errors=errors)
    warnings.append("unsupported CNY ledger document type")
    return _parsed_payload({}, raw_parse=raw_parse, warnings=warnings, errors=errors)


def _looks_like_cny_conversion_purchase(text: str, *, filename: str = "") -> bool:
    haystack = f"{filename}\n{text}".casefold()
    return (
        ("поручение" in haystack or "заявление" in haystack)
        and ("покуп" in haystack or "конверс" in haystack or "foreign currency" in haystack)
        and "cny" in haystack
        and ("rub" in haystack or "руб" in haystack)
    )


def _parse_cny_conversion_purchase_text(text: str) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    document_number = (
        _first_match(text, r"Поручение\s*№\s*([A-Za-zА-Яа-я0-9/-]+)", flags=re.I)
        or _first_match(text, r"Заявление\s*№\s*([A-Za-zА-Яа-я0-9/-]+)", flags=re.I)
        or _first_match(text, r"\b№\s*([A-Za-zА-Яа-я0-9/-]+)")
    )
    document_date = _parse_date(
        _first_match(text[:900], r"(?:от|дата)\s*(\d{1,2}\.\d{1,2}\.\d{4})", flags=re.I)
        or _first_match(text[:900], r"(\d{1,2}\.\d{1,2}\.\d{4})")
    )
    all_accounts = _extract_accounts(text)
    rub_debit_account = _extract_labeled_account(
        text,
        ("счет списания", "счёт списания", "списать", "rub debit", "debit account"),
    ) or _first_account_with_prefix(all_accounts, "408028") or (all_accounts[0] if all_accounts else "")
    cny_credit_account = _extract_labeled_account(
        text,
        ("счет зачисления", "счёт зачисления", "зачислить", "cny credit", "credit account"),
    ) or _first_account_with_prefix(all_accounts, "408021") or (all_accounts[1] if len(all_accounts) > 1 else "")
    rub_amount = _extract_currency_amount(text, "RUB") or _extract_currency_amount(text, "руб")
    cny_amount = _extract_currency_amount(text, "CNY")
    bank_rate = _extract_bank_rate(text)
    if bank_rate is None and rub_amount is not None and cny_amount is not None and cny_amount != 0:
        bank_rate = _quantize_rate(rub_amount / cny_amount)
    operation_datetime = _extract_operation_datetime(text)
    bank_status_text = _extract_bank_status_text(text)
    normalized = {
        "document_type": CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE,
        "document_number": document_number,
        "document_date": document_date,
        "order_date": document_date,
        "operation_datetime": _normalize_datetime(operation_datetime),
        "operation_date": _optional_iso_date(operation_datetime) or document_date,
        "rub_debit_account": rub_debit_account,
        "cny_credit_account": cny_credit_account,
        "rub_amount": _decimal_to_storage(rub_amount),
        "cny_amount": _decimal_to_storage(cny_amount),
        "currency": "CNY",
        "bank_rate": _decimal_to_storage(bank_rate),
        "bank": "ВТБ" if re.search(r"\bВТБ\b|VTB", text, flags=re.I) else "",
        "bank_branch": _extract_bank_branch(text),
        "bank_status_text": bank_status_text,
        "accepted_electronically": bool(re.search(r"принят[оа]?\s+электрон", text, flags=re.I)),
    }
    _append_missing_warnings(
        warnings,
        normalized,
        {
            "document_number": "document number",
            "document_date": "document date",
            "rub_debit_account": "RUB debit account",
            "cny_credit_account": "CNY credit account",
            "rub_amount": "RUB amount",
            "cny_amount": "CNY amount",
            "bank_rate": "bank rate",
        },
    )
    return normalized, warnings


def _build_planned_operations(documents: list[Mapping[str, Any]], *, created_at: str) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for document in documents:
        document_id = str(document.get("document_id") or "")
        document_type = str(document.get("document_type") or "")
        source_order_id = str(document.get("source_order_id") or "").strip()
        parsed = dict(document.get("parsed_payload") or {})
        status = str(document.get("status") or "")
        operation_datetime = str(document.get("operation_datetime") or parsed.get("operation_datetime") or "")
        operation_date = str(document.get("operation_date") or parsed.get("operation_date") or parsed.get("document_date") or "")
        document_created_at = str(document.get("created_at") or document.get("uploaded_at") or created_at)
        common = {
            "operation_id": _stable_cny_operation_id(document_id, "primary"),
            "source_document_id": document_id,
            "source_order_id": source_order_id,
            "operation_date": operation_date,
            "operation_datetime": operation_datetime,
            "created_at": document_created_at,
            "updated_at": created_at,
            "document_status": status,
        }
        if status == CNY_DOCUMENT_STATUS_PARSE_ERROR:
            operations.append(
                {
                    **common,
                    "operation_type": CNY_LEDGER_OPERATION_ADJUSTMENT,
                    "cny_delta": "",
                    "rub_value_delta": "",
                    "status": CNY_LEDGER_OPERATION_STATUS_BLOCKED,
                    "error_reason": CNY_CALC_STATUS_PARSE_ERROR,
                }
            )
            continue
        if document_type == CNY_DOCUMENT_TYPE_OPENING_BALANCE:
            cny_amount = _parse_decimal(parsed.get("cny_amount") or document.get("cny_amount"))
            rub_value = _parse_decimal(parsed.get("rub_value") or document.get("rub_amount"))
            operations.append(
                {
                    **common,
                    "operation_type": CNY_LEDGER_OPERATION_OPENING_BALANCE,
                    "cny_delta": _decimal_to_storage(cny_amount),
                    "rub_value_delta": _decimal_to_storage(rub_value),
                }
            )
        elif document_type == CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE:
            cny_amount = _parse_decimal(parsed.get("cny_amount") or document.get("cny_amount"))
            rub_amount = _parse_decimal(parsed.get("rub_amount") or document.get("rub_amount"))
            operations.append(
                {
                    **common,
                    "operation_type": CNY_LEDGER_OPERATION_CONVERSION_IN,
                    "cny_delta": _decimal_to_storage(cny_amount),
                    "rub_value_delta": _decimal_to_storage(rub_amount),
                }
            )
            fee_rub = _parse_decimal(parsed.get("fee_rub") or parsed.get("commission_rub"))
            if fee_rub and fee_rub > 0:
                operations.append(
                    {
                        **common,
                        "operation_id": _stable_cny_operation_id(document_id, "conversion_fee"),
                        "operation_type": CNY_LEDGER_OPERATION_CONVERSION_FEE,
                        "cny_delta": "0",
                        "rub_value_delta": _decimal_to_storage(fee_rub),
                    }
                )
        elif document_type == CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT:
            amount = _parse_decimal(parsed.get("cny_amount") or parsed.get("transfer_amount") or document.get("cny_amount"))
            operations.append(
                {
                    **common,
                    "operation_type": CNY_LEDGER_OPERATION_SUPPLIER_PAYMENT_OUT,
                    "cny_delta": _decimal_to_storage(-amount if amount is not None else None),
                    "rub_value_delta": "",
                }
            )
        elif document_type == CNY_DOCUMENT_TYPE_BANK_FEE:
            fee_cny = _parse_decimal(parsed.get("fee_cny"))
            fee_rub = _parse_decimal(parsed.get("fee_rub") or document.get("rub_amount"))
            operations.append(
                {
                    **common,
                    "operation_type": CNY_LEDGER_OPERATION_TRANSFER_FEE,
                    "cny_delta": _decimal_to_storage(-fee_cny if fee_cny is not None else Decimal("0")),
                    "rub_value_delta": _decimal_to_storage(-fee_rub if fee_rub is not None else Decimal("0")),
                }
            )
        elif document_type == CNY_DOCUMENT_TYPE_ADJUSTMENT:
            operations.append(
                {
                    **common,
                    "operation_type": CNY_LEDGER_OPERATION_ADJUSTMENT,
                    "cny_delta": _decimal_to_storage(_parse_decimal(parsed.get("cny_delta"))),
                    "rub_value_delta": _decimal_to_storage(_parse_decimal(parsed.get("rub_value_delta"))),
                }
            )
    _mark_date_only_sequence_warnings(operations)
    return operations


def _cny_operation_revision_payload(operation: Mapping[str, Any]) -> dict[str, str]:
    """Return every persisted semantic field except audit timestamps."""

    return {
        key: str(operation.get(key) or "")
        for key in (
            "operation_id",
            "operation_type",
            "source_document_id",
            "source_order_id",
            "operation_date",
            "operation_datetime",
            "sequence_key",
            "cny_delta",
            "rub_value_delta",
            "effective_rate_before",
            "balance_cny_after",
            "balance_rub_value_after",
            "average_rate_after",
            "status",
            "error_reason",
        )
    }


def _stable_cny_operation_id(document_id: str, component: str) -> str:
    identity = f"{str(document_id or '').strip()}|{str(component or '').strip()}"
    return "cnyop_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _mark_date_only_sequence_warnings(operations: list[dict[str, Any]]) -> None:
    by_date: dict[str, int] = {}
    for op in operations:
        date_value = str(op.get("operation_date") or "")
        if date_value:
            by_date[date_value] = by_date.get(date_value, 0) + 1
    for op in operations:
        date_value = str(op.get("operation_date") or "")
        if date_value and by_date.get(date_value, 0) > 1 and not str(op.get("operation_datetime") or ""):
            op["status"] = CNY_LEDGER_OPERATION_STATUS_NEEDS_REVIEW
            op["error_reason"] = "date_only_deterministic_sequence"


def _operation_sort_key(operation: Mapping[str, Any]) -> str:
    operation_datetime = _normalize_datetime(operation.get("operation_datetime"))
    operation_date = _optional_iso_date(operation.get("operation_date")) or "9999-12-31"
    created_at = str(operation.get("created_at") or "")
    source_document_id = str(operation.get("source_document_id") or "")
    operation_type = str(operation.get("operation_type") or "")
    if operation_datetime:
        return "|".join([operation_datetime, operation_date, "0", created_at, source_document_id, operation_type])
    priority = DATE_ONLY_OPERATION_PRIORITY.get(operation_type, 99)
    if operation_type in DATE_ONLY_OPERATION_START_OF_DAY_TYPES:
        primary = f"{operation_date}T00:00:{min(priority, 59):02d}Z"
    else:
        primary = f"{operation_date}T23:59:{min(priority, 59):02d}Z"
    return "|".join([primary, operation_date, "1", f"{priority:02d}", created_at, source_document_id, operation_type])


def _ledger_summary(
    documents: list[Mapping[str, Any]],
    operations: list[Mapping[str, Any]],
    replay_state: Mapping[str, Any],
) -> dict[str, Any]:
    last_operation = operations[-1] if operations else {}
    balance_cny = str(replay_state.get("balance_cny") or last_operation.get("balance_cny_after") or "")
    balance_rub_value = str(replay_state.get("balance_rub_value") or last_operation.get("balance_rub_value_after") or "")
    average_rate = str(replay_state.get("average_rate") or last_operation.get("average_rate_after") or "")
    return {
        "document_count": len(documents),
        "conversion_count": sum(1 for item in documents if item.get("document_type") == CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE),
        "operation_count": len(operations),
        "balance_cny": balance_cny,
        "balance_rub_value": balance_rub_value,
        "average_rate": average_rate,
        "replay_status": replay_state.get("status") or ("ok" if operations else "empty"),
        "replayed_at": replay_state.get("replayed_at") or "",
        "blocked_count": sum(1 for item in operations if item.get("status") == CNY_LEDGER_OPERATION_STATUS_BLOCKED),
        "needs_review_count": sum(1 for item in operations if item.get("status") == CNY_LEDGER_OPERATION_STATUS_NEEDS_REVIEW),
    }


def _ledger_diagnostics(operations: list[Mapping[str, Any]], replay_state: Mapping[str, Any]) -> dict[str, Any]:
    replay_diagnostics = replay_state.get("diagnostics")
    diagnostics_list = replay_diagnostics if isinstance(replay_diagnostics, list) else []
    return {
        "blocked_operations": sum(1 for item in operations if item.get("status") == CNY_LEDGER_OPERATION_STATUS_BLOCKED),
        "needs_review_operations": sum(1 for item in operations if item.get("status") == CNY_LEDGER_OPERATION_STATUS_NEEDS_REVIEW),
        "parse_errors": sum(1 for item in operations if item.get("error_reason") == CNY_CALC_STATUS_PARSE_ERROR),
        "items": diagnostics_list,
    }


def _conversion_row(document: Mapping[str, Any]) -> dict[str, Any]:
    parsed = dict(document.get("parsed_payload") or {})
    return {
        **dict(document),
        "operation_datetime": document.get("operation_datetime") or parsed.get("operation_datetime") or "",
        "operation_date": document.get("operation_date") or parsed.get("operation_date") or parsed.get("document_date") or "",
        "rub_amount": document.get("rub_amount") or parsed.get("rub_amount") or "",
        "cny_amount": document.get("cny_amount") or parsed.get("cny_amount") or "",
        "bank_rate": document.get("bank_rate") or parsed.get("bank_rate") or "",
        "source_label": "из заказа поставщика" if document.get("source") == CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER else "Счёт CNY",
    }


def _document_status_for_parse(
    document_type: str,
    normalized: Mapping[str, Any],
    warnings: list[str],
    errors: list[str],
) -> str:
    if errors or not document_type:
        return CNY_DOCUMENT_STATUS_PARSE_ERROR
    required = {
        CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE: ("rub_amount", "cny_amount", "document_date"),
        CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT: ("cny_amount", "document_date"),
    }.get(document_type, ())
    missing = [field for field in required if not normalized.get(field)]
    if missing:
        warnings.append("Missing CNY document fields: " + ", ".join(missing))
        return CNY_DOCUMENT_STATUS_NEEDS_REVIEW
    return CNY_DOCUMENT_STATUS_POSTED


def _document_natural_key(*, document_type: str, file_sha256: str, normalized: Mapping[str, Any]) -> str:
    if file_sha256:
        return f"{document_type}:sha256:{file_sha256}"
    return "|".join(
        [
            document_type,
            str(normalized.get("document_number") or ""),
            str(normalized.get("document_date") or normalized.get("operation_date") or ""),
        ]
    )


def _operation_time_fields(normalized: Mapping[str, Any]) -> tuple[str, str]:
    operation_datetime = _normalize_datetime(normalized.get("operation_datetime") or normalized.get("execution_time"))
    operation_date = (
        _optional_iso_date(operation_datetime)
        or _optional_iso_date(normalized.get("operation_date"))
        or _optional_iso_date(normalized.get("document_date"))
        or _optional_iso_date(normalized.get("order_date"))
    )
    return operation_datetime, operation_date


def _extract_accounts(text: str) -> list[str]:
    seen: set[str] = set()
    accounts: list[str] = []
    for match in re.finditer(r"\b(?:\d[\s-]?){20}\b", text):
        account = re.sub(r"\D+", "", match.group(0))
        if len(account) == 20 and account not in seen:
            seen.add(account)
            accounts.append(account)
    return accounts


def _extract_labeled_account(text: str, labels: tuple[str, ...]) -> str:
    lines = _text_to_lines(text)
    for index, line in enumerate(lines):
        if not any(label.casefold() in line.casefold() for label in labels):
            continue
        segment = " ".join(lines[index : index + 4])
        accounts = _extract_accounts(segment)
        if accounts:
            return accounts[0]
    return ""


def _first_account_with_prefix(accounts: list[str], prefix: str) -> str:
    return next((account for account in accounts if account.startswith(prefix)), "")


def _extract_currency_amount(text: str, currency: str) -> Decimal | None:
    escaped = re.escape(currency)
    patterns = (
        rf"([0-9][\d\s\u00a0\u202f.,]*[,.]\d{{2}})\s*{escaped}\b",
        rf"\b{escaped}\s*([0-9][\d\s\u00a0\u202f.,]*[,.]\d{{2}})",
    )
    for pattern in patterns:
        matches = re.findall(pattern, text, flags=re.I)
        values = [_parse_decimal(match) for match in matches]
        values = [value for value in values if value is not None]
        if values:
            return max(values)
    return None


def _extract_bank_rate(text: str) -> Decimal | None:
    patterns = (
        r"(?:курс(?:\s+банка)?|bank\s+rate|курс\s+покупки)[^\d]{0,80}([0-9]{1,3}[,.][0-9]{3,6})",
        r"([0-9]{1,3}[,.][0-9]{4,6})\s*(?:RUB/CNY|руб\.?\s*/\s*CNY|за\s+1\s*CNY)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.S)
        value = _parse_decimal(match.group(1) if match else "")
        if value is not None:
            return _quantize_rate(value)
    return None


def _extract_operation_datetime(text: str) -> str:
    status_window = _first_match(
        text,
        r"((?:Принят[оа]?|Исполнен[оа]?|Акцептован[оа]?)[^\n\r]{0,160})",
        flags=re.I,
    )
    if not re.search(r"\d{1,2}\.\d{1,2}\.\d{4}", status_window or ""):
        status_window = _first_match(
            text,
            r"((?:Принят[оа]?|Исполнен[оа]?|Акцептован[оа]?).{0,240})",
            flags=re.I | re.S,
        )
    haystack = status_window or text
    match = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})\s*(?:в\s*)?(\d{1,2}:\d{2})(?::\d{2})?", haystack, flags=re.I)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return ""


def _extract_bank_status_text(text: str) -> str:
    match = re.search(r"(Принят[оа]?\s+электрон[^\n\r]*|Исполнен[^\n\r]*|Акцептован[^\n\r]*)", text, flags=re.I)
    return _clean_value(match.group(1) if match else "")


def _extract_bank_branch(text: str) -> str:
    match = re.search(r"((?:Банк\s+)?ВТБ[^\n\r]{0,120})", text, flags=re.I)
    return _clean_value(match.group(1) if match else ("ВТБ" if re.search(r"\bВТБ\b|VTB", text, flags=re.I) else ""))


def _parsed_payload(
    normalized: Mapping[str, Any],
    *,
    raw_parse: Mapping[str, Any],
    warnings: list[str],
    errors: list[str],
) -> dict[str, Any]:
    return {
        "contract_name": CNY_LEDGER_CONTRACT_NAME,
        "parser_version": CNY_LEDGER_PARSER_VERSION,
        "normalized_parse": dict(normalized),
        "raw_parse": dict(raw_parse),
        "warnings": _dedupe_strings(warnings),
        "errors": _dedupe_strings(errors),
    }


def _append_missing_warnings(warnings: list[str], payload: Mapping[str, Any], labels: Mapping[str, str]) -> None:
    for key, label in labels.items():
        value = payload.get(key)
        if value is None or value == "":
            warnings.append(f"Missing {label}")


def _block_operation(operation: dict[str, Any], reason: str) -> None:
    operation["status"] = CNY_LEDGER_OPERATION_STATUS_BLOCKED
    operation["error_reason"] = reason


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    text = re.sub(r"[^\d,.\-]", "", text)
    if not text or text in {"-", ".", ","}:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(" ", "").replace(",", ".")
    else:
        text = text.replace(" ", "")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _as_decimal(value: Any) -> Decimal:
    parsed = _parse_decimal(value)
    return parsed if parsed is not None else Decimal("0")


def _decimal_to_storage(value: Decimal | None) -> str:
    if value is None:
        return ""
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _quantize_rate(value: Decimal) -> Decimal:
    return value.quantize(RATE_QUANT, rounding=ROUND_HALF_UP)


def _safe_div(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return _quantize_rate(numerator / denominator)


def _optional_iso_date(value: Any) -> str:
    raw = _clean_value(value)
    if not raw:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", raw)
    if match:
        day, month, year = match.groups()
        try:
            return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    return match.group(0) if match else ""


def _parse_date(value: Any) -> str:
    return _optional_iso_date(value)


def _normalize_datetime(value: Any) -> str:
    raw = _clean_value(value)
    if not raw:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z?", raw):
        return raw if raw.endswith("Z") else raw + "Z"
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s*(?:в\s*)?(\d{1,2}):(\d{2})(?::(\d{2}))?", raw, flags=re.I)
    if not match:
        return ""
    day, month, year, hour, minute, second = match.groups()
    try:
        parsed = datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second or "0"),
        )
    except ValueError:
        return ""
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_text(text: str) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def _text_to_lines(text: str) -> list[str]:
    return [_clean_value(line) for line in _normalize_text(text).split("\n") if _clean_value(line)]


def _first_match(text: str, pattern: str, flags: int = 0) -> str:
    match = re.search(pattern, text or "", flags)
    return _clean_value(match.group(1) if match else "")


def _clean_value(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ").replace("\u202f", " ")).strip()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [_clean_value(item) for item in value if _clean_value(item)]
    if isinstance(value, tuple):
        return [_clean_value(item) for item in value if _clean_value(item)]
    text = _clean_value(value)
    return [text] if text else []


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _clean_value(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _safe_filename(filename: str) -> str:
    normalized = Path(str(filename or "cny-document.pdf")).name.strip() or "cny-document.pdf"
    normalized = re.sub(r"[\\/:*?\"<>|]+", "_", normalized)
    return normalized[:180] or "cny-document.pdf"


def _relative_to_runtime(runtime_dir: Path, path: Path) -> str:
    return str(path.resolve().relative_to(runtime_dir.resolve()))


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _default_timestamp_factory() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
