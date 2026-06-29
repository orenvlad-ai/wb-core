"""Targeted smoke-check for supplier CNY account ledger, replay, and routes."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
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
from packages.contracts.cny_ledger import (  # noqa: E402
    CNY_CALC_STATUS_INSUFFICIENT_BALANCE,
    CNY_CALC_STATUS_MISSING_OPENING_BALANCE,
    CNY_CALC_STATUS_OK,
    CNY_DOCUMENT_SOURCE_CNY_ACCOUNT,
    CNY_DOCUMENT_SOURCE_SUPPLIER_ORDER,
    CNY_DOCUMENT_STATUS_POSTED,
    CNY_DOCUMENT_TYPE_CONVERSION_PURCHASE,
    CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT,
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

PAYMENT_TEXT = """Заявление на перевод № PAY-1
от 13 мая 2026
Исполнен 13.05.2026 в 12:10:00
Please debit our account with you): 40802156616580000008
Валюта Currency Code CNY
Сумма перевода Amount of transfer 50,00
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
Расходы и комиссии OUR
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
    _assert_application_ledger_replay()
    _assert_blocked_states()
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
        deleted = ledger.delete_document("conv-backdated")
        if not deleted.get("deleted"):
            raise AssertionError(f"delete must report deleted=true: {deleted}")
        order_a_deleted = runtime.load_supplier_shipment("order-a")["header"]
        if _dec(order_a_deleted["cny_ledger_effective_rate"]) != before_backfill_rate:
            raise AssertionError(f"delete replay must restore order-a effective rate: {order_a_deleted}")


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
            if order_conversion_status != 200 or order_conversion.get("source_order_id") != "http-order":
                raise AssertionError(f"order-context conversion upload changed: {order_conversion_status} {order_conversion}")
            duplicate_status, duplicate_conversion = _post_multipart(
                f"{base_url}{order_doc_path}",
                b"order-conversion-pdf",
                filename="order-conversion.pdf",
            )
            if duplicate_status != 200 or not duplicate_conversion.get("idempotent"):
                raise AssertionError(f"duplicate order conversion must be idempotent: {duplicate_status} {duplicate_conversion}")

            payment_status, payment_payload = _post_multipart(
                f"{base_url}{order_doc_path}",
                b"payment-pdf",
                filename="payment.pdf",
            )
            if payment_status != 200 or payment_payload.get("document_type") != CNY_DOCUMENT_TYPE_SUPPLIER_PAYMENT:
                raise AssertionError(f"supplier payment upload changed: {payment_status} {payment_payload}")

            order_docs_path = f"{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/http-order/{DEFAULT_SUPPLIER_ORDER_DOCUMENTS_SEGMENT}"
            docs_status, docs_payload = _get_json(f"{base_url}{order_docs_path}")
            docs = docs_payload.get("documents") or []
            if docs_status != 200 or not any(item.get("document_id") == payment_payload.get("document_id") for item in docs):
                raise AssertionError(f"CNY payment must be visible in supplier order documents: {docs_status} {docs_payload}")

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


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _dec(value: object) -> Decimal:
    return Decimal(str(value or "0"))


if __name__ == "__main__":
    main()
