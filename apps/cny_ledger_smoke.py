"""Targeted smoke-check for supplier CNY account ledger, replay, and routes."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error as urllib_error, request as urllib_request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH,
    DEFAULT_CNY_ACCOUNT_OPENING_BALANCE_PATH,
    DEFAULT_CNY_ACCOUNT_PATH,
    DEFAULT_CNY_ACCOUNT_REPLAY_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SUPPLIER_FINANCIAL_DOCUMENTS_SEGMENT,
    DEFAULT_SUPPLIER_ORDER_DOCUMENTS_SEGMENT,
    DEFAULT_SUPPLIER_SHIPMENTS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.cny_ledger import CnyLedgerBlock, parse_cny_document_text  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.sqlite_contention import SQLiteContentionExhausted  # noqa: E402
from packages.application.supplier_financial_documents import (  # noqa: E402
    build_bank_fee_statement_import_preview,
    build_financial_summary,
    parse_financial_document_text,
)
from packages.contracts.cny_ledger import (  # noqa: E402
    CNY_CALC_STATUS_INSUFFICIENT_BALANCE,
    CNY_CALC_STATUS_MISSING_OPENING_BALANCE,
    CNY_CALC_STATUS_OK,
    CNY_DOCUMENT_SOURCE_CNY_ACCOUNT,
    CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER,
    CNY_DOCUMENT_STATUS_POSTED,
    CNY_DOCUMENT_TYPE_BANK_FEE,
    CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE,
    CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT,
    CNY_LEDGER_OPERATION_CONVERSION_IN,
    CNY_LEDGER_OPERATION_STATUS_BLOCKED,
    CNY_LEDGER_OPERATION_STATUS_NEEDS_REVIEW,
    CNY_LEDGER_OPERATION_SUPPLIER_PAYMENT_OUT,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


CONVERSION_TEXT = """Поручение №2 от 12.05.2026
Банк ВТБ (ПАО)
Поручение на покупку иностранной валюты
Счет списания RUB 40802810012480001092
Счет зачисления CNY 40802156616580000008
Сумма списания 9 000 000,00 RUB
Сумма покупки 818 367,81 CNY
Курс банка 10,9975
Принято электронно 12.05.2026 11:20
"""

PAYMENT_TEXT = """Заявление на перевод № 1
от 13 мая 2026
Исполнен 13.05.2026 в 12:10:00
Please debit our account with you): 40802156616580000008
Валюта Currency Code CNY
Сумма перевода Amount of transfer 785087,50
50 Ordering Customer
ООО Тест
56 Банк-посредник
BANK CNY
57 Банк получателя
ABCNCNBJXXX BANK OF CHINA
59 Получатель
12345678901234567890 TEST SUPPLIER LTD
Назначение платежа Details of payment 70
PAYMENT UNDER CONTRACT CN-1 от 01.05.2026
INVOICE INV-1 от 10.05.2026
Расходы и комиссии OUR
"""

BANK_STATEMENT_TEXT = """
Выписка по счету 40802156616580000008 CNY за период с 13.05.2026 по 21.05.2026
Банк ВТБ
Входящий остаток 818 367,81 CNY
Исходящий остаток 17 000,00 CNY
13.05.2026 Документ № 1 Дебет 785 087,50 CNY Платёжное поручение № 1 оплата поставщику 785087.50 CNY контракт CN-1 от 01.05.2026 инвойс INV-1 от 10.05.2026 TEST SUPPLIER LTD
13.05.2026 Документ № VAT-1 Дебет 172.72 CNY НДС за ВК по операции №1 на сумму 785087.50 CNY контракт CN-1 от 01.05.2026 инвойс INV-1 от 10.05.2026
13.05.2026 Документ № VK-1 Дебет 785.09 CNY комиссия за ВК по операции №1 на сумму 785087.50 CNY контракт CN-1 от 01.05.2026 инвойс INV-1 от 10.05.2026
13.05.2026 Документ № SWIFT-1 Дебет 15 623.24 CNY комиссия за перевод SWIFT ВТБ Шанхай по операции №1 на сумму 785087.50 CNY контракт CN-1 от 01.05.2026 инвойс INV-1 от 10.05.2026
14.05.2026 Документ № 2 Дебет 100 000,00 CNY Платёжное поручение № 2 оплата поставщику 100000.00 CNY контракт CN-2 от 02.05.2026 инвойс INV-2 от 11.05.2026 OTHER SUPPLIER
14.05.2026 Документ № VAT-2 Дебет 22.00 CNY НДС за ВК по операции №2 на сумму 100000.00 CNY контракт CN-2 от 02.05.2026
14.05.2026 Документ № VK-2 Дебет 100.00 CNY комиссия за ВК по операции №2 на сумму 100000.00 CNY контракт CN-2 от 02.05.2026
13.05.2026 Документ № CNV-1 Кредит 818 367,81 CNY Покупка иностранной валюты поручение №2
"""

RUB_BANK_STATEMENT_TEXT = """
Выписка по счету 40802810012480001092 RUB за период с 13.05.2026 по 13.05.2026
Банк ВТБ
13.05.2026 Документ № RUBFEE-1 Дебет 12 345,67 RUB комиссия банка по операции №1 на сумму 785087.50 CNY контракт CN-1 от 01.05.2026 инвойс INV-1 от 10.05.2026
"""

WEAK_BANK_STATEMENT_TEXT = """
Выписка по счету 40802156616580000008 CNY за период с 13.05.2026 по 13.05.2026
Банк ВТБ
13.05.2026 Документ № WEAK-1 Дебет 172.72 CNY комиссия банка
"""

HTTP_NOW = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"2026-05-01T08:{self.value:02d}:00Z"


def main() -> None:
    _assert_vtb_parser()
    _assert_bank_fee_statement_parser_and_matching()
    _assert_exact_cost_summary_rules()
    _assert_application_ledger_replay()
    _assert_same_day_date_only_financial_priority()
    _assert_blocked_states()
    _assert_http_delete_replays_and_removes_owned_file()
    _assert_http_routes_and_order_integration()


def _assert_vtb_parser() -> None:
    parsed = parse_cny_document_text(CONVERSION_TEXT, filename="sanitized-vtb-cny-conversion.txt")
    normalized = parsed["normalized_parse"]
    expected = {
        "document_type": CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE,
        "document_number": "2",
        "document_date": "2026-05-12",
        "rub_debit_account": "40802810012480001092",
        "cny_credit_account": "40802156616580000008",
        "rub_amount": "9000000",
        "cny_amount": "818367.81",
        "currency": "CNY",
        "bank_rate": "10.9975",
        "bank": "ВТБ",
        "operation_datetime": "2026-05-12T11:20:00Z",
    }
    for key, value in expected.items():
        if normalized.get(key) != value:
            raise AssertionError(f"VTB parser field {key} changed: {normalized.get(key)!r}")
    if parsed.get("errors"):
        raise AssertionError(f"VTB parser must not emit errors: {parsed['errors']}")


def _assert_bank_fee_statement_parser_and_matching() -> None:
    parsed = parse_financial_document_text(BANK_STATEMENT_TEXT, filename="sanitized-vtb-bank-statement.txt")
    normalized = parsed["normalized_parse"]
    if normalized.get("document_type") != "bank_fee_statement":
        raise AssertionError(f"bank statement parser did not detect document type: {normalized}")
    expected = {
        "period_start": "2026-05-13",
        "period_end": "2026-05-21",
        "account_number": "40802156616580000008",
        "account_currency": "CNY",
    }
    for key, value in expected.items():
        if normalized.get(key) != value:
            raise AssertionError(f"bank statement parser field {key} changed: {normalized.get(key)!r}")
    payments = normalized.get("payment_rows") or []
    if not any(item.get("operation_number") == "1" and _dec(item.get("amount")) == Decimal("785087.50") for item in payments):
        raise AssertionError(f"statement must extract operation #1 supplier payment: {payments}")
    if len(normalized.get("fee_rows") or []) != 5:
        raise AssertionError(f"statement must extract three op #1 fees and two unrelated op #2 fees: {normalized.get('fee_rows')}")
    if len(normalized.get("conversion_rows") or []) != 1:
        raise AssertionError(f"conversion rows must be parsed but not imported as fees: {normalized.get('conversion_rows')}")

    anchor = {
        "document_id": "pay-1",
        "document_number": "1",
        "document_date": "2026-05-13",
        "operation_datetime": "2026-05-13T12:10:00Z",
        "cny_amount": "785087.50",
        "contract_number": "CN-1",
        "contract_date": "2026-05-01",
        "invoice_number": "INV-1",
        "invoice_date": "2026-05-10",
    }
    shipment = {"header": {"shipment_id": "order-1", "contract_no": "CN-1", "contract_date": "2026-05-01", "invoice_no": "INV-1", "invoice_date": "2026-05-10"}}
    preview = build_bank_fee_statement_import_preview(normalized, shipment=shipment, payment_documents=[anchor])
    if preview["status"] != "ready_to_confirm" or preview["match_confidence"] != "strong":
        raise AssertionError(f"anchored fee preview must be ready with strong confidence: {preview}")
    amounts = sorted((_dec(item.get("amount")), item.get("fee_category"), item.get("currency")) for item in preview["matched_fee_rows"])
    expected_amounts = sorted(
        [
            (Decimal("172.72"), "currency_control_vat", "CNY"),
            (Decimal("785.09"), "currency_control_fee", "CNY"),
            (Decimal("15623.24"), "bank_transfer_fee", "CNY"),
        ]
    )
    if amounts != expected_amounts:
        raise AssertionError(f"operation #1 matched fees changed: {amounts}")
    if preview["fee_totals_by_currency"].get("CNY") != "16581.05":
        raise AssertionError(f"CNY fee total changed: {preview['fee_totals_by_currency']}")
    ignored = preview["ignored_rows"]
    if not any(item.get("row_type") == "conversion_in" for item in ignored):
        raise AssertionError(f"conversion inflow row must be ignored by order fee import: {ignored}")
    if not any(item.get("operation_number") == "2" for item in ignored):
        raise AssertionError(f"operation #2 rows must be ignored for operation #1 anchor: {ignored}")

    rub_parsed = parse_financial_document_text(RUB_BANK_STATEMENT_TEXT, filename="sanitized-vtb-rub-statement.txt")
    rub_preview = build_bank_fee_statement_import_preview(
        rub_parsed["normalized_parse"],
        shipment=shipment,
        payment_documents=[anchor],
    )
    rub_rows = rub_preview.get("matched_fee_rows") or []
    if rub_preview["status"] != "ready_to_confirm" or len(rub_rows) != 1:
        raise AssertionError(f"RUB statement fee must be matched through the same anchor: {rub_preview}")
    if rub_rows[0].get("currency") != "RUB" or _dec(rub_rows[0].get("amount")) != Decimal("12345.67"):
        raise AssertionError(f"RUB fee amount/currency changed: {rub_rows}")

    weak_parsed = parse_financial_document_text(WEAK_BANK_STATEMENT_TEXT, filename="sanitized-vtb-weak-statement.txt")
    weak_preview = build_bank_fee_statement_import_preview(
        weak_parsed["normalized_parse"],
        shipment={"header": {"shipment_id": "weak"}},
        payment_documents=[{"document_id": "weak-pay", "document_date": "2026-05-13", "cny_amount": "785087.50"}],
    )
    if weak_preview["status"] != "needs_review" or weak_preview.get("matched_fee_rows"):
        raise AssertionError(f"weak match must not auto-import: {weak_preview}")
    if not weak_preview.get("weak_candidates"):
        raise AssertionError(f"weak candidates should be exposed for manual review: {weak_preview}")


def _assert_exact_cost_summary_rules() -> None:
    confirmed_statement = {
        "document_id": "fee-statement",
        "document_type": "bank_fee_statement",
        "parse_status": "confirmed",
        "normalized_parse": {},
    }
    rub_fee_line = {
        "line_id": "rub-fee",
        "financial_document_id": "fee-statement",
        "category": "bank_transfer_fee",
        "currency": "RUB",
        "amount": 123.45,
        "amount_rub": 123.45,
        "raw": {"source": "bank_fee_statement", "original_currency": "RUB"},
    }
    cny_fee_line = {
        "line_id": "cny-fee",
        "financial_document_id": "fee-statement",
        "category": "bank_transfer_fee",
        "currency": "CNY",
        "amount": 10.0,
        "amount_rub": None,
        "raw": {"source": "bank_fee_statement", "original_currency": "CNY", "rub_equivalent_status": "cny_ledger_pending"},
    }
    exact_summary = build_financial_summary(
        [confirmed_statement],
        [rub_fee_line],
        shipment={"header": {"shipment_id": "exact-ok", "product_qty_total": 10, "invoice_amount_total": 100, "approx_yuan_rate": 99, "cny_payment_currency_rub_cost": 1000, "cny_calculation_status": CNY_CALC_STATUS_OK}},
    )
    per_unit = exact_summary["per_unit"]
    if per_unit["exact_bank_fees_rub"] != 123.45 or per_unit["exact_landed_cost_per_unit_rub"] != 112.35:
        raise AssertionError(f"RUB bank fee must add directly to exact cost: {per_unit}")

    no_payment_summary = build_financial_summary(
        [confirmed_statement],
        [rub_fee_line],
        shipment={"header": {"shipment_id": "no-payment", "product_qty_total": 10, "invoice_amount_total": 100, "approx_yuan_rate": 99}},
    )
    no_payment_per_unit = no_payment_summary["per_unit"]
    if no_payment_per_unit["exact_landed_cost_per_unit_rub"] is not None or "cny_payment_cost_unavailable" not in no_payment_per_unit["exact_cost_blockers"]:
        raise AssertionError(f"exact cost must not fall back to approximate CNY rate: {no_payment_per_unit}")

    cny_pending_summary = build_financial_summary(
        [confirmed_statement],
        [cny_fee_line],
        shipment={"header": {"shipment_id": "cny-pending", "product_qty_total": 10, "cny_payment_currency_rub_cost": 1000, "cny_calculation_status": CNY_CALC_STATUS_OK}},
    )
    cny_pending_per_unit = cny_pending_summary["per_unit"]
    if cny_pending_per_unit["exact_landed_cost_per_unit_rub"] is not None or "cny_ledger_missing" not in cny_pending_per_unit["exact_cost_blockers"]:
        raise AssertionError(f"CNY fee must wait for CNY ledger RUB equivalent: {cny_pending_per_unit}")


def _assert_application_ledger_replay() -> None:
    clock = Clock()
    with TemporaryDirectory(prefix="cny-ledger-application-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_supplier_order(runtime, "order-a", approx_rate=13.5)
        _seed_supplier_order(runtime, "order-b", approx_rate=14.0)
        ledger = CnyLedgerBlock(runtime=runtime, timestamp_factory=clock)

        ledger.create_opening_balance({"operation_date": "2026-05-01", "cny_amount": "100", "rub_value": "1000"})
        _save_conversion(
            runtime,
            document_id="conv-linked-order-a",
            operation_datetime="2026-05-02T10:00:00Z",
            cny_amount="100",
            rub_amount="1200",
            source=CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER,
            source_order_id="order-a",
        )
        _save_payment(runtime, "pay-a-1", "order-a", "2026-05-03T09:00:00Z", "50")
        _save_conversion(
            runtime,
            document_id="conv-direct",
            operation_datetime="2026-05-04T10:00:00Z",
            cny_amount="100",
            rub_amount="1400",
        )
        _save_payment(runtime, "pay-a-2", "order-a", "2026-05-05T09:00:00Z", "50")
        _save_payment(runtime, "pay-b-1", "order-b", "2026-05-06T09:00:00Z", "100")

        first_replay = ledger.replay_ledger(reason="smoke_initial")
        if first_replay["replay"]["status"] != CNY_CALC_STATUS_OK:
            raise AssertionError(f"initial replay must be ok, got {first_replay}")
        order_a = runtime.load_supplier_shipment("order-a")["header"]
        order_b = runtime.load_supplier_shipment("order-b")["header"]
        if order_a.get("approx_yuan_rate") != 13.5:
            raise AssertionError("manual estimated CNY rate must not be overwritten")
        if _dec(order_a["cny_paid_amount"]) != Decimal("100"):
            raise AssertionError(f"multiple payments must aggregate paid CNY, got {order_a}")
        if _dec(order_a["cny_payment_currency_rub_cost"]) != Decimal("1160"):
            raise AssertionError(f"order-a weighted payment cost changed: {order_a}")
        if _dec(order_a["cny_ledger_effective_rate"]) != Decimal("11.6"):
            raise AssertionError(f"order-a effective weighted rate changed: {order_a}")
        if _dec(order_b["cny_payment_currency_rub_cost"]) != Decimal("1220"):
            raise AssertionError(f"order-b payment cost changed: {order_b}")

        status = ledger.get_status()
        linked_conversion = next(
            item for item in status["conversions"] if item["document_id"] == "conv-linked-order-a"
        )
        if linked_conversion.get("source_order_id") != "order-a":
            raise AssertionError("order-linked conversion must be visible in global CNY account")
        if _dec(status["summary"]["balance_cny"]) != Decimal("100"):
            raise AssertionError(f"residual CNY balance changed: {status['summary']}")

        before_backfill_rate = _dec(order_a["cny_ledger_effective_rate"])
        _save_conversion(
            runtime,
            document_id="conv-backdated",
            operation_datetime="2026-05-02T12:00:00Z",
            cny_amount="100",
            rub_amount="800",
        )
        backfilled = ledger.replay_ledger(reason="smoke_backdated_conversion")
        if backfilled["replay"]["status"] != CNY_CALC_STATUS_OK:
            raise AssertionError(f"backdated replay must be ok, got {backfilled}")
        order_a_after = runtime.load_supplier_shipment("order-a")["header"]
        if _dec(order_a_after["cny_ledger_effective_rate"]) >= before_backfill_rate:
            raise AssertionError("backdated conversion must change subsequent effective order rate")
        if _dec(order_a_after["cny_payment_currency_rub_cost"]) != Decimal("1057.14"):
            raise AssertionError(f"backdated conversion cost replay changed: {order_a_after}")
        operations_before_repeat = runtime.list_cny_ledger_operations()
        operation_ids_before_repeat = [
            str(item.get("operation_id") or "") for item in operations_before_repeat
        ]
        revisions_before_repeat = {
            str(item.get("operation_id") or ""): str(item.get("updated_at") or "")
            for item in operations_before_repeat
        }
        ledger.replay_ledger(reason="idempotency_probe")
        operations_after_repeat = runtime.list_cny_ledger_operations()
        operation_ids_after_repeat = [
            str(item.get("operation_id") or "") for item in operations_after_repeat
        ]
        if operation_ids_after_repeat != operation_ids_before_repeat:
            raise AssertionError(
                "CNY replay must retain deterministic operation identities: "
                f"{operation_ids_before_repeat} -> {operation_ids_after_repeat}"
            )
        revisions_after_repeat = {
            str(item.get("operation_id") or ""): str(item.get("updated_at") or "")
            for item in operations_after_repeat
        }
        if revisions_after_repeat != revisions_before_repeat:
            raise AssertionError(
                "CNY no-op replay must preserve semantic operation revisions: "
                f"{revisions_before_repeat} -> {revisions_after_repeat}"
            )
        deleted = ledger.delete_document("conv-backdated")
        if deleted.get("deleted") is not False or deleted.get("archived") is not True:
            raise AssertionError(f"delete UI action must archive with audit retained: {deleted}")
        order_a_deleted = runtime.load_supplier_shipment("order-a")["header"]
        if _dec(order_a_deleted["cny_ledger_effective_rate"]) != before_backfill_rate:
            raise AssertionError(f"delete replay must restore order-a effective rate: {order_a_deleted}")


def _assert_same_day_date_only_financial_priority() -> None:
    with TemporaryDirectory(prefix="cny-ledger-same-day-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_supplier_order(runtime, "same-day-order", approx_rate=99.0)
        ledger = CnyLedgerBlock(runtime=runtime, timestamp_factory=Clock())
        ledger.create_opening_balance({"operation_date": "2026-05-19", "cny_amount": "56317.81", "rub_value": "563178.10"})
        _save_date_only_payment(
            runtime,
            document_id="same-day-pay",
            source_order_id="same-day-order",
            operation_date="2026-05-20",
            cny_amount="345337.5",
            created_at="2026-05-20T08:00:00Z",
        )
        _save_date_only_conversion(
            runtime,
            document_id="same-day-conv",
            operation_date="2026-05-20",
            cny_amount="345337.5",
            rub_amount="3453375.00",
            created_at="2026-05-20T08:01:00Z",
        )

        replay = ledger.replay_ledger(reason="smoke_same_day_date_only_priority")
        if replay["replay"]["status"] != CNY_CALC_STATUS_OK:
            raise AssertionError(f"same-day date-only replay must be counted, got {replay}")
        if _dec(replay["summary"]["balance_cny"]) != Decimal("56317.81"):
            raise AssertionError(f"same-day replay must not overstate residual CNY balance: {replay['summary']}")

        operations = runtime.list_cny_ledger_operations()
        conversion_index = next(
            index
            for index, item in enumerate(operations)
            if item.get("source_document_id") == "same-day-conv"
            and item.get("operation_type") == CNY_LEDGER_OPERATION_CONVERSION_IN
        )
        payment_index = next(
            index
            for index, item in enumerate(operations)
            if item.get("source_document_id") == "same-day-pay"
            and item.get("operation_type") == CNY_LEDGER_OPERATION_SUPPLIER_PAYMENT_OUT
        )
        if conversion_index >= payment_index:
            raise AssertionError(f"date-only conversion must be sequenced before same-day payment: {operations}")
        payment_operation = operations[payment_index]
        if payment_operation.get("status") == CNY_LEDGER_OPERATION_STATUS_BLOCKED:
            raise AssertionError(f"same-day covered payment must not be blocked: {payment_operation}")
        if (
            payment_operation.get("status") != CNY_LEDGER_OPERATION_STATUS_NEEDS_REVIEW
            or payment_operation.get("error_reason") != "date_only_deterministic_sequence"
        ):
            raise AssertionError(f"same-day date-only payment must remain counted with review warning: {payment_operation}")

        header = runtime.load_supplier_shipment("same-day-order")["header"]
        if header.get("cny_calculation_status") != CNY_CALC_STATUS_OK:
            raise AssertionError(f"same-day order calculation must be ok: {header}")
        if _dec(header.get("cny_payment_currency_rub_cost")) != Decimal("3453375"):
            raise AssertionError(f"same-day payment RUB cost changed: {header}")
        if _dec(header.get("cny_ledger_effective_rate")) != Decimal("10"):
            raise AssertionError(f"same-day effective rate must use the counted same-day conversion: {header}")

    with TemporaryDirectory(prefix="cny-ledger-same-day-timed-payment-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_supplier_order(runtime, "same-day-timed-payment", approx_rate=99.0)
        ledger = CnyLedgerBlock(runtime=runtime, timestamp_factory=Clock())
        ledger.create_opening_balance({"operation_date": "2026-05-19", "cny_amount": "56317.81", "rub_value": "563178.10"})
        _save_payment(
            runtime,
            "same-day-timed-pay",
            "same-day-timed-payment",
            "2026-05-20T16:56:45Z",
            "345337.5",
        )
        _save_date_only_conversion(
            runtime,
            document_id="same-day-date-only-conv",
            operation_date="2026-05-20",
            cny_amount="345337.5",
            rub_amount="3453375.00",
            created_at="2026-05-20T18:00:00Z",
        )

        replay = ledger.replay_ledger(reason="smoke_same_day_date_only_conversion_covers_timed_payment")
        if replay["replay"]["status"] != CNY_CALC_STATUS_OK:
            raise AssertionError(f"date-only conversion must cover same-day timed payment, got {replay}")
        if _dec(replay["summary"]["balance_cny"]) != Decimal("56317.81"):
            raise AssertionError(f"timed-payment same-day replay must not overstate CNY balance: {replay['summary']}")
        operations = runtime.list_cny_ledger_operations()
        conversion_index = next(
            index
            for index, item in enumerate(operations)
            if item.get("source_document_id") == "same-day-date-only-conv"
            and item.get("operation_type") == CNY_LEDGER_OPERATION_CONVERSION_IN
        )
        payment_index = next(
            index
            for index, item in enumerate(operations)
            if item.get("source_document_id") == "same-day-timed-pay"
            and item.get("operation_type") == CNY_LEDGER_OPERATION_SUPPLIER_PAYMENT_OUT
        )
        if conversion_index >= payment_index:
            raise AssertionError(f"date-only conversion must sort before same-day timed payment: {operations}")
        payment_operation = operations[payment_index]
        if payment_operation.get("status") == CNY_LEDGER_OPERATION_STATUS_BLOCKED:
            raise AssertionError(f"same-day timed payment must not be blocked: {payment_operation}")
        conversion_operation = operations[conversion_index]
        if (
            conversion_operation.get("status") != CNY_LEDGER_OPERATION_STATUS_NEEDS_REVIEW
            or conversion_operation.get("error_reason") != "date_only_deterministic_sequence"
        ):
            raise AssertionError(f"date-only conversion must expose deterministic-order warning: {conversion_operation}")
        header = runtime.load_supplier_shipment("same-day-timed-payment")["header"]
        if header.get("cny_calculation_status") != CNY_CALC_STATUS_OK:
            raise AssertionError(f"same-day timed payment order calculation must be ok: {header}")


def _assert_blocked_states() -> None:
    with TemporaryDirectory(prefix="cny-ledger-blocked-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_supplier_order(runtime, "missing-opening")
        ledger = CnyLedgerBlock(runtime=runtime, timestamp_factory=Clock())
        _save_payment(runtime, "pay-missing-opening", "missing-opening", "2026-05-01T09:00:00Z", "10")
        replay = ledger.replay_ledger(reason="smoke_missing_opening")
        if replay["replay"]["status"] != "blocked":
            raise AssertionError(f"missing opening replay must block, got {replay}")
        header = runtime.load_supplier_shipment("missing-opening")["header"]
        if header["cny_calculation_status"] != CNY_CALC_STATUS_MISSING_OPENING_BALANCE:
            raise AssertionError(f"missing opening order status changed: {header}")

    with TemporaryDirectory(prefix="cny-ledger-insufficient-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        _seed_supplier_order(runtime, "insufficient")
        ledger = CnyLedgerBlock(runtime=runtime, timestamp_factory=Clock())
        ledger.create_opening_balance({"operation_date": "2026-05-01", "cny_amount": "5", "rub_value": "50"})
        _save_payment(runtime, "pay-insufficient", "insufficient", "2026-05-02T09:00:00Z", "10")
        replay = ledger.replay_ledger(reason="smoke_insufficient")
        if replay["replay"]["status"] != "blocked":
            raise AssertionError(f"insufficient balance replay must block, got {replay}")
        header = runtime.load_supplier_shipment("insufficient")["header"]
        if header["cny_calculation_status"] != CNY_CALC_STATUS_INSUFFICIENT_BALANCE:
            raise AssertionError(f"insufficient balance order status changed: {header}")


def _assert_http_delete_replays_and_removes_owned_file() -> None:
    clock = Clock()
    with TemporaryDirectory(prefix="cny-ledger-http-delete-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=_reserve_free_port(),
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: "2026-05-12T08:00:00Z",
            now_factory=lambda: HTTP_NOW,
        )
        entrypoint.cny_ledger_block.timestamp_factory = clock
        entrypoint.cny_ledger_block.pdf_text_extractor = _fixture_text_extractor
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            opening_status, _ = _post_json(
                f"{base_url}{DEFAULT_CNY_ACCOUNT_OPENING_BALANCE_PATH}",
                {"operation_date": "2026-05-01", "cny_amount": "100", "rub_value": "1000"},
            )
            if opening_status != 200:
                raise AssertionError(f"delete fixture opening balance failed: HTTP {opening_status}")

            uploaded: list[dict[str, object]] = []
            for index in (1, 2):
                upload_status, upload_payload = _post_multipart(
                    f"{base_url}{DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH}",
                    f"direct-conversion-pdf-{index}".encode("utf-8"),
                    filename=f"direct-conversion-{index}.pdf",
                )
                if upload_status != 200 or upload_payload.get("document_type") != CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE:
                    raise AssertionError(f"delete fixture conversion {index} failed: {upload_status} {upload_payload}")
                uploaded.append(upload_payload)

            before_status, before = _get_json(f"{base_url}{DEFAULT_CNY_ACCOUNT_PATH}")
            if before_status != 200 or len(before.get("conversions") or []) != 2:
                raise AssertionError(f"delete preflight must expose two conversions: {before_status} {before}")
            before_summary = dict(before.get("summary") or {})
            deleted_document = uploaded[0]
            deleted_document_id = str(deleted_document.get("document_id") or "")
            stored_file_path = str(deleted_document.get("stored_file_path") or "")
            owned_file = runtime_dir / stored_file_path
            if not deleted_document_id or not stored_file_path or not owned_file.is_file():
                raise AssertionError(f"delete preflight must persist an owned runtime file: {deleted_document}")

            delete_status, deleted = _delete_json(
                f"{base_url}{DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH}/{deleted_document_id}"
            )
            replay = dict(deleted.get("replay") or {})
            if (
                delete_status != 200
                or deleted.get("deleted") is not False
                or deleted.get("archived") is not True
                or deleted.get("document_id") != deleted_document_id
                or replay.get("reason") != "document_archive"
            ):
                raise AssertionError(f"CNY account HTTP delete contract changed: {delete_status} {deleted}")

            after_status, after = _get_json(f"{base_url}{DEFAULT_CNY_ACCOUNT_PATH}")
            if after_status != 200:
                raise AssertionError(f"CNY account reload after delete failed: {after_status} {after}")
            remaining_conversions = list(after.get("conversions") or [])
            remaining_documents = list(after.get("documents") or [])
            remaining_operations = list(after.get("ledger_operations") or [])
            if len([item for item in remaining_conversions if item.get("status") != "excluded"]) != 1 or not any(
                str(item.get("document_id") or "") == deleted_document_id
                and str(item.get("status") or "") == "excluded"
                for item in remaining_documents
            ):
                raise AssertionError(f"archived canonical CNY document audit mismatch: {after}")
            if any(str(item.get("source_document_id") or "") == deleted_document_id for item in remaining_operations):
                raise AssertionError(f"deleted document ledger operations remained after replay: {remaining_operations}")

            remaining = next(item for item in remaining_conversions if item.get("status") != "excluded")
            expected_balance_cny = Decimal("100") + _dec(remaining.get("cny_amount"))
            expected_balance_rub = Decimal("1000") + _dec(remaining.get("rub_amount"))
            expected_average_rate = (expected_balance_rub / expected_balance_cny).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
            after_summary = dict(after.get("summary") or {})
            if (
                _dec(after_summary.get("balance_cny")) != expected_balance_cny
                or _dec(after_summary.get("balance_rub_value")) != expected_balance_rub
                or _dec(after_summary.get("average_rate")) != expected_average_rate
            ):
                raise AssertionError(
                    "delete replay balance/rate changed: "
                    f"expected=({expected_balance_cny}, {expected_balance_rub}, {expected_average_rate}) "
                    f"actual={after_summary}"
                )
            if (
                _dec(before_summary.get("balance_cny")) == _dec(after_summary.get("balance_cny"))
                or _dec(before_summary.get("average_rate")) == _dec(after_summary.get("average_rate"))
            ):
                raise AssertionError(f"delete fixture must prove balance and average-rate recalculation: {before_summary} -> {after_summary}")
            archived_document = runtime.load_cny_document(deleted_document_id)
            if not owned_file.exists() or str((archived_document or {}).get("status") or "") != "excluded":
                raise AssertionError("HTTP delete must retain the canonical audit record and owned source file")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


def _assert_http_routes_and_order_integration() -> None:
    clock = Clock()
    with TemporaryDirectory(prefix="cny-ledger-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        _seed_supplier_order(runtime, "http-order", approx_rate=15.75)
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=_reserve_free_port(),
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: "2026-05-12T08:00:00Z",
            now_factory=lambda: HTTP_NOW,
        )
        entrypoint.cny_ledger_block.timestamp_factory = clock
        entrypoint.cny_ledger_block.pdf_text_extractor = _fixture_text_extractor
        entrypoint.supplier_financial_documents_block.pdf_text_extractor = _fixture_text_extractor
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            operator_status, operator_html = _get_text(f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}?embedded_tab=factory-order")
            if operator_status != 200 or "Счёт CNY" not in operator_html or DEFAULT_CNY_ACCOUNT_PATH not in operator_html:
                raise AssertionError("operator UI must render CNY account subsection and config paths")

            status_code, payload = _get_json(f"{base_url}{DEFAULT_CNY_ACCOUNT_PATH}")
            if status_code != 200 or payload["summary"]["document_count"] != 0:
                raise AssertionError(f"empty CNY account status changed: {status_code} {payload}")

            created_code, created_payload = _post_json(
                f"{base_url}{DEFAULT_CNY_ACCOUNT_OPENING_BALANCE_PATH}",
                {"operation_date": "2026-05-01", "cny_amount": "100", "rub_value": "1000"},
            )
            if created_code != 200 or created_payload.get("document_type") != "opening_balance":
                raise AssertionError(f"opening balance API changed: {created_code} {created_payload}")

            conversion_status, conversion_payload = _post_multipart(
                f"{base_url}{DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH}",
                b"direct-conversion-pdf",
                filename="direct-conversion.pdf",
            )
            if conversion_status != 200 or conversion_payload.get("document_type") != CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE:
                raise AssertionError(f"CNY account conversion upload changed: {conversion_status} {conversion_payload}")

            order_doc_path = (
                f"{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/http-order/{DEFAULT_SUPPLIER_FINANCIAL_DOCUMENTS_SEGMENT}"
            )
            order_conversion_status, order_conversion = _post_multipart(
                f"{base_url}{order_doc_path}",
                b"order-conversion-pdf",
                filename="order-conversion.pdf",
            )
            if (
                order_conversion_status != 200
                or order_conversion.get("duplicate_action") != "semantic_warning"
            ):
                raise AssertionError(f"order-context conversion upload changed: {order_conversion_status} {order_conversion}")
            order_conversion_status, order_conversion = _post_json(
                f"{base_url}{order_doc_path}/confirm-upload",
                {
                    "confirmation_token": order_conversion["confirmation_token"],
                    "allow_semantic_duplicate": True,
                    "duplicate_reason": "Smoke fixture intentionally uses a second source file",
                },
            )
            if (
                order_conversion_status != 200
                or order_conversion.get("supplier_order_id") != "http-order"
                or order_conversion.get("outcome") != "created"
            ):
                raise AssertionError(f"order-context conversion confirmation changed: {order_conversion_status} {order_conversion}")
            duplicate_status, duplicate_conversion = _post_multipart(
                f"{base_url}{order_doc_path}",
                b"order-conversion-pdf",
                filename="order-conversion.pdf",
            )
            if (
                duplicate_status != 200
                or duplicate_conversion.get("duplicate_action") != "already_present"
            ):
                raise AssertionError(f"duplicate order conversion must be idempotent: {duplicate_status} {duplicate_conversion}")
            duplicate_status, duplicate_conversion = _post_json(
                f"{base_url}{order_doc_path}/confirm-upload",
                {"confirmation_token": duplicate_conversion["confirmation_token"]},
            )
            if duplicate_status != 200 or duplicate_conversion.get("outcome") != "already_present":
                raise AssertionError(f"duplicate order conversion confirmation must be idempotent: {duplicate_status} {duplicate_conversion}")

            payment_status, payment_payload = _post_multipart(
                f"{base_url}{order_doc_path}",
                b"payment-pdf",
                filename="payment.pdf",
            )
            if payment_status != 200 or payment_payload.get("duplicate_action") != "create":
                raise AssertionError(f"supplier payment upload changed: {payment_status} {payment_payload}")
            payment_status, payment_payload = _post_json(
                f"{base_url}{order_doc_path}/confirm-upload",
                {"confirmation_token": payment_payload["confirmation_token"]},
            )
            if (
                payment_status != 200
                or payment_payload.get("document_type") != CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT
                or payment_payload.get("outcome") != "created"
            ):
                raise AssertionError(f"supplier payment confirmation changed: {payment_status} {payment_payload}")

            statement_status, statement_stage = _post_multipart(
                f"{base_url}{order_doc_path}",
                b"bank-statement-pdf",
                filename="bank-statement.pdf",
            )
            if statement_status != 200 or not statement_stage.get("confirmation_token"):
                raise AssertionError(f"bank statement staging changed: {statement_status} {statement_stage}")
            statement_status, statement_preview = _post_json(
                f"{base_url}{order_doc_path}/confirm-upload",
                {"confirmation_token": statement_stage["confirmation_token"]},
            )
            import_preview = statement_preview.get("import_preview") or {}
            if (
                statement_status != 200
                or not statement_preview.get("preview_required")
                or import_preview.get("status") != "ready_to_confirm"
                or len(import_preview.get("matched_fee_rows") or []) != 3
            ):
                raise AssertionError(f"bank statement upload must return fee preview: {statement_status} {statement_preview}")
            duplicate_statement_status, duplicate_statement = _post_multipart(
                f"{base_url}{order_doc_path}",
                b"bank-statement-pdf",
                filename="bank-statement.pdf",
            )
            if (
                duplicate_statement_status != 200
                or duplicate_statement.get("duplicate_action") != "idempotent_active"
            ):
                raise AssertionError(f"duplicate statement preview must be idempotent: {duplicate_statement_status} {duplicate_statement}")
            duplicate_statement_status, duplicate_statement = _post_json(
                f"{base_url}{order_doc_path}/confirm-upload",
                {"confirmation_token": duplicate_statement["confirmation_token"]},
            )
            if duplicate_statement_status != 200 or not duplicate_statement.get("idempotent"):
                raise AssertionError(f"duplicate statement confirmation must be idempotent: {duplicate_statement_status} {duplicate_statement}")
            before_confirm_lines = runtime.list_supplier_financial_expense_lines("http-order")
            if before_confirm_lines:
                raise AssertionError(f"preview must not create expense lines before confirmation: {before_confirm_lines}")
            statement_document_id = str(statement_preview.get("document_id") or "")
            selected_logical_fee_ids = [
                str(item.get("logical_fee_id") or "")
                for item in import_preview.get("logical_fee_groups") or []
                if str(item.get("operation_status") or "") == "new"
                and bool(item.get("import_allowed"))
            ]
            if len(selected_logical_fee_ids) != 3:
                raise AssertionError(
                    "bank statement preview must expose three unselected logical fee groups: "
                    f"{import_preview}"
                )
            replay_ledger = entrypoint.cny_ledger_block.replay_ledger

            def fail_derived_replay_once(*, reason: str = "manual") -> dict[str, object]:
                raise SQLiteContentionExhausted(
                    wait_ms=30_000,
                    retries=12,
                    phase="begin",
                )

            entrypoint.cny_ledger_block.replay_ledger = fail_derived_replay_once
            confirm_status, pending_statement = _post_json(
                f"{base_url}{order_doc_path}/{statement_document_id}/confirm-import",
                {
                    "selected_operation_ids": selected_logical_fee_ids,
                    "source_sha256": statement_preview.get("source_sha256")
                    or statement_preview.get("file_sha256"),
                    "target_revision": import_preview.get("target_revision"),
                },
            )
            entrypoint.cny_ledger_block.replay_ledger = replay_ledger
            pending_cny_documents = [
                item
                for item in runtime.list_cny_documents()
                if item.get("document_type") == CNY_DOCUMENT_TYPE_BANK_FEE
                and item.get("source_order_id") == "http-order"
            ]
            if (
                confirm_status != 202
                or pending_statement.get("status") != "pending"
                or pending_statement.get("operation_applied") is not True
                or pending_statement.get("retryable") is not True
                or len(
                    runtime.list_supplier_financial_expense_lines("http-order")
                )
                != 3
                or len(pending_cny_documents) != 3
            ):
                raise AssertionError(
                    "contention after atomic confirm must return resumable pending "
                    "with all business rows committed together: "
                    f"{confirm_status} {pending_statement}"
                )
            confirm_status, confirmed_statement = _post_json(
                f"{base_url}{order_doc_path}/{statement_document_id}/confirm-import",
                {
                    "selected_operation_ids": selected_logical_fee_ids,
                    "source_sha256": statement_preview.get("source_sha256")
                    or statement_preview.get("file_sha256"),
                    "target_revision": import_preview.get("target_revision"),
                },
            )
            if (
                confirm_status != 200
                or confirmed_statement.get("parse_status") != "confirmed"
                or not confirmed_statement.get("already_added")
            ):
                raise AssertionError(
                    "statement confirm resume changed: "
                    f"{confirm_status} {confirmed_statement}"
                )
            imported_lines = runtime.list_supplier_financial_expense_lines("http-order")
            if len(imported_lines) != 3 or {item.get("currency") for item in imported_lines} != {"CNY"}:
                raise AssertionError(f"confirmed import must create exactly three CNY expense lines: {imported_lines}")
            cny_fee_documents = [
                item
                for item in runtime.list_cny_documents()
                if item.get("document_type") == CNY_DOCUMENT_TYPE_BANK_FEE
                and item.get("source_order_id") == "http-order"
            ]
            if len(cny_fee_documents) != 3:
                raise AssertionError(f"CNY fee import must create three CNY ledger documents: {cny_fee_documents}")
            guarded_document = cny_fee_documents[0]
            guarded_document_id = str(guarded_document.get("document_id") or "")
            guarded_file = runtime_dir / str(guarded_document.get("stored_file_path") or "")
            guarded_documents_before = runtime.list_cny_documents()
            guarded_operations_before = runtime.list_cny_ledger_operations()
            guarded_delete_status, guarded_delete = _delete_json(
                f"{base_url}{DEFAULT_CNY_ACCOUNT_DOCUMENTS_PATH}/{guarded_document_id}"
            )
            if (
                guarded_delete_status != 404
                or "delete the source document instead" not in str(guarded_delete.get("error") or "")
            ):
                raise AssertionError(
                    "source-owned supplier financial CNY document must reject direct account delete: "
                    f"{guarded_delete_status} {guarded_delete}"
                )
            if (
                runtime.list_cny_documents() != guarded_documents_before
                or runtime.list_cny_ledger_operations() != guarded_operations_before
                or runtime.load_cny_document(guarded_document_id) is None
                or not guarded_file.is_file()
            ):
                raise AssertionError("source-owned delete guard must leave canonical document, ledger, and source file unchanged")
            duplicate_confirm_status, duplicate_confirm = _post_json(
                f"{base_url}{order_doc_path}/{statement_document_id}/confirm-import",
                {
                    "selected_operation_ids": selected_logical_fee_ids,
                    "source_sha256": statement_preview.get("source_sha256")
                    or statement_preview.get("file_sha256"),
                    "target_revision": import_preview.get("target_revision"),
                },
            )
            if duplicate_confirm_status != 200 or not duplicate_confirm.get("already_added"):
                raise AssertionError(f"duplicate confirm must be idempotent: {duplicate_confirm_status} {duplicate_confirm}")
            if len(runtime.list_supplier_financial_expense_lines("http-order")) != 3:
                raise AssertionError("duplicate confirm must not duplicate expense lines")

            order_docs_path = f"{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/http-order/{DEFAULT_SUPPLIER_ORDER_DOCUMENTS_SEGMENT}"
            docs_status, docs_payload = _get_json(f"{base_url}{order_docs_path}")
            docs = docs_payload.get("documents") or []
            if docs_status != 200 or not any(item.get("document_id") == payment_payload.get("document_id") for item in docs):
                raise AssertionError(f"CNY payment must be visible in supplier order documents: {docs_status} {docs_payload}")
            if not any(item.get("document_id") == statement_document_id for item in docs):
                raise AssertionError(f"confirmed bank statement must be visible in supplier order documents: {docs_status} {docs_payload}")

            replay_status, replay_payload = _post_json(f"{base_url}{DEFAULT_CNY_ACCOUNT_REPLAY_PATH}", {})
            if replay_status != 200 or replay_payload["replay"]["status"] != CNY_CALC_STATUS_OK:
                raise AssertionError(f"explicit replay route changed: {replay_status} {replay_payload}")

            status_code, payload = _get_json(f"{base_url}{DEFAULT_CNY_ACCOUNT_PATH}")
            if status_code != 200 or payload["summary"]["conversion_count"] < 2:
                raise AssertionError(f"CNY account must show direct and order-linked conversions: {status_code} {payload}")
            shipment = runtime.load_supplier_shipment("http-order")["header"]
            if shipment["cny_calculation_status"] != CNY_CALC_STATUS_OK:
                raise AssertionError(f"supplier payment must recalculate order CNY fields: {shipment}")
            if not shipment["cny_ledger_effective_rate"] or not shipment["cny_payment_currency_rub_cost"]:
                raise AssertionError(f"order CNY calculated fields missing: {shipment}")
            if shipment["approx_yuan_rate"] != 15.75:
                raise AssertionError("HTTP flow must not overwrite old estimated CNY rate")
            detail_status, detail = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/http-order")
            if (
                detail_status != 200
                or detail.get("exact_landed_cost_per_unit_rub") is not None
                or detail.get("exact_cost_status") != "unavailable"
                or not any(
                    "товарных строк invoice" in str(reason)
                    for reason in detail.get("exact_cost_blockers") or []
                )
            ):
                raise AssertionError(
                    "a legacy header quantity cannot replace missing canonical invoice SKU lines: "
                    f"{detail_status} {detail}"
                )
            delete_statement_status, delete_statement_preview = _post_json(
                f"{base_url}{order_doc_path}/{statement_document_id}/delete-preview",
                {},
            )
            if delete_statement_status != 200 or not delete_statement_preview.get("confirmation_token"):
                raise AssertionError(
                    f"bank statement delete preview changed: {delete_statement_status} {delete_statement_preview}"
                )
            delete_statement_status, delete_statement = _post_json(
                f"{base_url}{order_doc_path}/{statement_document_id}/delete-confirm",
                {
                    "confirmation_token": delete_statement_preview[
                        "confirmation_token"
                    ]
                },
            )
            if (
                delete_statement_status != 200
                or delete_statement.get("deleted") is not False
                or delete_statement.get("archived") is not True
                or len(delete_statement.get("cny_documents_archived") or []) != 3
                or not delete_statement.get("cny_replay")
            ):
                raise AssertionError(f"bank statement delete must cleanup linked CNY fees: {delete_statement_status} {delete_statement}")
            remaining_fee_documents = [
                item
                for item in runtime.list_cny_documents()
                if item.get("document_type") == CNY_DOCUMENT_TYPE_BANK_FEE
                and item.get("source_order_id") == "http-order"
            ]
            if len(remaining_fee_documents) != 3 or any(item.get("status") != "excluded" for item in remaining_fee_documents):
                raise AssertionError(f"linked CNY bank fee documents must be archived after source delete: {remaining_fee_documents}")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


def _save_conversion(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    document_id: str,
    operation_datetime: str,
    cny_amount: str,
    rub_amount: str,
    source: str = CNY_DOCUMENT_SOURCE_CNY_ACCOUNT,
    source_order_id: str = "",
) -> None:
    operation_date = operation_datetime.split("T", 1)[0]
    runtime.save_cny_document(
        {
            **_base_cny_document(document_id, CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE, operation_date, operation_datetime),
            "source": source,
            "source_order_id": source_order_id,
            "context_order_id": source_order_id,
            "rub_amount": rub_amount,
            "cny_amount": cny_amount,
            "bank_rate": str((Decimal(rub_amount) / Decimal(cny_amount)).normalize()),
            "parsed_payload": {
                "document_type": CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE,
                "document_number": document_id,
                "document_date": operation_date,
                "operation_datetime": operation_datetime,
                "operation_date": operation_date,
                "rub_amount": rub_amount,
                "cny_amount": cny_amount,
                "currency": "CNY",
                "bank_rate": str((Decimal(rub_amount) / Decimal(cny_amount)).normalize()),
            },
        }
    )


def _save_payment(
    runtime: RegistryUploadDbBackedRuntime,
    document_id: str,
    source_order_id: str,
    operation_datetime: str,
    cny_amount: str,
) -> None:
    operation_date = operation_datetime.split("T", 1)[0]
    runtime.save_cny_document(
        {
            **_base_cny_document(document_id, CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT, operation_date, operation_datetime),
            "source": CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER,
            "source_order_id": source_order_id,
            "context_order_id": source_order_id,
            "rub_amount": "",
            "cny_amount": cny_amount,
            "bank_rate": "",
            "parsed_payload": {
                "document_type": CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT,
                "document_number": document_id,
                "document_date": operation_date,
                "operation_datetime": operation_datetime,
                "operation_date": operation_date,
                "currency": "CNY",
                "cny_amount": cny_amount,
                "transfer_amount": cny_amount,
            },
        }
    )


def _save_date_only_conversion(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    document_id: str,
    operation_date: str,
    cny_amount: str,
    rub_amount: str,
    created_at: str,
) -> None:
    runtime.save_cny_document(
        {
            **_base_cny_document(document_id, CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE, operation_date, ""),
            "created_at": created_at,
            "uploaded_at": created_at,
            "updated_at": created_at,
            "rub_amount": rub_amount,
            "cny_amount": cny_amount,
            "bank_rate": str((Decimal(rub_amount) / Decimal(cny_amount)).normalize()),
            "parsed_payload": {
                "document_type": CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE,
                "document_number": document_id,
                "document_date": operation_date,
                "operation_date": operation_date,
                "rub_amount": rub_amount,
                "cny_amount": cny_amount,
                "currency": "CNY",
                "bank_rate": str((Decimal(rub_amount) / Decimal(cny_amount)).normalize()),
            },
        }
    )


def _save_date_only_payment(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    document_id: str,
    source_order_id: str,
    operation_date: str,
    cny_amount: str,
    created_at: str,
) -> None:
    runtime.save_cny_document(
        {
            **_base_cny_document(document_id, CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT, operation_date, ""),
            "source": CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER,
            "source_order_id": source_order_id,
            "context_order_id": source_order_id,
            "created_at": created_at,
            "uploaded_at": created_at,
            "updated_at": created_at,
            "cny_amount": cny_amount,
            "parsed_payload": {
                "document_type": CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT,
                "document_number": document_id,
                "document_date": operation_date,
                "operation_date": operation_date,
                "currency": "CNY",
                "cny_amount": cny_amount,
                "transfer_amount": cny_amount,
            },
        }
    )


def _base_cny_document(
    document_id: str,
    document_type: str,
    operation_date: str,
    operation_datetime: str,
) -> dict[str, object]:
    return {
        "document_id": document_id,
        "document_type": document_type,
        "source": CNY_DOCUMENT_SOURCE_CNY_ACCOUNT,
        "source_order_id": "",
        "context_order_id": "",
        "linked_financial_document_id": "",
        "original_filename": "",
        "stored_file_path": "",
        "file_content_type": "application/pdf",
        "file_sha256": document_id,
        "natural_key": f"smoke:{document_id}",
        "uploaded_at": "2026-05-01T08:00:00Z",
        "created_at": "2026-05-01T08:00:00Z",
        "updated_at": "2026-05-01T08:00:00Z",
        "operation_date": operation_date,
        "operation_datetime": operation_datetime,
        "status": CNY_DOCUMENT_STATUS_POSTED,
        "document_number": document_id,
        "currency": "CNY",
        "rub_amount": "",
        "cny_amount": "",
        "bank_rate": "",
        "parsed_payload": {},
        "raw_parse": {},
        "parser_version": "smoke",
        "warnings": [],
        "errors": [],
    }


def _seed_supplier_order(
    runtime: RegistryUploadDbBackedRuntime,
    shipment_id: str,
    *,
    approx_rate: float = 11.0,
) -> None:
    runtime.save_supplier_shipment(
        header={
            "shipment_id": shipment_id,
            "created_at": "2026-05-01T07:00:00Z",
            "updated_at": "2026-05-01T07:00:00Z",
            "shipment_date": "2026-05-20",
            "invoice_no": shipment_id,
            "invoice_date": "2026-05-01",
            "contract_no": "CN-1",
            "contract_date": "2026-05-01",
            "supplier_name": "TEST SUPPLIER LTD",
            "customer_name": "TEST CUSTOMER",
            "currency": "CNY",
            "approx_yuan_rate": approx_rate,
            "product_qty_total": 1,
            "product_amount_total": 1,
            "extras_amount_total": 0,
            "invoice_amount_total": 1,
            "declared_invoice_total": 1,
            "match_status": "all_matched",
            "source_filename": "smoke.xlsx",
            "source_file_sha256": "",
            "source_file_path": "",
            "parser_version": "smoke",
            "warnings": [],
            "errors": [],
        },
        lines=[],
    )


def _fixture_text_extractor(file_bytes: bytes, filename: str) -> tuple[str, dict[str, object], list[str]]:
    if "statement" in filename.lower() or b"statement" in file_bytes:
        return BANK_STATEMENT_TEXT, {"method": "smoke_text_fixture"}, []
    if "payment" in filename.lower() or b"payment" in file_bytes:
        return PAYMENT_TEXT, {"method": "smoke_text_fixture"}, []
    return CONVERSION_TEXT, {"method": "smoke_text_fixture"}, []


def _post_multipart(
    url: str,
    body_bytes: bytes,
    *,
    filename: str,
    content_type: str = "application/pdf",
) -> tuple[int, dict[str, object]]:
    boundary = "----wbcore-cny" + uuid4().hex
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
            body_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    request = urllib_request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json"},
        method="POST",
    )
    return _open_json(request)


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    return _open_json(request)


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"}, method="GET")
    return _open_json(request)


def _get_text(url: str) -> tuple[int, str]:
    request = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _open_json(request: urllib_request.Request) -> tuple[int, dict[str, object]]:
    try:
        with urllib_request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _delete_json(url: str) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(url, method="DELETE", headers={"Accept": "application/json"})
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _dec(value: object) -> Decimal:
    return Decimal(str(value or "0"))


if __name__ == "__main__":
    main()
