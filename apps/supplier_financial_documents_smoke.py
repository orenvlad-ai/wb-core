"""Smoke-check supplier order financial document parser and API routes."""

from __future__ import annotations

import hashlib
from http import HTTPStatus
from io import BytesIO
import json
from pathlib import Path
import socket
import sys
import threading
from tempfile import TemporaryDirectory
from typing import Any
from urllib import request as urllib_request
import zipfile

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_COMPARE_QUOTE_PATH,
    DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_PATH,
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
    apply_supplier_order_document_match,
    build_financial_summary,
    build_supplier_shipment_registry,
    parse_financial_document_upload,
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

QUOTE_2026_06_19_TEXT = """
Коммерческое предложение на транспортно-экспедиционные услуги по тарифу «Авто стандарт 25-30 дней»
Transitplus International Ltd
Наименование груза: СТЕКЛА ДЛЯ СМАРТФОНА
г. Москва 19.06.2026
Город отправки: Guangzhou (Гуанчжоу)
Пункт назначения: Москва
Сроки доставки: 25-30 дней
Вес брутто, кг. 6713,45
Вес нетто, кг: 6713,45
Объем, м3 31,28
Оценочная стоимость груза, долл. 77423,22 USD или 541962,50 юаней
1. Предварительный расчет стоимости:
№ Перечень услуг Общая стоимость
1 Стоимость доставки 12420
2 Таможенные платежи и сборы 27175
3 Экологический сбор 0
4 Брокерские услуги 350
5 Комиссия компании 0
6 Страховая ставка, % 775
ИТОГО: 40720 USD
Оформление разрешительной документации 0 USD
Оплата за доставку производится: по курсу Банка ВТБ (на дату выставления счета)
Предложение действительно в течение 5 календарных дней
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

INVOICE_121_TEXT = """
Счет на оплату № 121 от 29 июня 2026 г.
Поставщик (Исполнитель): ООО "ВОРЛД-ЛОГИСТИК"
Основание: ДОГОВОР ТРАНСПОРТНОЙ ЭКСПЕДИЦИИ № ORE от 04.06.2026
1 Организация экспедирования груза по маршруту г. Суйфэньхэ - г. Пограничный, CMR № 464-ORE-003
Итого: 5 000,00
НДС 0% -
Всего к оплате: 5 000,00
Оплатить не позднее 02.07.2026
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
1 7020008000 С N
CN   422.400 ОООО-ОО
4000 000 380.160
2 7020008000 С N
CN   78.600 ОООО-ОО
4000 000 70.740
3 7020008000 С N
CN   210.000 ОООО-ОО
4000 000 189.000
4 7020008000 С N
CN   198.000 ОООО-ОО
4000 000 178.200
5 7020008000 С N
CN   217.250 ОООО-ОО
4000 000 195.530
6 7020008000 С N
CN   192.150 ОООО-ОО
4000 000 172.940
7 7020008000 С N
CN   374.300 ОООО-ОО
4000 000 336.870
8 7020008000 С N
CN   110.250 ОООО-ОО
4000 000 99.230
9 7020008000 С N
CN   518.700 ОООО-ОО
4000 000 466.830
10 7020008000 С N
CN   963.800 ОООО-ОО
4000 000 867.420
11 7020008000 С N
CN   498.750 ОООО-ОО
4000 000 448.880
12 7020008000 С N
CN   40.600 ОООО-ОО
4000 000 36.540
13 7020008000 С N
CN   168.800 ОООО-ОО
4000 000 151.920
14 7020008000 С N
CN   366.300 ОООО-ОО
4000 000 329.670
15 7020008000 С N
CN   294.000 ОООО-ОО
4000 000 264.600
16 7020008000 С N
CN   570.700 ОООО-ОО
4000 000 513.630
17 7020008000 С N
CN   688.500 ОООО-ОО
4000 000 619.650
18 7020008000 С N
CN   1214.950 ОООО-ОО
4000 000 1093.460
19 7020008000 С N
CN   575.900 ОООО-ОО
4000 000 518.310
20 7020008000 С N
CN   401.100 ОООО-ОО
4000 000 360.990
21 7020008000 С N
CN   77.800 ОООО-ОО
4000 000 70.020
22 7020008000 С N
CN   126.000 ОООО-ОО
4000 000 113.400
23 7020008000 С N
CN   372.400 ОООО-ОО
4000 000 335.160
24 7020008000 С N
CN   106.750 ОООО-ОО
4000 000 96.080
25 7020008000 С N
CN   192.150 ОООО-ОО
4000 000 172.940
26 7020008000 С N
CN   392.000 ОООО-ОО
4000 000 352.800
27 7020008000 С N
CN   305.200 ОООО-ОО
4000 000 274.680
28 7020008000 С N
CN   107.250 ОООО-ОО
4000 000 96.530
"""

BANK_CONTROL_TEXT = """
Документ сформирован системой дистанционного банковского обслуживания Банка ВТБ (ПАО)
ВЕДОМОСТЬ БАНКОВСКОГО КОНТРОЛЯ ПО КОНТРАКТУ
Уникальный номер контракта 2 6 0 5 1 3 8 4 / 1 0 0 0 / 0 0 8 1 / 2 / 2 от 12.05.2026
Раздел I. Учетная информация
1.Сведения о резиденте
Индивидуальный предприниматель ТЕСТОВ ВЛАДИСЛАВ РАДИКОВИЧ
1.2 Адрес: Субъект Российской Федерации
2.Реквизиты нерезидента (нерезидентов)
Наименование
Страна
Признак аффилированного лица
Наименование Код
1 2 3 4
Guangzhou Zifriend Communicate Technology
Co., Ltd КИТАЙ 156
3.Общие сведения о контракте
№ Дата
Валюта контракта
Сумма контракта Дата завершения исполнения обязательств по
контрактунаименование код
1 2 3 4 5 6
082/26 04.04.2026 ЮАНЬ 156 785087.50 31.12.2026
Раздел II. Сведения о платежах
1 13.05.202
6 2 11100 156 785087.50 156 785087.50 30.06.20
26 156 4
Раздел V. Итоговые данные расчетов по контракту
04.06.2026 156 0.00 785087.50 0.00 0.00 0.00 0.00 -785087.50
"""

BANK_CONTROL_MULTI_PAYMENT_TEXT = """
Документ сформирован системой дистанционного банковского обслуживания Банка ВТБ (ПАО)
ВЕДОМОСТЬ БАНКОВСКОГО КОНТРОЛЯ ПО КОНТРАКТУ
Уникальный номер контракта 2 6 0 6 2 7 4 3 / 1 0 0 0 / 0 0 8 1 / 2 / 2 от 11.06.2026
Раздел I. Учетная информация
1.Сведения о резиденте
Индивидуальный предприниматель ТЕСТОВ ВЛАДИСЛАВ РАДИКОВИЧ
1.2 Адрес: Субъект Российской Федерации
2.Реквизиты нерезидента (нерезидентов)
Наименование
Страна
Признак аффилированного лица
Наименование Код
1 2 3 4
Guangzhou Zifriend Communicate Technology
Co., Ltd КИТАЙ 156
3.Общие сведения о контракте
№ Дата
Валюта контракта
Сумма контракта Дата завершения исполнения обязательств по
контрактунаименование код
1 2 3 4 5 6
FR-001/26 08.06.2026 ЮАНЬ 156 БС 07.06.2027
Раздел II. Сведения о платежах
1 11.06.2026 2 11100 156 345 337,50 156 345337.50 07.06.2027 156 4
2 30.06.2026 2 11100 156 59921.25 156 59921.25 31.07.2026 156 4
Раздел V. Итоговые данные расчетов по контракту
30.06.2026 156 0.00 405258.75 0.00 0.00 0.00 0.00 -405258.75
"""

BANK_TRANSFER_TEXT = """
Филиал "Центральный" Банка ВТБ (ПАО)
044525411
Исполнен
22.05.2026 в 00:43:03
ЗАЯВЛЕНИЕ
на перевод № 2
от  г.21 мая 2026
Сумму перевода просим списать с нашего счёта у Вас (Please debit our account with
you):
4 0 8 0 2 1 5 6 6 1 6 5 8 0 0 0 0 0 0 8
Валюта
Currency Code
CNY
Сумма перевода
(цифрами и прописью)
Amount of transfer
(in figures and in writing)
32 541.962,50
Пятьсот сорок одна тысяча девятьсот шестьдесят два юаня 50/100
Отправитель*
Ordering Customer (Name, address, city, country)
50
IE TESTOV VLADISLAV RADIKOVICH
CODE COUNTRY: RU
INN: 560912740163
Банк-посредник**
Intermediary Institution (SWIFT BIC, national clearing
code, name, city, country)
56
Банк получателя*
Account with Institution, Beneficiary’s bank (SWIFT
BIC, national clearing code, name, city, country)
57
//CN767290000018
VTB BANK (PJSC) SHANGHAI BRANCH VTBRCNSHXXX
SHANGHAI TOWER, RM. 2503-2505 FLOOR 25, 501 MIDDLE YINCHENG ROAD,
PUDONG SHANGHAI
CN
Получатель*
Номер счета (IBAN)
Account number (IBAN)
40807156200610034920
Наименование, адрес, город, страна
Beneficiary Customer (Name, address, city, country)
59 GUANGZHOU ZIFRIEND COMMUNICATE TECHNOLOGY CO., LTD
GUANGZHOU
CN
Назначение платежа*
Details of payment 70 CONTRACT 083/26 DD 13.05.2026
Дополнительная информация**
Sender to Receiver Information 72 /PYTR/GOD/
Расходы и комиссии по переводу (Bank charges and commissions):
  - за счет отправителя
