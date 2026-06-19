"""Smoke-check supplier order financial document parser and API routes."""

from __future__ import annotations

from http import HTTPStatus
import json
from pathlib import Path
import socket
import sys
import threading
from tempfile import TemporaryDirectory
from typing import Any
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SUPPLIER_SHIPMENTS_PATH,
    DEFAULT_UPLOAD_PATH,
    RegistryUploadHttpEntrypointConfig,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.supplier_financial_documents import (  # noqa: E402
    StaticUsdRateProvider,
    SupplierFinancialDocumentsBlock,
    build_financial_summary,
    parse_financial_document_text,
)


QUOTE_TEXT = """
Коммерческое предложение на транспортно-экспедиционные услуги по тарифу «Авто стандарт 25-30 дней»
Transitplus International Ltd
Наименование груза: СТЕКЛА ДЛЯ СМАРТФОНА
г. Москва 02.06.2026
№
1
2
3
4
5
6
Общая стоимость
14360
40985
0
320
350
1121
57136 USD
Город отправки: Guangzhou (Гуанчжоу)
Пункт назначения: Москва
Сроки доставки: 25-30 дней
Вес брутто, кг. 9644,6
Вес нетто, кг: 9644,6
Объем, м3 45,32
Оценочная стоимость груза, долл. 112155,36 USD или 785087,50 юаней
1. Предварительный расчет стоимости:
Стоимость доставки
Таможенные платежи и сборы
Экологический сбор
Брокерские услуги
Комиссия компании
Страховая ставка, % 1,0%
ИТОГО:
Оформление разрешительной документации 0 USD
Стоимость оформления экспортных документов 80 - 150 USD
Оплата за доставку производится: по курсу Банка ВТБ (на дату выставления счета)
Предложение действительно в течение 5 календарных дней
"""

QUOTE_PDFTOTEXT_TEXT = """
Коммерческое предложение
на транспортно-экспедиционные услуги
по тарифу «Авто стандарт 25-30 дней»

г. Москва                                                                                                       02.06.2026
Наименование груза:                                           СТЕКЛА ДЛЯ СМАРТФОНА
Условия поставки:                                             EXW
Город отправки:                                               Guangzhou (Гуанчжоу)
Пункт назначения:                                             Москва
Сроки доставки:                                               25-30 дней
Вес брутто, кг.                                               9644,6
Вес нетто, кг:                                                9644,6
Объем, м3                                                     45,32
Оценочная стоимость груза, долл.                              112155,36 USD или 785087,50 юаней

1. Предварительный расчет стоимости:(окончательный будет предоставлен по факту получения и взвешивания груза на складе
Transitplus в Китае или после предоставления окончательного упаковочного листа от поставщика)

    №                           Перечень услуг                       Стоимость за кг / Ставка / Тип           Общая стоимость
    1      Стоимость доставки                                                                                           14360
     2     Таможенные платежи и сборы                                                                                   40985
     3     Экологический сбор                                                                                               0
     4     Брокерские услуги                                                                                              320 USD
     5     Комиссия компании                                                                                              350
     6     Страховая ставка, %                                                     1,0%                                  1121
     7     Стоимость дополнительной упаковки
ИТОГО:                                                                                                                  57136 USD

    №                         Дополнительные услуги                                             Общая стоимость
     1     Оформление разрешительной документации                                                        0 USD
     2     Тип разрешительной документации                                                             0

                                                                            80 - 150 USD (стандартно оплачивается поставщиком
     3     Стоимость оформления экспортных документов
                                                                            китайскому брокеру напрямую. В случае отказа поставщика
                                                                            оплачивать, расход выставляется на Клиента)
 ИТОГО:                                                                                                                     0 USD

2. Оплата за доставку производится: по курсу Банка ВТБ (на дату выставления счета), без оплаты груз Клиенту не выдается.
6. Предложение действительно в течение 5 календарных дней (после этого срока требуется актуализация ставки).
Transitplus International Ltd
"""

BROKEN_QUOTE_TEXT = """
Коммерческое предложение на транспортно-экспедиционные услуги по тарифу «Авто стандарт 25-30 дней»
Transitplus International Ltd
Наименование груза: СТЕКЛА ДЛЯ СМАРТФОНА
г. Москва 02.06.2026
57136 USD
Город отправки: Guangzhou (Гуанчжоу)
Пункт назначения: Москва
Вес брутто, кг. 9644,6
Объем, м3 45,32
Предварительный расчет стоимости:
Стоимость доставки
Таможенные платежи и сборы
Брокерские услуги 320 USD
ИТОГО:
Оплата за доставку производится: по курсу Банка ВТБ (на дату выставления счета)
"""

INVOICE_103_TEXT = """
Счет на оплату № 103 от 05 июня 2026 г.
Поставщик (Исполнитель): ООО "ВОРЛД-ЛОГИСТИК"
Основание: ДОГОВОР ТРАНСПОРТНОЙ ЭКСПЕДИЦИИ № ORE от 04.06.2026
1 Организация экспедирования груза по маршруту г. Суйфэньхэ - г. Пограничный, CMR № 457-ORE-002
Итого: 5 000,00
НДС 0% -
Всего к оплате: 5 000,00
Оплатить не позднее 10.06.2026
"""

INVOICE_113_TEXT = """
Счет на оплату № 113 от 18 июня 2026 г.
Поставщик (Исполнитель): ООО "ВОРЛД-ЛОГИСТИК"
Основание: ДОГОВОР ТРАНСПОРТНОЙ ЭКСПЕДИЦИИ № ORE от 04.06.2026
1 Организация экспедирования груза по маршруту г. Пограничный - г. Москва
Итого: 1 210 975,00
В том числе НДС 5%: 57 665,48
Всего к оплате: 1 210 975,00
Оплатить не позднее 23.06.2026
"""

CUSTOMS_TEXT = """
ИМ 40 ЭД
1 10
28 465
СМ. ГРАФУ 14 ДТ
CN 8313659.53
CNY 785087.50 10.5831 010 00
ИУ 1010-49240.00-643-0000000000
2010-831365.99-643-0000000000
5010-2011905.61-643-0000000000
2892511.60
10 10.06.26 14:15:45
ВЫПУСК ТОВАРОВ РАЗРЕШЕН
10131010/100626/5187132
ДЕКЛАРАЦИЯ НА ТОВАРЫ
04031/0 103 от 05.06.2026
04033/0 ORE от 04.06.2026
457-ORE-002 ОТ 08.06.2026
"""

TEXT_BY_FILENAME = {
    "quote.pdf": QUOTE_TEXT,
    "invoice-103.pdf": INVOICE_103_TEXT,
    "invoice-113.pdf": INVOICE_113_TEXT,
    "customs.pdf": CUSTOMS_TEXT,
}


def main() -> None:
    _assert_parser_smoke()
    _assert_http_api_smoke()
    print("supplier_financial_documents_smoke: OK")


def _assert_parser_smoke() -> None:
    quote_payload = parse_financial_document_text(QUOTE_TEXT, filename="quote.txt")
    _assert_transitplus_quote_payload(quote_payload)

    pdftotext_quote_payload = parse_financial_document_text(QUOTE_PDFTOTEXT_TEXT, filename="quote-pdftotext.txt")
    _assert_transitplus_quote_payload(pdftotext_quote_payload)

    invoice_103 = parse_financial_document_text(INVOICE_103_TEXT, filename="invoice-103.txt")["normalized_parse"]
    if (
        invoice_103.get("document_type") != "logistics_invoice"
        or invoice_103.get("invoice_number") != "103"
        or invoice_103.get("amount_rub") != 5000.0
        or invoice_103.get("category_suggestion") != "border_expedition"
    ):
        raise AssertionError(f"invoice 103 parser fields mismatch: {invoice_103}")

    invoice_113 = parse_financial_document_text(INVOICE_113_TEXT, filename="invoice-113.txt")["normalized_parse"]
    if (
        invoice_113.get("invoice_number") != "113"
        or invoice_113.get("amount_rub") != 1210975.0
        or invoice_113.get("vat_rate") != 5.0
        or invoice_113.get("vat_amount_rub") != 57665.48
    ):
        raise AssertionError(f"invoice 113 parser fields mismatch: {invoice_113}")

    customs = parse_financial_document_text(CUSTOMS_TEXT, filename="customs.txt")["normalized_parse"]
    if (
        customs.get("document_type") != "customs_declaration"
        or customs.get("declaration_number") != "10131010/100626/5187132"
        or customs.get("total_goods_count") != 28
        or customs.get("total_customs_payments_rub") != 2892511.6
    ):
        raise AssertionError(f"customs parser fields mismatch: {customs}")
    _assert_incomplete_quote_summary_smoke()