OUR  - за счет получателя
BEN  - расходы банка ВТБ за
SHA
счет отправителя, расходы
инобанков - за счет получателя
Продленный операционный день
"""

BANK_TRANSFER_PDFTOTEXT_LAYOUT_TEXT = """
ЗАЯВЛЕНИЕ

на перевод № 2

от 21 мая 2026 г.

                                                                 Сумму перевода просим списать с нашего счёта у Вас (Please debit our account with
  В случае необходимости просим связаться по                     you):
  телефону
  (If required please call on):                                                         4 0 8 0 2 1 5 6 6 1 6 5 8 0 0 0 0 0 0 8

  Валюта                                                                   CNY
  Currency Code

  Сумма перевода                                                           541.962,50
                                                                  32
  (цифрами и прописью)                                                     Пятьсот сорок одна тысяча девятьсот шестьдесят два юаня 50/100
  Amount of transfer
  (in figures and in writing)

  Отправитель*                                                             IE TESTOV VLADISLAV RADIKOVICH
  (Наименование, адрес, город, страна, ИНН,                                V.I.LENINA APT.3 ELISTA
                                                                  50
  КПП, ОКПО и/или ОГРН)                                                    CODE COUNTRY: RU
  Ordering Customer (Name, address, city, country)                         INN: 560912740163

  Банк-посредник**
  (SWIFT, национальный клиринговый код,
  наименование, город, страна)                                    56
  Intermediary Institution (SWIFT BIC, national clearing
  code, name, city, country)

  Банк получателя*                                                         //CN767290000018
  Счет в Банке-посреднике, SWIFT,                                          VTB BANK (PJSC) SHANGHAI BRANCH VTBRCNSHXXX
  национальный клиринговый код,                                            SHANGHAI TOWER, RM. 2503-2505 FLOOR 25, 501 MIDDLE YINCHENG ROAD,
                                                                  57
  наименование, город, страна                                              PUDONG SHANGHAI
  Account with Institution, Beneficiary’s bank (SWIFT                      CN
  BIC, national clearing code, name, city, country)

  Получатель*                                                              40807156200610034920
  Номер счета (IBAN)
  Account number (IBAN)

  Наименование, адрес, город, страна                              59       GUANGZHOU ZIFRIEND COMMUNICATE TECHNOLOGY CO., LTD
  Beneficiary Customer (Name, address, city, country)                      GUANGZHOU
                                                                           CN

  Назначение платежа*                                                      CONTRACT 083/26 DD 13.05.2026
                                                                  70
  Details of payment

  Дополнительная информация**                                              /PYTR/GOD/
                                                                  72
  Sender to Receiver Information

  Информация для регулирующих органов**
                                                                 77B
  Regulatory reporting

                           Расходы и комиссии по переводу (Bank charges and commissions):

        OUR - за счет отправителя                               BEN - за счет получателя                              SHA - расходы банка ВТБ за
                                                                                                                 счет отправителя, расходы

                                                                                                       044525411
                                                                                                       Исполнен
                                                                                                       22.05.2026 в 00:43:03