def _assert_transitplus_quote_payload(quote_payload: dict[str, Any]) -> None:
    quote = quote_payload["normalized_parse"]
    if (
        quote.get("document_type") != "logistics_quote"
        or quote.get("quote_date") != "2026-06-02"
        or quote.get("gross_weight_kg") != 9644.6
        or quote.get("total_amount") != 57136.0
        or quote.get("quote_logistics_component_usd") != 16151.0
        or quote.get("quote_customs_component_usd") != 40985.0
        or quote.get("quote_required_amounts_complete") is not True
    ):
        raise AssertionError(f"quote parser fields mismatch: {quote}")
    quote_lines = {line.get("category"): line for line in quote_payload.get("expense_lines", [])}
    expected_quote_amounts = {
        "delivery_cost": 14360.0,
        "customs_payments_and_fees": 40985.0,
        "brokerage_services": 320.0,
        "company_commission": 350.0,
        "insurance": 1121.0,
    }
    for category, expected in expected_quote_amounts.items():
        actual = quote_lines.get(category, {}).get("amount")
        if actual != expected:
            raise AssertionError(f"quote line {category} mismatch: expected {expected}, got {actual}")
    if any("required amount" in warning for warning in quote_payload.get("warnings", [])):
        raise AssertionError(f"quote parser must not report missing required amounts: {quote_payload.get('warnings')}")


def _assert_incomplete_quote_summary_smoke() -> None:
    quote_payload = parse_financial_document_text(BROKEN_QUOTE_TEXT, filename="broken-quote.txt")
    quote = quote_payload["normalized_parse"]
    if quote.get("quote_required_amounts_complete") is not False:
        raise AssertionError(f"incomplete quote must be marked incomplete: {quote}")
    if "delivery_cost" not in quote.get("quote_missing_required_amounts", []):
        raise AssertionError(f"incomplete quote must expose missing delivery: {quote}")
    if not any("required amount" in warning for warning in quote_payload.get("warnings", [])):
        raise AssertionError(f"incomplete quote must warn about required amounts: {quote_payload}")
    documents, lines = _summary_fixture_documents_and_lines(quote_payload)
    summary = build_financial_summary(documents, lines)
    match = summary.get("quote_invoice_match") or {}
    if match.get("implied_rate") is not None or match.get("estimated_bank_rate_on_quote_date") is not None:
        raise AssertionError(f"incomplete quote must not calculate rate: {match}")
    if match.get("status") != "needs_review":
        raise AssertionError(f"incomplete quote match status mismatch: {match}")
    if summary.get("quote", {}).get("required_amounts_complete") is not False:
        raise AssertionError(f"summary must expose incomplete quote base: {summary}")