"""

TEXT_BY_FILENAME = {
    "quote.pdf": QUOTE_TEXT,
    "quote-2026-06-19.pdf": QUOTE_2026_06_19_TEXT,
    "invoice-103.pdf": INVOICE_103_TEXT,
    "invoice-113.pdf": INVOICE_113_TEXT,
    "invoice-121.pdf": INVOICE_121_TEXT,
    "customs.pdf": CUSTOMS_TEXT,
    "bank-control.pdf": BANK_CONTROL_TEXT,
    "bank-transfer.pdf": BANK_TRANSFER_TEXT,
}


def _packing_list_workbook_bytes() -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Packing"
    rows = [
        ["26GN462 装箱单"],
        ["箱号", "名称&规格", "型号", "外箱数量", "数量/箱", "总数量", "毛重/箱", "总毛重", "纸箱尺寸", "体积"],
        ["1", "大猩猩除尘仓\n\n丝印高清膜\n带包装", "iPhone 14 / 13 / 13Pro", 1, 250, 250, 19.2, 19.2, "51*39*49", 21.538881],
        ["2--4", "", "iPhone 14 Pro", 3, 250, 750, 19.65, 58.95, "", ""],
        ["5--221", "", "iPhone 14 Pro Max", 217, 250, 54250, 21.22, 4602.3, "", ""],
        ["Total: 221 CTNS", "", "", 221, "", 55250, "", 4680.45, "", 21.538881],
    ]
    for row in rows:
        worksheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


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
        or customs.get("total_places") != 465
        or not _approx(customs.get("customs_gross_weight_kg"), 9784.6, tolerance=0.01)
        or not _approx(customs.get("customs_net_weight_kg"), 8806.18, tolerance=0.01)
        or customs.get("total_customs_payments_rub") != 2892511.6
    ):
        raise AssertionError(f"customs parser fields mismatch: {customs}")

    bank_control_payload = parse_financial_document_text(BANK_CONTROL_TEXT, filename="bank-control.txt")
    bank_control = bank_control_payload["normalized_parse"]
    if (
        bank_control.get("document_type") != "bank_control_statement"
        or bank_control.get("unique_contract_registration_number") != "26051384/1000/0081/2/2"
        or bank_control.get("document_date") != "2026-05-12"
        or bank_control.get("resident_name") != "Индивидуальный предприниматель ТЕСТОВ ВЛАДИСЛАВ РАДИКОВИЧ"
        or bank_control.get("non_resident_vendor") != "Guangzhou Zifriend Communicate Technology Co., Ltd"
        or bank_control.get("contract_number") != "082/26"
        or bank_control.get("contract_date") != "2026-04-04"
        or bank_control.get("contract_currency_code") != "156"
        or bank_control.get("contract_amount") != 785087.5
        or bank_control.get("payment_operation_date") != "2026-05-13"
        or bank_control.get("payment_operation_amount") != 785087.5
        or len(bank_control.get("payment_operations") or []) != 1
        or (bank_control.get("payment_operations") or [{}])[0].get("operation_type_code") != "11100"
        or bank_control.get("calculated_balance") != -785087.5
        or bank_control_payload.get("errors")
    ):
        raise AssertionError(f"bank control parser fields mismatch: {bank_control_payload}")

    bank_control_multi_payload = parse_financial_document_text(
        BANK_CONTROL_MULTI_PAYMENT_TEXT,
        filename="bank-control-multi-payment.txt",
    )
    bank_control_multi = bank_control_multi_payload["normalized_parse"]
    payment_operations = bank_control_multi.get("payment_operations") or []
    if (
        bank_control_multi.get("document_type") != "bank_control_statement"
        or bank_control_multi.get("unique_contract_registration_number") != "26062743/1000/0081/2/2"
        or bank_control_multi.get("document_date") != "2026-06-11"
        or bank_control_multi.get("contract_number") != "FR-001/26"
        or bank_control_multi.get("contract_date") != "2026-06-08"
        or bank_control_multi.get("contract_currency_code") != "156"
        or bank_control_multi.get("contract_amount") is not None
        or bank_control_multi.get("contract_amount_raw") != "БС"
        or bank_control_multi.get("total_payment_operations_amount") != 405258.75
        or bank_control_multi.get("total_amount") != 405258.75
        or len(payment_operations) != 2
        or payment_operations[0].get("row_index") != 1
        or payment_operations[0].get("operation_date") != "2026-06-11"
        or payment_operations[0].get("payment_direction") != "2"
        or payment_operations[0].get("operation_type_code") != "11100"
        or payment_operations[0].get("payment_currency_code") != "156"
        or payment_operations[0].get("payment_amount") != 345337.5
        or payment_operations[0].get("contract_currency_code") != "156"
        or payment_operations[0].get("contract_amount") != 345337.5
        or payment_operations[0].get("expected_repatriation_date") != "2027-06-07"
        or payment_operations[1].get("row_index") != 2
        or payment_operations[1].get("payment_amount") != 59921.25
        or payment_operations[1].get("expected_repatriation_date") != "2026-07-31"
        or bank_control_multi.get("calculated_balance") != -405258.75
        or bank_control_multi_payload.get("errors")
    ):
        raise AssertionError(f"multi-payment bank control parser fields mismatch: {bank_control_multi_payload}")

    bank_transfer_payload = parse_financial_document_text(BANK_TRANSFER_TEXT, filename="bank-transfer.txt")
    _assert_bank_transfer_payload(bank_transfer_payload, label="bank transfer pypdf-layout")
    bank_transfer_pdftotext_payload = parse_financial_document_text(
        BANK_TRANSFER_PDFTOTEXT_LAYOUT_TEXT,
        filename="bank-transfer-pdftotext-layout.txt",
    )
    _assert_bank_transfer_payload(bank_transfer_pdftotext_payload, label="bank transfer pdftotext-layout")
    _assert_packing_list_parser_smoke()
    _assert_bank_control_multi_payment_match_smoke(bank_control_multi_payload)
    _assert_order_document_verification_smoke(bank_transfer_payload)
    _assert_summary_metrics_smoke()
    _assert_missing_customs_data_summary_smoke()
    _assert_new_quote_parser_smoke()
    _assert_bad_quote_rate_guardrail_smoke()
    _assert_registry_lead_time_rows_smoke()
    _assert_approx_landed_cost_summary_smoke()
    _assert_incomplete_quote_summary_smoke()


def _assert_bank_transfer_payload(payload: dict[str, Any], *, label: str) -> None:
    bank_transfer = payload["normalized_parse"]
    if (
        bank_transfer.get("document_type") != "bank_transfer_application"
        or bank_transfer.get("transfer_application_number") != "2"
        or bank_transfer.get("document_date") != "2026-05-21"
        or bank_transfer.get("execution_status") != "Исполнен"
        or bank_transfer.get("execution_time") != "22.05.2026 00:43:03"
        or bank_transfer.get("debit_account") != "40802156616580000008"
        or bank_transfer.get("currency") != "CNY"
        or bank_transfer.get("transfer_amount") != 541962.5
        or bank_transfer.get("amount_in_words") != "Пятьсот сорок одна тысяча девятьсот шестьдесят два юаня 50/100"
        or bank_transfer.get("ordering_customer") != "IE TESTOV VLADISLAV RADIKOVICH"
        or bank_transfer.get("payer_inn") != "560912740163"
        or bank_transfer.get("payer_country_code") != "RU"
        or bank_transfer.get("beneficiary_customer") != "GUANGZHOU ZIFRIEND COMMUNICATE TECHNOLOGY CO., LTD"
        or bank_transfer.get("beneficiary_account") != "40807156200610034920"
        or bank_transfer.get("beneficiary_bank_swift_bic") != "VTBRCNSHXXX"
        or bank_transfer.get("beneficiary_bank_clearing_code") != "//CN767290000018"
        or bank_transfer.get("beneficiary_bank_country") != "CN"
        or "VTB BANK" not in str(bank_transfer.get("beneficiary_bank") or "")
        or bank_transfer.get("payment_details") != "CONTRACT 083/26 DD 13.05.2026"
        or bank_transfer.get("contract_ref") != "CONTRACT 083/26 DD 13.05.2026"
        or bank_transfer.get("contract_number") != "083/26"
        or bank_transfer.get("contract_date") != "2026-05-13"
        or bank_transfer.get("charges_mode") != "OUR"
        or bank_transfer.get("sender_to_receiver_info") != "/PYTR/GOD/"
        or payload.get("errors")
        or payload.get("warnings")
    ):
        raise AssertionError(f"{label} parser fields mismatch: {payload}")


def _assert_packing_list_parser_smoke() -> None:
    payload = parse_financial_document_upload(_packing_list_workbook_bytes(), filename="packing-list.xlsx")
    normalized = payload["normalized_parse"]
    if (
        normalized.get("document_type") != "packing_list"
        or normalized.get("document_title") != "26GN462 装箱单"
        or normalized.get("document_number") != "26GN462"
        or normalized.get("total_cartons") != 221.0
        or normalized.get("total_quantity") != 55250.0
        or normalized.get("total_gross_weight_kg") != 4680.45
        or normalized.get("total_volume_m3") != 21.538881
        or normalized.get("carton_size") != "51*39*49"
        or normalized.get("line_item_count") != 3
        or payload.get("errors")
    ):
        raise AssertionError(f"packing list parser fields mismatch: {payload}")
    first_line = (normalized.get("line_items") or [{}])[0]
    if first_line.get("carton_range") != "1" or first_line.get("model") != "iPhone 14 / 13 / 13Pro":
        raise AssertionError(f"packing list line item mismatch: {first_line}")


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


def _assert_bank_control_multi_payment_match_smoke(bank_control_payload: dict[str, Any]) -> None:
    document = {
        "document_type": "bank_control_statement",
        "parse_status": "parsed",
        "normalized_parse": dict(bank_control_payload.get("normalized_parse") or {}),
        "warnings": [],
    }
    first_invoice_match = apply_supplier_order_document_match(
        document,
        {
            "contract_no": "FR-001/26",
            "contract_date": "2026-06-08",
            "invoice_no": "26GN462",
            "invoice_date": "2026-06-09",
            "invoice_amount_total": 345337.5,
            "currency": "RMB/CNY",
            "supplier_name": "Guangzhou Zifriend Communicate Technology Co., Ltd",
        },
    )
    if (
        first_invoice_match.get("order_match_status") != "matched"
        or first_invoice_match.get("payment_operation_match_status") != "matched"
        or first_invoice_match.get("matched_payment_operation_row_index") != 1
        or first_invoice_match.get("parse_status") != "parsed"
    ):
        raise AssertionError(f"first invoice must match bank control payment row 1: {first_invoice_match}")
    first_normalized = first_invoice_match.get("normalized_parse") or {}
    first_operation = first_normalized.get("matched_payment_operation") or {}
    if first_operation.get("payment_amount") != 345337.5 or first_operation.get("operation_date") != "2026-06-11":
        raise AssertionError(f"first invoice matched operation payload mismatch: {first_invoice_match}")

    second_invoice_match = apply_supplier_order_document_match(
        document,
        {
            "contract_no": "FR-001/26",
            "contract_date": "2026-06-08",
            "invoice_no": "26GN463",
            "invoice_date": "2026-06-29",
            "invoice_amount_total": 59921.25,
            "currency": "CNY",
            "supplier_name": "Guangzhou Zifriend Communicate Technology Co., Ltd",
        },
    )
    if (
        second_invoice_match.get("order_match_status") != "matched"
        or second_invoice_match.get("payment_operation_match_status") != "matched"
        or second_invoice_match.get("matched_payment_operation_row_index") != 2
        or second_invoice_match.get("parse_status") != "parsed"
    ):
        raise AssertionError(f"second invoice must match bank control payment row 2: {second_invoice_match}")

    missing_payment_match = apply_supplier_order_document_match(
        document,
        {
            "contract_no": "FR-001/26",
            "contract_date": "2026-06-08",
            "invoice_no": "26GN464",
            "invoice_date": "2026-06-09",
            "invoice_amount_total": 111111.0,
            "currency": "CNY",
            "supplier_name": "Guangzhou Zifriend Communicate Technology Co., Ltd",
        },
    )
    if (
        missing_payment_match.get("order_match_status") != "needs_review"
        or missing_payment_match.get("payment_operation_match_status") != "needs_review"
        or missing_payment_match.get("parse_status") != "needs_review"
        or not any("нет платёжной строки" in warning for warning in missing_payment_match.get("warnings", []))
    ):
        raise AssertionError(f"missing bank control payment row must require review: {missing_payment_match}")


def _assert_order_document_verification_smoke(bank_transfer_payload: dict[str, Any]) -> None:
    document = {
        "document_type": "bank_transfer_application",
        "parse_status": "parsed",
        "normalized_parse": dict(bank_transfer_payload.get("normalized_parse") or {}),
        "warnings": [],
    }
    matched = apply_supplier_order_document_match(
        document,
        {
            "contract_no": "083/26",
            "contract_date": "2026-05-13",
            "invoice_amount_total": 541962.5,
            "currency": "CNY",
            "supplier_name": "GUANGZHOU ZIFRIEND COMMUNICATE TECHNOLOGY CO., LTD",
        },
    )
    if matched.get("order_match_status") != "matched" or matched.get("parse_status") != "parsed":
        raise AssertionError(f"matching bank transfer must stay parsed/matched: {matched}")

    mismatch = apply_supplier_order_document_match(
        document,
        {"contract_no": "082/26", "contract_date": "2026-04-04", "currency": "CNY"},
    )
    if mismatch.get("order_match_status") != "mismatch" or mismatch.get("parse_status") != "needs_review":
        raise AssertionError(f"wrong contract bank transfer must be needs_review/mismatch: {mismatch}")
    if not any("другому заказу" in warning for warning in mismatch.get("warnings", [])):
        raise AssertionError(f"mismatch warning missing: {mismatch}")

    poor_parse = {
        **document,
        "normalized_parse": {
            key: value
            for key, value in dict(document["normalized_parse"]).items()
            if key not in {"contract_ref", "contract_number", "contract_date", "payment_details"}
        },
    }
    needs_review = apply_supplier_order_document_match(
        poor_parse,
        {"contract_no": "083/26", "contract_date": "2026-05-13", "currency": "CNY"},
    )
    if needs_review.get("order_match_status") != "needs_review" or needs_review.get("parse_status") != "needs_review":
        raise AssertionError(f"poor parse must require review: {needs_review}")

    with TemporaryDirectory(prefix="supplier-financial-linked-contract-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        now = "2026-05-30T08:00:00Z"
        runtime.save_supplier_shipment(
            header={
                "shipment_id": "sup_linked_contract_match",
                "created_at": now,
                "updated_at": now,
                "shipment_date": "2026-05-21",
                "order_status": "production",
                "invoice_no": "LINKED-CONTRACT-ORDER",
                "invoice_date": "2026-05-21",
                "contract_no": "",
                "contract_date": "",
                "supplier_name": "GUANGZHOU ZIFRIEND COMMUNICATE TECHNOLOGY CO., LTD",
                "customer_name": "",
                "currency": "CNY",
                "product_qty_total": 1,
                "product_amount_total": 541962.5,
                "extras_amount_total": 0,
                "invoice_amount_total": 541962.5,
                "declared_invoice_total": 541962.5,
                "match_status": "all_matched",
                "source_filename": "linked-contract.xlsx",
                "source_file_sha256": "linked-contract-sha",
                "source_file_path": "",
                "invoice_document_id": "tdoc_linked_invoice",
                "parser_version": "fixture",
                "warnings": [],
                "errors": [],
            },
            lines=[],
        )
        runtime.save_trade_document(
            {
                "document_id": "tdoc_linked_invoice",
                "document_type": "invoice",
                "number": "LINKED-CONTRACT-ORDER",
                "document_date": "2026-05-21",
                "supplier_name": "GUANGZHOU ZIFRIEND COMMUNICATE TECHNOLOGY CO., LTD",
                "currency": "CNY",
                "amount_total": 541962.5,
                "source": "fixture",
                "source_shipment_id": "sup_linked_contract_match",
                "source_upload_id": "",
                "file_original_name": "linked-contract.xlsx",
                "file_content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "file_sha256": "linked-invoice-sha",
                "file_path": "",
                "parser_version": "fixture",
                "parsed_metadata": {},
                "warnings": [],
                "errors": [],
                "status": "active",
                "created_at": now,
                "updated_at": now,
            }
        )
        runtime.save_trade_document(
            {
                "document_id": "tdoc_linked_contract",
                "document_type": "contract",
                "number": "083/26",
                "document_date": "2026-05-13",
                "supplier_name": "GUANGZHOU ZIFRIEND COMMUNICATE TECHNOLOGY CO., LTD",
                "currency": "CNY",
                "amount_total": None,
                "source": "fixture",
                "source_shipment_id": "",
                "source_upload_id": "",
                "file_original_name": "linked-contract.pdf",
                "file_content_type": "application/pdf",
                "file_sha256": "linked-contract-sha",
                "file_path": "",
                "parser_version": "fixture",
                "parsed_metadata": {},
                "warnings": [],
                "errors": [],
                "status": "active",
                "created_at": now,
                "updated_at": now,
            }
        )
        runtime.save_invoice_contract_link(
            invoice_document_id="tdoc_linked_invoice",
            contract_document_id="tdoc_linked_contract",
            created_at=now,
            updated_at=now,
            linked_by="smoke",
            source="fixture",
        )
        block = SupplierFinancialDocumentsBlock(
            runtime=runtime,
            timestamp_factory=lambda: now,
            usd_rate_provider=StaticUsdRateProvider({}),
            pdf_text_extractor=lambda file_bytes, filename: (BANK_TRANSFER_TEXT, {"method": "fixture"}, []),
        )
        linked_upload = block.upload_document(
            "sup_linked_contract_match",
            file_bytes=b"%PDF-1.4\n% linked contract smoke\n",
            uploaded_filename="linked-bank-transfer.pdf",
        )
        if linked_upload.get("order_match_status") != "matched" or linked_upload.get("parse_status") != "parsed":
            raise AssertionError(f"upload must match via linked contract when header contract is empty: {linked_upload}")


def _assert_approx_landed_cost_summary_smoke() -> None:
    documents = [
        {"document_id": "quote", "document_type": "logistics_quote", "parse_status": "parsed", "normalized_parse": {}},
        {"document_id": "invoice", "document_type": "logistics_invoice", "parse_status": "parsed", "normalized_parse": {}},
        {"document_id": "customs", "document_type": "customs_declaration", "parse_status": "parsed", "normalized_parse": {}},
    ]
    lines = [
        {
            "line_id": "quote_line",
            "financial_document_id": "quote",
            "category": "delivery_cost",
            "currency": "USD",
            "amount": 999999.0,
            "amount_rub": 999999.0,
            "included_in_logistics_efficiency": True,
            "included_in_customs_total": False,
        },
        {
            "line_id": "invoice_line",
            "financial_document_id": "invoice",
            "category": "domestic_transport",
            "currency": "RUB",
            "amount": 20000.0,
            "amount_rub": 20000.0,
            "included_in_logistics_efficiency": True,
            "included_in_customs_total": False,
        },
        {
            "line_id": "customs_line",
            "financial_document_id": "customs",
            "category": "customs_payments_and_fees",
            "currency": "RUB",
            "amount": 28000.0,
            "amount_rub": 28000.0,
            "included_in_logistics_efficiency": False,
            "included_in_customs_total": True,
        },
    ]
    shipment = {
        "header": {
            "shipment_id": "approx_formula",
            "invoice_amount_total": 10000,
            "approx_yuan_rate": 13.2,
            "product_qty_total": 600,
        },
        "lines": [],
    }
    quote_only_summary = build_financial_summary([documents[0]], [lines[0]], shipment=shipment)
    quote_only_per_unit = quote_only_summary.get("per_unit") or {}
    if (
        quote_only_per_unit.get("factual_supply_expenses_rub") is not None
        or quote_only_per_unit.get("approx_invoice_cost_rub") != 132000.0
        or quote_only_per_unit.get("approx_landed_cost_per_unit_rub") is not None
    ):
        raise AssertionError(f"quote-only expenses must not produce approximate landed cost: {quote_only_summary}")
    summary = build_financial_summary(documents, lines, shipment=shipment)
    per_unit = summary.get("per_unit") or {}
    if (
        per_unit.get("factual_supply_expenses_rub") != 48000.0
        or per_unit.get("approx_invoice_cost_rub") != 132000.0
        or per_unit.get("approx_landed_cost_per_unit_rub") != 300.0
    ):
        raise AssertionError(f"approx landed formula must use invoice CNY, manual CNY rate, fact expenses and qty only: {summary}")
    registry = build_supplier_shipment_registry(
        [
            {
                "shipment_id": "approx_formula",
                "header": shipment["header"],
                "lines": [],
                "documents": documents,
                "expense_lines": lines,
                "summary": summary,
            }
        ]
    )
    if _registry_cell_display(registry, "cargo_value", "approx_landed_cost_per_unit_rub", "approx_formula") != "300.00 ₽":
        raise AssertionError(f"registry matrix must expose approximate landed cost row: {registry}")


def _assert_summary_metrics_smoke() -> None:
    quote_payload = parse_financial_document_text(QUOTE_TEXT, filename="quote.txt")
    documents, lines = _summary_fixture_documents_and_lines(quote_payload, include_customs=True)
    summary = build_financial_summary(documents, lines, shipment=_summary_shipment_fixture())
    _assert_current_financial_metrics(summary)
    if summary.get("warnings", []) and any("Нет " in warning for warning in summary.get("warnings", [])):
        raise AssertionError(f"complete summary must not report missing metric source warnings: {summary.get('warnings')}")


def _assert_missing_customs_data_summary_smoke() -> None:
    quote_payload = parse_financial_document_text(QUOTE_TEXT, filename="quote.txt")
    documents, lines = _summary_fixture_documents_and_lines(quote_payload, include_customs=False)
    summary = build_financial_summary(documents, lines)
    customs_weight = summary.get("per_kg", {}).get("customs_weight", {})
    fact_percent = summary.get("percent_of_value", {}).get("fact_customs_value", {})
    for key in ("logistics_invoice_rub_per_kg", "customs_payments_rub_per_kg", "delivery_customs_rub_per_kg"):
        if customs_weight.get(key) is not None:
            raise AssertionError(f"missing-DT {key} must be unavailable: {customs_weight}")
    for key in ("logistics_pct", "customs_without_vat_pct", "customs_with_vat_pct", "delivery_customs_pct"):
        if fact_percent.get(key) is not None:
            raise AssertionError(f"missing-DT {key} must be unavailable: {fact_percent}")
    per_unit = summary.get("per_unit", {})
    if per_unit.get("quote_delivery_customs_rub_per_unit") is not None or per_unit.get("fact_delivery_customs_rub_per_unit") is not None:
        raise AssertionError(f"missing total_units must make per-unit metrics unavailable: {per_unit}")
    if "NaN" in json.dumps(summary, ensure_ascii=False) or "Infinity" in json.dumps(summary, ensure_ascii=False):
        raise AssertionError(f"missing-data summary must not expose invalid numbers: {summary}")


def _assert_new_quote_parser_smoke() -> None:
    quote_payload = parse_financial_document_text(QUOTE_2026_06_19_TEXT, filename="transitplus-2026-06-19.txt")
    quote = quote_payload["normalized_parse"]
    expected = {
        "quote_date": "2026-06-19",
        "vendor": "Transitplus International Ltd",
        "tariff": "Авто стандарт 25-30 дней",
        "origin": "Guangzhou / Гуанчжоу",
        "destination": "Москва",
        "delivery_days_min": 25,
        "delivery_days_max": 30,
        "gross_weight_kg": 6713.45,
        "net_weight_kg": 6713.45,
        "volume_m3": 31.28,
        "estimated_cargo_value_usd": 77423.22,
        "estimated_cargo_value_cny": 541962.50,
        "delivery_cost_usd": 12420.0,
        "customs_payments_and_fees_usd": 27175.0,
        "ecological_fee_usd": 0.0,
        "brokerage_services_usd": 350.0,
        "company_commission_usd": 0.0,
        "insurance_usd": 775.0,
        "total_quote_usd": 40720.0,
        "quote_logistics_component_usd": 13545.0,
        "quote_customs_component_usd": 27175.0,
    }
    for key, expected_value in expected.items():
        actual = quote.get(key)
        if actual != expected_value:
            raise AssertionError(f"new Transitplus quote {key} mismatch: expected {expected_value}, got {actual}; quote={quote}")
    documents, lines = _summary_documents_and_lines_from_payloads([("quote-2026-06-19", quote_payload)])
    summary = build_financial_summary(documents, lines)
    quote_percent = summary.get("percent_of_value", {}).get("quote_cargo_value", {})
    if (
        not _approx(quote_percent.get("logistics_pct"), 17.49, tolerance=0.01)
        or not _approx(quote_percent.get("customs_pct"), 35.10, tolerance=0.01)
        or not _approx(quote_percent.get("delivery_customs_pct"), 52.59, tolerance=0.01)
    ):
        raise AssertionError(f"new Transitplus quote percent metrics mismatch: {quote_percent}")


def _assert_bad_quote_rate_guardrail_smoke() -> None:
    quote_payload = parse_financial_document_text(QUOTE_2026_06_19_TEXT, filename="quote-2026-06-19.txt")
    invoice_121 = parse_financial_document_text(INVOICE_121_TEXT, filename="invoice-121.txt")
    documents, lines = _summary_documents_and_lines_from_payloads(
        [("quote-2026-06-19", quote_payload), ("invoice-121", invoice_121)]
    )
    shipment = {
        "header": {
            "shipment_id": "bad_rate",
            "product_qty_total": 80250,
        },
        "lines": [],
    }
    summary = build_financial_summary(documents, lines, shipment=shipment)
    match = summary.get("quote_invoice_match") or {}
    per_unit = summary.get("per_unit") or {}
    quote = summary.get("quote") or {}
    if quote.get("total_usd") != 40720.0 or quote.get("total_rub_equivalent") is not None:
        raise AssertionError(f"bad-rate guard must preserve USD quote and hide RUB quote total: {summary}")
    if (
        match.get("rate_sanity_status") != "rejected"
        or match.get("implied_rate") is not None
        or match.get("estimated_bank_rate_on_quote_date") is not None
        or not _approx(match.get("rejected_implied_rate"), 0.37, tolerance=0.01)
    ):
        raise AssertionError(f"bad-rate guard must reject implausible implied rate: {match}")
    if (
        per_unit.get("quote_total_rub_equivalent") is not None
        or per_unit.get("quote_delivery_customs_rub_per_unit") is not None
    ):
        raise AssertionError(f"bad-rate guard must hide quote RUB/unit metrics: {per_unit}")
    if not any("рублёвые КП-метрики скрыты" in warning for warning in summary.get("warnings", [])):
        raise AssertionError(f"bad-rate guard must surface needs-review warning: {summary.get('warnings')}")
    registry = build_supplier_shipment_registry(
        [
            {
                "shipment_id": "bad_rate",
                "header": shipment["header"],
                "lines": [],
                "documents": documents,
                "expense_lines": lines,
                "summary": summary,
            }
        ]
    )
    if _registry_cell_display(registry, "quote_logistics", "quote_total_usd", "bad_rate") != "40 720.00 USD":
        raise AssertionError(f"bad-rate registry must still show USD quote total: {registry}")
    for row_id in (
        "quote_total_rub",
        "quote_total_rub_per_unit",
        "quote_logistics_rub_per_quote_kg",
        "quote_customs_rub_per_quote_kg",
        "quote_total_rub_per_quote_kg",
    ):
        if _registry_cell_display(registry, "quote_logistics", row_id, "bad_rate") != "—":
            raise AssertionError(f"bad-rate registry row {row_id} must render blank: {registry}")


def _assert_registry_lead_time_rows_smoke() -> None:
    quote_payload = parse_financial_document_text(QUOTE_TEXT, filename="quote.txt")
    documents, lines = _summary_fixture_documents_and_lines(quote_payload, include_customs=False)
    shipment = {
        "header": {
            "shipment_id": "missing_dates",
            "product_qty_total": 10,
        },
        "lines": [],
    }
    registry = build_supplier_shipment_registry(
        [
            {
                "shipment_id": "missing_dates",
                "header": shipment["header"],
                "lines": [],
                "documents": documents,
                "expense_lines": lines,
                "summary": build_financial_summary(documents, lines, shipment=shipment),
            },
            {
                "shipment_id": "invalid_fact_dates",
                "header": {
                    "shipment_id": "invalid_fact_dates",
                    "shipment_date": "not-a-date",
                    "actual_shipment_date": "2026/06/02",
                    "actual_ff_acceptance_date": "2026-06-99",
                    "product_qty_total": 10,
                },
                "lines": [],
                "documents": documents,
                "expense_lines": lines,
                "summary": build_financial_summary(documents, lines, shipment=shipment),
            }
        ]
    )
    labels = _registry_row_labels(registry, "lead_times")
    if "Срок до ДТ" not in labels:
        raise AssertionError(f"registry lead-times must expose Срок до ДТ: {labels}")
    if "Фактический срок поставки" not in labels:
        raise AssertionError(f"registry lead-times must expose actual delivery days: {labels}")
    forbidden = " ".join(labels).lower()
    if "отклонение срока" in forbidden:
        raise AssertionError(f"registry lead-times must not expose misleading rows: {labels}")
    if _registry_cell_display(registry, "lead_times", "actual_delivery_days", "missing_dates") != "—":
        raise AssertionError(f"missing fact shipment dates must render actual delivery days as unavailable: {registry}")
    if _registry_cell_display(registry, "lead_times", "days_to_customs_declaration", "missing_dates") != "—":
        raise AssertionError(f"missing lead-time dates must render as unavailable: {registry}")
    if _registry_cell_display(registry, "cargo_value", "approx_landed_cost_per_unit_rub", "missing_dates") != "—":
        raise AssertionError(f"missing approx_yuan_rate must render approximate landed cost as unavailable: {registry}")
    if (
        _registry_cell_display(registry, "passport", "shipment_date", "invalid_fact_dates") != "—"
        or _registry_cell_display(registry, "passport", "actual_shipment_date", "invalid_fact_dates") != "—"
        or _registry_cell_display(registry, "passport", "actual_ff_acceptance_date", "invalid_fact_dates") != "—"
        or _registry_cell_display(registry, "lead_times", "actual_delivery_days", "invalid_fact_dates") != "—"
    ):
        raise AssertionError(f"invalid registry dates must render as unavailable: {registry}")
    if len(registry.get("warnings", [])) < 3:
        raise AssertionError(f"invalid registry dates must surface warnings: {registry}")


def _assert_current_financial_metrics(summary: dict[str, Any]) -> None:
    quote_weight = summary.get("per_kg", {}).get("quote_weight", {})
    customs_weight = summary.get("per_kg", {}).get("customs_weight", {})
    per_unit = summary.get("per_unit", {})
    quote_percent = summary.get("percent_of_value", {}).get("quote_cargo_value", {})
    fact_percent = summary.get("percent_of_value", {}).get("fact_customs_value", {})
    expected_metrics = [
        (quote_weight.get("logistics_invoice_rub_per_kg"), 126.08, "quote-weight logistics rub/kg"),
        (quote_weight.get("customs_payments_rub_per_kg"), 299.91, "quote-weight customs rub/kg"),
        (quote_weight.get("delivery_customs_rub_per_kg"), 425.99, "quote-weight total rub/kg"),
        (customs_weight.get("logistics_invoice_rub_per_kg"), 124.27, "customs-weight logistics rub/kg"),
        (customs_weight.get("customs_payments_rub_per_kg"), 295.62, "customs-weight customs rub/kg"),
        (customs_weight.get("delivery_customs_rub_per_kg"), 419.89, "customs-weight total rub/kg"),
        (quote_percent.get("logistics_pct"), 14.40, "quote logistics percent"),
        (quote_percent.get("customs_pct"), 36.54, "quote customs percent"),
        (quote_percent.get("delivery_customs_pct"), 50.94, "quote total percent"),
        (per_unit.get("total_units"), 116250.0, "total units"),
        (per_unit.get("quote_total_rub_equivalent"), 4088263.64, "quote total RUB equivalent", 100.0),
        (per_unit.get("quote_delivery_customs_rub_per_unit"), 35.17, "quote delivery+customs RUB/unit"),
        (per_unit.get("fact_delivery_customs_rub_per_unit"), 35.34, "fact delivery+customs RUB/unit"),
        (per_unit.get("factual_supply_expenses_rub"), 4108486.6, "factual supply expenses RUB"),
        (fact_percent.get("logistics_pct"), 14.63, "fact logistics percent"),
        (fact_percent.get("customs_without_vat_pct"), 10.59, "fact customs without VAT percent"),
        (fact_percent.get("customs_with_vat_pct"), 34.79, "fact customs with VAT percent"),
        (fact_percent.get("delivery_customs_pct"), 49.42, "fact total percent"),
    ]
    for item in expected_metrics:
        actual, expected, label = item[:3]
        tolerance = item[3] if len(item) > 3 else 0.01
        if not _approx(actual, expected, tolerance=tolerance):
            raise AssertionError(f"{label} mismatch: expected {expected}, got {actual}; summary={summary}")
    if not _approx(summary.get("customs_declaration", {}).get("gross_weight_kg"), 9784.6, tolerance=0.01):
        raise AssertionError(f"summary customs gross weight mismatch: {summary}")
    if fact_percent.get("customs_payments_without_vat_rub") != 880605.99:
        raise AssertionError(f"summary customs without VAT mismatch: {fact_percent}")


def _summary_shipment_fixture() -> dict[str, Any]:
    return {
        "header": {
            "shipment_id": "sup_financial",
            "product_qty_total": 116250,
        },
        "lines": [],
    }


def _summary_fixture_documents_and_lines(quote_payload: dict[str, Any], *, include_customs: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    invoice_103 = parse_financial_document_text(INVOICE_103_TEXT, filename="invoice-103.txt")
    invoice_113 = parse_financial_document_text(INVOICE_113_TEXT, filename="invoice-113.txt")
    payloads = [("quote", quote_payload), ("invoice-103", invoice_103), ("invoice-113", invoice_113)]
    if include_customs:
        payloads.append(("customs", parse_financial_document_text(CUSTOMS_TEXT, filename="customs.txt")))
    return _summary_documents_and_lines_from_payloads(payloads)


def _summary_documents_and_lines_from_payloads(payloads: list[tuple[str, dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rates = {
        "quote": 78.0,
        "invoice-103": 77.5,
        "invoice-113": 82.07119747159833,
        "invoice-121": 77.06,
        "customs": None,
        "quote-2026-06-19": 78.0,
    }
    documents = [_document_from_parsed(document_id, payload, cbr_rate=rates.get(document_id)) for document_id, payload in payloads]
    lines: list[dict[str, Any]] = []
    for document, (_, parsed) in zip(documents, payloads, strict=True):
        for line in parsed.get("expense_lines", []):
            next_line = dict(line)
            next_line["financial_document_id"] = document["document_id"]
            lines.append(next_line)
    return documents, lines


def _document_from_parsed(document_id: str, parsed: dict[str, Any], *, cbr_rate: float | None) -> dict[str, Any]:
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
                    "2026-06-18": "82.07119747159833",
                    "2026-06-19": "78.00",
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
            _assert_current_financial_metrics(summary)
            registry_status, registry = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_PATH}")
            if registry_status != 200 or registry.get("contract_name") != "sheet_vitrina_v1_supplier_shipment_registry":
                raise AssertionError(f"shipment registry route mismatch: {registry_status} {registry}")
            if registry.get("meta", {}).get("shipment_count") != 1:
                raise AssertionError(f"shipment registry must expose one shipment column: {registry}")
            registry_json = json.dumps(registry, ensure_ascii=False)
            if "NaN" in registry_json or "Infinity" in registry_json:
                raise AssertionError(f"shipment registry must not expose invalid numeric output: {registry}")
            section_ids = [section.get("section_id") for section in registry.get("sections", [])]
            for expected_section in ("passport", "cargo_physics", "quote_logistics", "fact_expenses", "fact_normalized", "documents"):
                if expected_section not in section_ids:
                    raise AssertionError(f"shipment registry missing section {expected_section}: {section_ids}")
            lead_time_labels = _registry_row_labels(registry, "lead_times")
            if "Срок до ДТ" not in lead_time_labels:
                raise AssertionError(f"shipment registry lead-time row missing: {lead_time_labels}")
            if "Фактический срок поставки" not in lead_time_labels:
                raise AssertionError(f"shipment registry actual delivery row missing: {lead_time_labels}")
            forbidden_lead_time_labels = " ".join(lead_time_labels).lower()
            if "отклонение срока" in forbidden_lead_time_labels:
                raise AssertionError(f"shipment registry exposes misleading lead-time rows: {lead_time_labels}")
            if _registry_cell_display(registry, "lead_times", "actual_delivery_days", "sup_financial") != "17 дн.":
                raise AssertionError(f"registry actual delivery days mismatch: {registry}")
            if _registry_cell_display(registry, "lead_times", "days_to_customs_declaration", "sup_financial") != "8 дн.":
                raise AssertionError(f"registry days-to-customs-declaration mismatch: {registry}")
            if _registry_cell_display(registry, "quote_logistics", "quote_total_rub_per_unit", "sup_financial") != "35.17 ₽":
                raise AssertionError(f"registry quote ₽/шт mismatch: {registry}")
            if _registry_cell_display(registry, "fact_expenses", "fact_total_rub_per_unit", "sup_financial") != "35.34 ₽":
                raise AssertionError(f"registry fact ₽/шт mismatch: {registry}")
            if _registry_cell_display(registry, "cargo_value", "approx_landed_cost_per_unit_rub", "sup_financial") != "36.48 ₽":
                raise AssertionError(f"registry approximate landed cost mismatch: {registry}")
            if _registry_cell_display(registry, "cargo_physics", "customs_weight", "sup_financial") != "9 784.60 кг":
                raise AssertionError(f"registry customs weight mismatch: {registry}")
            compare_status, compare_payload = _post_multipart(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_COMPARE_QUOTE_PATH}",
                b"%PDF-1.4\n% synthetic quote comparison smoke\n",
                filename="quote-2026-06-19.pdf",
                fields={"shipment_id": "sup_financial"},
            )
            if (
                compare_status != 200
                or compare_payload.get("contract_name") != "sheet_vitrina_v1_supplier_shipment_registry_quote_comparison"
                or compare_payload.get("selected_shipment", {}).get("shipment_id") != "sup_financial"
            ):
                raise AssertionError(f"registry compare quote route mismatch: {compare_status} {compare_payload}")
            compare_json = json.dumps(compare_payload, ensure_ascii=False)
            if "NaN" in compare_json or "Infinity" in compare_json:
                raise AssertionError(f"registry compare quote must not expose invalid numbers: {compare_payload}")
            if '"лучше"' in compare_json or '"хуже"' in compare_json:
                raise AssertionError(f"registry compare quote must not expose bare better/worse statuses: {compare_payload}")
            quote_meta = compare_payload.get("quote", {}).get("normalized_parse", {})
            if (
                quote_meta.get("quote_date") != "2026-06-19"
                or quote_meta.get("gross_weight_kg") != 6713.45
                or quote_meta.get("total_quote_usd") != 40720.0
            ):
                raise AssertionError(f"registry compare quote parsed fields mismatch: {quote_meta}")
            if _comparison_cell_display(compare_payload, "quote_logistics", "quote_total_pct", "quote") != "52.59%":
                raise AssertionError(f"registry compare quote percent mismatch: {compare_payload}")
            if _comparison_cell_display(compare_payload, "normalized", "delivery_customs_pct_of_value", "shipment") != "49.42%":
                raise AssertionError(f"registry compare shipment fact percent mismatch: {compare_payload}")
            compare_lead_rows = _comparison_row_ids(compare_payload, "lead_times")
            if "quote_delivery_days" not in compare_lead_rows or "days_to_customs_declaration" not in compare_lead_rows:
                raise AssertionError(f"registry compare lead-time rows mismatch: {compare_lead_rows}")
            if "actual_delivery_days" in compare_lead_rows or "delivery_days_delta" in compare_lead_rows:
                raise AssertionError(f"registry compare must not expose misleading lead-time rows: {compare_lead_rows}")
            if _comparison_cell_display(compare_payload, "lead_times", "days_to_customs_declaration", "shipment") != "8 дн.":
                raise AssertionError(f"registry compare days-to-customs-declaration mismatch: {compare_payload}")
            if _comparison_cell_display(compare_payload, "quote_logistics", "quote_logistics_usd", "status") != "КП выгоднее":
                raise AssertionError(f"registry compare cheaper quote status mismatch: {compare_payload}")
            if _comparison_cell_display(compare_payload, "normalized", "delivery_customs_rub_per_kg", "status") != "КП дороже":
                raise AssertionError(f"registry compare costlier quote status mismatch: {compare_payload}")
            quote_unit_cell = _comparison_cell(compare_payload, "normalized", "delivery_customs_rub_per_unit", "quote")
            shipment_unit_cell = _comparison_cell(compare_payload, "normalized", "delivery_customs_rub_per_unit", "shipment")
            quote_kg_cell = _comparison_cell(compare_payload, "normalized", "delivery_customs_rub_per_kg", "quote")
            shipment_kg_cell = _comparison_cell(compare_payload, "normalized", "delivery_customs_rub_per_kg", "shipment")
            estimator = quote_unit_cell.get("estimator") or {}
            units_per_kg = 116250.0 / 9784.6
            estimated_quote_units = 6713.45 * units_per_kg
            quote_total_rub = compare_payload.get("quote", {}).get("summary", {}).get("quote", {}).get("total_rub_equivalent")
            if not _approx(estimator.get("units_per_kg"), units_per_kg, tolerance=0.01):
                raise AssertionError(f"registry compare units/kg estimator mismatch: {quote_unit_cell}")
            if not _approx(estimator.get("estimated_quote_units"), estimated_quote_units, tolerance=1.0):
                raise AssertionError(f"registry compare estimated quote units mismatch: {quote_unit_cell}")
            if not _approx(quote_unit_cell.get("value"), float(quote_total_rub) / estimated_quote_units, tolerance=0.01):
                raise AssertionError(f"registry compare quote ₽/шт must use estimated quote units: {quote_unit_cell}")
            if _approx(quote_unit_cell.get("value"), float(quote_total_rub) / 116250.0, tolerance=0.01):
                raise AssertionError(f"registry compare quote ₽/шт must not divide by selected shipment units: {quote_unit_cell}")
            if (
                float(quote_kg_cell.get("value")) > float(shipment_kg_cell.get("value"))
                and not float(quote_unit_cell.get("value")) > float(shipment_unit_cell.get("value"))
            ):
                raise AssertionError(f"quote ₽/шт must stay costlier when same units/kg estimator makes ₽/кг costlier: {compare_payload}")
            note = str(quote_unit_cell.get("note") or "")
            if "оценочно" not in note or "шт/кг" not in note:
                raise AssertionError(f"registry compare quote ₽/шт must explain estimator in note: {quote_unit_cell}")
            runtime.save_supplier_shipment(
                header={
                    "shipment_id": "missing_estimator",
                    "created_at": "2026-06-19T08:00:00Z",
                    "updated_at": "2026-06-19T08:00:00Z",
                    "shipment_date": "2026-06-19",
                    "order_status": "in_transit",
                    "invoice_no": "MISSING-ESTIMATOR",
                    "invoice_date": "2026-06-19",
                },
                lines=[],
            )
            missing_compare_status, missing_compare_payload = _post_multipart(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_COMPARE_QUOTE_PATH}",
                b"%PDF-1.4\n% synthetic quote comparison smoke\n",
                filename="quote-2026-06-19.pdf",
                fields={"shipment_id": "missing_estimator"},
            )
            if missing_compare_status != 200:
                raise AssertionError(f"registry missing-estimator compare route failed: {missing_compare_status} {missing_compare_payload}")
            missing_quote_unit_cell = _comparison_cell(missing_compare_payload, "normalized", "delivery_customs_rub_per_unit", "quote")
            if missing_quote_unit_cell.get("display") != "—":
                raise AssertionError(f"missing estimator must make quote ₽/шт unavailable: {missing_compare_payload}")
            if "Нет коэффициента шт/кг для оценки КП" not in missing_compare_payload.get("warnings", []):
                raise AssertionError(f"missing estimator warning missing: {missing_compare_payload}")
            after_compare_status, after_compare_list = _get_json(collection_url)
            if after_compare_status != 200 or len(after_compare_list.get("documents", [])) != 4:
                raise AssertionError(f"temporary quote compare must not persist financial documents: {after_compare_status} {after_compare_list}")
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
            packing_status, packing_payload = _post_multipart(
                collection_url,
                _packing_list_workbook_bytes(),
                filename="packing-list.xlsx",
            )
            if (
                packing_status != 200
                or packing_payload.get("document_type") != "packing_list"
                or packing_payload.get("parse_status") != "parsed"
                or (packing_payload.get("normalized_parse") or {}).get("total_cartons") != 221.0
            ):
                raise AssertionError(f"packing list upload failed: {packing_status} {packing_payload}")
            packed_status, packed_list = _get_json(collection_url)
            packing_summary = (packed_list.get("summary") or {}).get("packing_list") or {}
            if (
                packed_status != 200
                or len(packed_list.get("documents", [])) != 5
                or len(packed_list.get("expense_lines", [])) != 14
                or packing_summary.get("total_cartons") != 221.0
                or packing_summary.get("total_quantity") != 55250.0
                or packing_summary.get("total_gross_weight_kg") != 4680.45
                or packing_summary.get("total_volume_m3") != 21.538881
            ):
                raise AssertionError(f"packing list summary mismatch: {packed_status} {packed_list}")
            documents_url = f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/sup_financial/documents"
            documents_status, order_documents = _get_json(documents_url)
            if documents_status != 200 or order_documents.get("contract_name") != "sheet_vitrina_v1_supplier_order_documents":
                raise AssertionError(f"order documents route mismatch: {documents_status} {order_documents}")
            required_rows = order_documents.get("required_documents", [])
            if _required_document_status(required_rows, "invoice") != "Загружен":
                raise AssertionError(f"order documents must include uploaded invoice: {order_documents}")
            if _required_document_status(required_rows, "contract") != "Загружен":
                raise AssertionError(f"order documents must include uploaded contract: {order_documents}")
            if _required_document_status(required_rows, "bank_control_statement") != "Не загружен":
                raise AssertionError(f"missing bank control statement must be shown: {order_documents}")
            if _required_document_status(required_rows, "bank_transfer_application") != "Не загружен":
                raise AssertionError(f"missing bank transfer application must be shown: {order_documents}")
            if _required_document_status(required_rows, "bank_fee_statement") != "Не загружен":
                raise AssertionError(f"missing bank fees must be shown: {order_documents}")
            if _required_document_status(required_rows, "packing_list") != "Загружен":
                raise AssertionError(f"uploaded packing list must be shown: {order_documents}")
            archive_status, archive_bytes, _ = _get_bytes(f"{documents_url}/archive.zip")
            if archive_status != 200:
                raise AssertionError(f"all-documents archive route failed: {archive_status}")
            archive_manifest = _zip_manifest(archive_bytes)
            if not set(archive_manifest.get("missing_required_types", [])) >= {"bank_control_statement", "bank_transfer_application", "bank_fee_statement"}:
                raise AssertionError(f"all-documents archive must warn about missing bank docs: {archive_manifest}")

            for filename in ("bank-control.pdf", "bank-transfer.pdf"):
                status, payload = _post_multipart(collection_url, b"%PDF-1.4\n% synthetic bank smoke\n", filename=filename)
                if status != 200 or payload.get("parse_status") != "needs_review" or payload.get("order_match_status") != "mismatch":
                    raise AssertionError(f"bank document upload failed for {filename}: {status} {payload}")
                if not any("другому заказу" in warning for warning in payload.get("warnings", [])):
                    raise AssertionError(f"bank document mismatch warning missing for {filename}: {payload}")
            bank_documents_status, bank_order_documents = _get_json(documents_url)
            if bank_documents_status != 200:
                raise AssertionError(f"order documents after bank upload failed: {bank_documents_status} {bank_order_documents}")
            bank_rows = bank_order_documents.get("required_documents", [])
            if _required_document_status(bank_rows, "bank_control_statement") != "Проверить":
                raise AssertionError(f"uploaded bank control statement must be shown: {bank_order_documents}")
            if _required_document_status(bank_rows, "bank_transfer_application") != "Проверить":
                raise AssertionError(f"uploaded bank transfer application must be shown: {bank_order_documents}")
            if _required_document_status(bank_rows, "bank_fee_statement") != "Не загружен":
                raise AssertionError(f"missing bank fee import must still be shown: {bank_order_documents}")
            if _required_document_status(bank_rows, "packing_list") != "Загружен":
                raise AssertionError(f"packing list checklist status changed: {bank_order_documents}")
            document_rows = bank_order_documents.get("documents", [])
            payment_rows = [item for item in document_rows if item.get("document_type") == "bank_transfer_application"]
            if len(payment_rows) != 1:
                raise AssertionError(f"linked CNY supplier payment must not duplicate order document rows: {bank_order_documents}")
            logistics_status, logistics_bytes, _ = _get_bytes(f"{documents_url}/logistics-package.zip")
            if logistics_status != 200:
                raise AssertionError(f"logistics package route failed: {logistics_status}")
            logistics_manifest = _zip_manifest(logistics_bytes)
            logistics_types = [item.get("document_type") for item in logistics_manifest.get("included", [])]
            if set(logistics_manifest.get("missing_required_types", [])):
                raise AssertionError(f"logistics package must be complete after bank uploads: {logistics_manifest}")
            if set(logistics_types) != {"contract", "bank_control_statement", "bank_transfer_application"}:
                raise AssertionError(f"logistics package included wrong document types: {logistics_manifest}")
            if not any("другому заказу" in warning for warning in logistics_manifest.get("warnings", [])):
                raise AssertionError(f"logistics package must expose mismatch warnings: {logistics_manifest}")
            all_status, all_bytes, _ = _get_bytes(f"{documents_url}/archive.zip")
            all_manifest = _zip_manifest(all_bytes)
            all_types = [item.get("document_type") for item in all_manifest.get("included", [])]
            if all_status != 200 or len(all_manifest.get("included", [])) != 9:
                raise AssertionError(f"all-documents archive must include all uploaded docs: {all_status} {all_manifest}")
            for expected_type in ("invoice", "contract", "logistics_quote", "logistics_invoice", "customs_declaration", "bank_control_statement", "bank_transfer_application", "packing_list"):
                if expected_type not in all_types:
                    raise AssertionError(f"all-documents archive missing {expected_type}: {all_manifest}")
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _seed_supplier_order(runtime: RegistryUploadDbBackedRuntime) -> None:
    invoice_file = runtime.runtime_dir / "trade_documents" / "files" / "invoice" / "tdoc_invoice_safe" / "safe-order.xlsx"
    contract_file = runtime.runtime_dir / "trade_documents" / "files" / "contract" / "tdoc_contract_safe" / "contract-ore.pdf"
    invoice_file.parent.mkdir(parents=True, exist_ok=True)
    contract_file.parent.mkdir(parents=True, exist_ok=True)
    invoice_bytes = b"synthetic invoice workbook bytes"
    contract_bytes = b"%PDF-1.4\n% synthetic contract smoke\n"
    invoice_file.write_bytes(invoice_bytes)
    contract_file.write_bytes(contract_bytes)
    invoice_relative = invoice_file.relative_to(runtime.runtime_dir).as_posix()
    contract_relative = contract_file.relative_to(runtime.runtime_dir).as_posix()
    runtime.save_trade_document(
        {
            "document_id": "tdoc_invoice_safe",
            "document_type": "invoice",
            "number": "SAFE-ORDER",
            "document_date": "2026-06-02",
            "supplier_name": "HanShang Technology",
            "currency": "CNY",
            "amount_total": 0,
            "source": "smoke",
            "source_shipment_id": "sup_financial",
            "source_upload_id": "",
            "file_original_name": "safe-order.xlsx",
            "file_content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "file_sha256": hashlib.sha256(invoice_bytes).hexdigest(),
            "file_path": invoice_relative,
            "parser_version": "fixture",
            "parsed_metadata": {},
            "warnings": [],
            "errors": [],
            "status": "active",
            "created_at": "2026-06-19T08:00:00Z",
            "updated_at": "2026-06-19T08:00:00Z",
        }
    )
    runtime.save_trade_document(
        {
            "document_id": "tdoc_contract_safe",
            "document_type": "contract",
            "number": "ORE",
            "document_date": "2026-06-04",
            "supplier_name": "HanShang Technology",
            "currency": "",
            "amount_total": None,
            "source": "smoke",
            "source_shipment_id": "",
            "source_upload_id": "",
            "file_original_name": "contract-ore.pdf",
            "file_content_type": "application/pdf",
            "file_sha256": hashlib.sha256(contract_bytes).hexdigest(),
            "file_path": contract_relative,
            "parser_version": "fixture",
            "parsed_metadata": {},
            "warnings": [],
            "errors": [],
            "status": "active",
            "created_at": "2026-06-19T08:00:00Z",
            "updated_at": "2026-06-19T08:00:00Z",
        }
    )
    runtime.save_invoice_contract_link(
        invoice_document_id="tdoc_invoice_safe",
        contract_document_id="tdoc_contract_safe",
        created_at="2026-06-19T08:00:00Z",
        updated_at="2026-06-19T08:00:00Z",
        linked_by="smoke",
        source="smoke",
    )
    runtime.save_supplier_shipment(
        header={
            "shipment_id": "sup_financial",
            "created_at": "2026-06-19T08:00:00Z",
            "updated_at": "2026-06-19T08:00:00Z",
            "shipment_date": "2026-06-02",
            "actual_shipment_date": "2026-06-02",
            "actual_ff_acceptance_date": "2026-06-19",
            "order_status": "in_transit",
            "invoice_no": "SAFE-ORDER",
            "invoice_date": "2026-06-02",
            "contract_no": "ORE",
            "contract_date": "2026-06-04",
            "supplier_name": "HanShang Technology",
            "customer_name": "",
            "currency": "CNY",
            "approx_yuan_rate": 13.2,
            "product_qty_total": 116250,
            "product_amount_total": 0,
            "extras_amount_total": 0,
            "invoice_amount_total": 10000,
            "declared_invoice_total": 10000,
            "match_status": "all_matched",
            "source_filename": "safe.xlsx",
            "source_file_sha256": "",
            "source_file_path": invoice_relative,
            "invoice_document_id": "tdoc_invoice_safe",
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


def _required_document_status(rows: list[Mapping[str, Any]], document_type: str) -> str:
    for row in rows:
        if row.get("document_type") == document_type:
            return str(row.get("status_label") or "")
    return ""


def _zip_manifest(archive_bytes: bytes) -> dict[str, Any]:
    from io import BytesIO

    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        return json.loads(archive.read("manifest.json").decode("utf-8"))


def _registry_cell_display(registry: Mapping[str, Any], section_id: str, row_id: str, shipment_id: str) -> str:
    for section in registry.get("sections", []):
        if section.get("section_id") != section_id:
            continue
        for row in section.get("rows", []):
            if row.get("row_id") == row_id:
                return str((row.get("cells", {}).get(shipment_id) or {}).get("display") or "")
    return ""


def _registry_row_labels(registry: Mapping[str, Any], section_id: str) -> list[str]:
    for section in registry.get("sections", []):
        if section.get("section_id") == section_id:
            return [str(row.get("label") or "") for row in section.get("rows", [])]
    return []


def _comparison_row_ids(comparison: Mapping[str, Any], section_id: str) -> list[str]:
    for section in comparison.get("sections", []):
        if section.get("section_id") == section_id:
            return [str(row.get("row_id") or "") for row in section.get("rows", [])]
    return []


def _comparison_cell(comparison: Mapping[str, Any], section_id: str, row_id: str, cell_key: str) -> dict[str, Any]:
    for section in comparison.get("sections", []):
        if section.get("section_id") != section_id:
            continue
        for row in section.get("rows", []):
            if row.get("row_id") == row_id:
                return dict(row.get(cell_key) or {})
    return {}


def _comparison_cell_display(comparison: Mapping[str, Any], section_id: str, row_id: str, cell_key: str) -> str:
    return str(_comparison_cell(comparison, section_id, row_id, cell_key).get("display") or "")


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


def _post_multipart(url: str, body: bytes, *, filename: str, fields: Mapping[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    boundary = "----wb-core-financial-smoke"
    parts = []
    for key, value in dict(fields or {}).items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode("utf-8") + body + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    payload = (
        b"".join(parts)
    )
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