def _summary_fixture_documents_and_lines(quote_payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    invoice_103 = parse_financial_document_text(INVOICE_103_TEXT, filename="invoice-103.txt")
    invoice_113 = parse_financial_document_text(INVOICE_113_TEXT, filename="invoice-113.txt")
    documents = [
        _document_from_parsed("quote", quote_payload, cbr_rate=78.0),
        _document_from_parsed("invoice-103", invoice_103, cbr_rate=77.5),
        _document_from_parsed("invoice-113", invoice_113, cbr_rate=78.2),
    ]
    lines: list[dict[str, Any]] = []
    for document, parsed in zip(documents, (quote_payload, invoice_103, invoice_113), strict=True):
        for line in parsed.get("expense_lines", []):
            next_line = dict(line)
            next_line["financial_document_id"] = document["document_id"]
            lines.append(next_line)
    return documents, lines


def _document_from_parsed(document_id: str, parsed: dict[str, Any], *, cbr_rate: float) -> dict[str, Any]:
    normalized = dict(parsed.get("normalized_parse") or {})
    return {
        "document_id": document_id,
        "document_type": normalized.get("document_type"),
        "parse_status": "needs_review" if normalized.get("quote_required_amounts_complete") is False else "parsed",
        "document_date": normalized.get("document_date") or normalized.get("invoice_date") or normalized.get("quote_date"),
        "total_amount": normalized.get("total_amount"),
        "total_amount_rub": normalized.get("total_amount_rub"),
        "cbr_usd_rate_value": cbr_rate,
        "cbr_usd_rate_requested_date": normalized.get("document_date") or normalized.get("invoice_date") or normalized.get("quote_date"),
        "cbr_usd_rate_effective_date": normalized.get("document_date") or normalized.get("invoice_date") or normalized.get("quote_date"),
        "normalized_parse": normalized,
    }


def _assert_http_api_smoke() -> None:
    with TemporaryDirectory(prefix="supplier-financial-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        _seed_supplier_order(runtime)
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
            activated_at_factory=lambda: "2026-06-19T08:00:00Z",
        )
        entrypoint.supplier_financial_documents_block = SupplierFinancialDocumentsBlock(
            runtime=runtime,
            timestamp_factory=lambda: "2026-06-19T08:00:00Z",
            usd_rate_provider=StaticUsdRateProvider(
                {
                    "2026-06-02": "78.00",
                    "2026-06-05": "77.50",
                    "2026-06-18": "78.20",
                }
            ),
            pdf_text_extractor=_fixture_text_extractor,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"
            collection_url = f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/sup_financial/financial-documents"
            for filename in ("quote.pdf", "invoice-103.pdf", "invoice-113.pdf", "customs.pdf"):
                status, payload = _post_multipart(collection_url, b"%PDF-1.4\n% synthetic financial smoke\n", filename=filename)
                if status != 200 or not payload.get("document_id") or payload.get("parse_status") != "parsed":
                    raise AssertionError(f"financial upload failed for {filename}: {status} {payload}")
            list_status, listed = _get_json(collection_url)
            if list_status != 200 or len(listed.get("documents", [])) != 4 or len(listed.get("expense_lines", [])) != 14:
                raise AssertionError(f"financial list/detail count mismatch: {list_status} {listed}")
            summary = listed.get("summary") or {}
            if (
                summary.get("quote", {}).get("logistics_usd") != 16151.0
                or summary.get("invoices", {}).get("fact_rub") != 1215975.0
                or summary.get("customs_declaration", {}).get("total_customs_payments_rub") != 2892511.6
                or summary.get("quote_invoice_match", {}).get("status") != "needs_review"
            ):
                raise AssertionError(f"financial summary mismatch: {summary}")
            match = summary.get("quote_invoice_match", {})
            efficiency = summary.get("logistics_efficiency", {})
            if not _approx(match.get("implied_rate"), 75.29, tolerance=0.01):
                raise AssertionError(f"implied rate must use full quote logistics component, got {match}")
            if not _approx(efficiency.get("rub_per_kg"), 126.08, tolerance=0.01):
                raise AssertionError(f"rub/kg mismatch: {efficiency}")
            if not _approx(efficiency.get("rub_per_m3"), 26830.87, tolerance=0.01):
                raise AssertionError(f"rub/m3 mismatch: {efficiency}")
            if _approx(match.get("implied_rate"), 3799.92, tolerance=0.01) or _approx(match.get("relative_spread_pct"), 51.23, tolerance=0.01):
                raise AssertionError(f"summary must not expose bogus rate/spread: {match}")
            quote_document_id = _document_id_by_type(listed, "logistics_quote")
            if not quote_document_id:
                raise AssertionError(f"uploaded quote document missing: {listed}")
            detail_status, detail = _get_json(f"{collection_url}/{quote_document_id}")
            if detail_status != 200 or detail.get("document_id") != quote_document_id or not detail.get("expense_lines"):
                raise AssertionError(f"financial detail mismatch: {detail_status} {detail}")
            file_status, file_bytes, headers = _get_bytes(f"{collection_url}/{quote_document_id}/file")
            if file_status != 200 or b"synthetic financial smoke" not in file_bytes:
                raise AssertionError(f"financial file download mismatch: {file_status} {headers}")
            delete_status, delete_payload = _delete_json(f"{collection_url}/{quote_document_id}")
            if delete_status != 200 or delete_payload.get("deleted") is not True or delete_payload.get("file_deleted") is not True:
                raise AssertionError(f"financial delete failed: {delete_status} {delete_payload}")
            deleted_detail_status, deleted_detail = _get_json(f"{collection_url}/{quote_document_id}")
            if deleted_detail_status != 404:
                raise AssertionError(f"deleted financial detail must return 404: {deleted_detail_status} {deleted_detail}")
            deleted_list_status, after_delete = _get_json(collection_url)
            if (
                deleted_list_status != 200
                or len(after_delete.get("documents", [])) != 3
                or len(after_delete.get("expense_lines", [])) != 5
                or after_delete.get("summary", {}).get("quote", {}).get("logistics_usd") is not None
                or after_delete.get("summary", {}).get("quote_invoice_match", {}).get("implied_rate") is not None
            ):
                raise AssertionError(f"financial list after delete mismatch: {deleted_list_status} {after_delete}")
            status, payload = _post_multipart(collection_url, b"%PDF-1.4\n% synthetic financial smoke\n", filename="quote.pdf")
            if status != 200 or payload.get("parse_status") != "parsed":
                raise AssertionError(f"financial re-upload after delete failed: {status} {payload}")
            final_status, final_list = _get_json(collection_url)
            final_summary = final_list.get("summary") or {}
            if (
                final_status != 200
                or len(final_list.get("documents", [])) != 4
                or len(final_list.get("expense_lines", [])) != 14
                or final_summary.get("quote", {}).get("logistics_usd") != 16151.0
                or not _approx(final_summary.get("quote_invoice_match", {}).get("implied_rate"), 75.29, tolerance=0.01)
            ):
                raise AssertionError(f"financial re-upload summary mismatch: {final_status} {final_list}")
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _seed_supplier_order(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_supplier_shipment(
        header={
            "shipment_id": "sup_financial",
            "created_at": "2026-06-19T08:00:00Z",
            "updated_at": "2026-06-19T08:00:00Z",
            "shipment_date": "2026-06-02",
            "order_status": "in_transit",
            "invoice_no": "SAFE-ORDER",
            "invoice_date": "2026-06-02",
            "contract_no": "ORE",
            "contract_date": "2026-06-04",
            "supplier_name": "HanShang Technology",
            "customer_name": "",
            "currency": "CNY",
            "product_qty_total": 0,
            "product_amount_total": 0,
            "extras_amount_total": 0,
            "invoice_amount_total": 0,
            "declared_invoice_total": 0,
            "match_status": "all_matched",
            "source_filename": "safe.xlsx",
            "source_file_sha256": "",
            "source_file_path": "",
            "invoice_document_id": "",
            "parser_version": "fixture",
            "warnings": [],
            "errors": [],
        },
        lines=[],
    )


def _fixture_text_extractor(file_bytes: bytes, filename: str) -> tuple[str, dict[str, Any], list[str]]:
    del file_bytes
    text = TEXT_BY_FILENAME.get(filename, "")
    return text, {"method": "fixture_text", "filename": filename}, []


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _approx(actual: Any, expected: float, *, tolerance: float) -> bool:
    try:
        return abs(float(actual) - expected) <= tolerance
    except (TypeError, ValueError):
        return False


def _document_id_by_type(payload: Mapping[str, Any], document_type: str) -> str:
    for document in payload.get("documents", []):
        if document.get("document_type") == document_type:
            return str(document.get("document_id") or "")
    return ""


def _get_json(url: str) -> tuple[int, dict[str, Any]]:
    request = urllib_request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib_request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_request.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_bytes(url: str) -> tuple[int, bytes, dict[str, str]]:
    request = urllib_request.Request(url)
    try:
        with urllib_request.urlopen(request, timeout=20) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib_request.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


def _delete_json(url: str) -> tuple[int, dict[str, Any]]:
    request = urllib_request.Request(url, method="DELETE", headers={"Accept": "application/json"})
    try:
        with urllib_request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_request.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_multipart(url: str, body: bytes, *, filename: str) -> tuple[int, dict[str, Any]]:
    boundary = "----wb-core-financial-smoke"
    payload = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8") + body + f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = urllib_request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_request.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


if __name__ == "__main__":
    main()
