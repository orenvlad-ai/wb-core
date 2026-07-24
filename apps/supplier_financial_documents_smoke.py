"""Smoke-check supplier order financial document parser and API routes."""

from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import socket
import sys
import threading
from tempfile import TemporaryDirectory
from typing import Any, Mapping
from urllib import request as urllib_request
import zipfile
import zlib

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
from packages.contracts.supplier_financial_documents import FINANCIAL_DOCUMENT_PARSER_VERSION  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.supplier_customs_breakdown import DT_ANNEX_ITEMS_PARSER_VERSION  # noqa: E402
from packages.application.supplier_financial_documents import (  # noqa: E402
    StaticUsdRateProvider,
    SupplierFinancialDocumentsBlock,
    _enrich_customs_goods_items_from_annex_rows,
    _extract_customs_annex_rows_from_layout_pages,
    _statement_row_from_segment,
    _statement_reference_identity,
    apply_supplier_order_document_match,
    build_bank_fee_statement_import_preview,
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

QUOTE_2026_06_09_TEXT = """
Коммерческое предложение на транспортно-экспедиционные услуги по тарифу «Авто стандарт 25-30 дней»
Transitplus International Ltd
Наименование груза: СТЕКЛА ДЛЯ СМАРТФОНА
г. Москва 09.06.2026
Город отправки: Guangzhou (Гуанчжоу)
Пункт назначения: Москва
Сроки доставки: 25-30 дней
Вес брутто, кг. 4680,45
Вес нетто, кг: 4680,45
Объем, м3 21,54
Оценочная стоимость груза, долл.                            49333,93
1. Предварительный расчет стоимости:
№ Перечень услуг Общая стоимость
1 Стоимость доставки 9495
2 Таможенные платежи и сборы 17130
3 Экологический сбор 0
4 Брокерские услуги 350
5 Комиссия компании 0
6 Страховая ставка, % 412
ИТОГО: 27387 USD
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

CUSTOMS_ITEMS_TEXT = CUSTOMS_TEXT + """
ТОВАР № 1
Наименование товара: Exact Model Alpha
Количество: 10 ШТ
Штрихкод: 4600000000001
Код ТН ВЭД: 7020008000
Модель: ALPHA-1
ТОВАР № 2
Наименование товара: Exact Model Group
Количество: 5 ШТ
Единица: ШТ
"""

CUSTOMS_WITH_REFERENCED_OLD_DECLARATION_TEXT = """
ИМ 40 ЭД
1 1
321
CN 6220930.50
CNY 541962.50 11.4693 010 00
ИУ 1010-49240.00-643-0000000000
2010-622093.05-643-0000000000
5010-1505465.18-643-0000000000
2176798.23
04011/2 3 ОТ 02.03.2022 10720010/130226/5011959; СМ.ДОПОЛНЕНИЕ
РАЗРЕШЕН 030726 ЛНП 036
10228010/030726/5211187
ДЕКЛАРАЦИЯ НА ТОВАРЫ
04031/0 121 от 29.06.2026
09999/2 2 от 02.03.2022 10720010/130226/5011959
1 7020008000 С N
CN   6713.450 ОООО-ОО
4000 000 6042.160
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

BANK_CONTROL_MULTI_PAYMENT_COLUMNAR_TEXT = """
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
\f
                    2
                                        1
                                                            1
                                                            № п/п

                    6
                                        6
                                                            2
                                                            Дата операции

                    30.06.202
                                        11.06.202

                    2
                                        2
                                                            3
                                                            Направление (признак) платежа

                                                            4
                                                            Код вида операции
                    11100
                                        11100

                                                            5
                                                            код валюты
                    156
                                        156

                                                            6
                                                            сумма
                    59921.25
                                        345337.50

                                                            7
                                                            код валюты
                    156
                                        156

                                                            8
                                                            сумма
                    59921.25
                                        345337.50

                                                            9
                                                            Ожидаемые сроки репатриации
                    26
                                        27
                    31.07.20
                                        07.06.20

                                                            Раздел II. Сведения о платежах
Раздел III. Сведения о подтверждающих документах
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

VTB_RECLASSIFICATION_TEXT = """
ВЫПИСКА за период с 29.06.2026 по 24.07.2026
Счет 40802810012480001092 (Валюта 643, Российский рубль)
Владелец счета: Тест
Входящий остаток на 29.06.2026: 20000.00
Дата № ВО Контрагент Обороты, RUR Назначение
30.06.2026 37 01 7728486029 044525092 40702810470010357554 ЛОГИСТ 5000.00 0.00 Счёт на оплату №121 от 29 июня 2026 г.
ИТОГО за период с 29.06.2026 по 24.07.2026
ИСХОДЯЩИЙ ОСТАТОК: 15000.00
ФИЛИАЛ "ЦЕНТРАЛЬНЫЙ" БАНКА ВТБ (ПАО)
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
    "vtb-reclassification.pdf": VTB_RECLASSIFICATION_TEXT,
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
    _assert_parser_reclassification_staging()
    _assert_http_api_smoke()
    print("supplier_financial_documents_smoke: OK")


def _assert_vtb_statement_parser_and_preview() -> None:
    incidental_invoice_wording = parse_financial_document_text(
        """
        Платёжный реестр контрагента
        30.06.2026 операция 37: назначение платежа Счёт на оплату №121.
        Этот фрагмент не содержит заголовка счёта или структуры банковской выписки.
        """,
        filename="unknown-register.pdf",
    )
    if (
        incidental_invoice_wording.get("raw_parse", {})
        .get("classification", {})
        .get("status")
        != "needs_review"
    ):
        raise AssertionError(
            "incidental payment-purpose wording must not classify an invoice"
        )
    overlapping_row = (
        "30.06.2026 130623 02 7702070139 044525187 "
        "30101810700000000187 БАНК ВТБ 948.60 0.00 "
        "Комиссия за ВК по платежу №7 на сумму 59921.25 CNY."
    )
    overlap_a = _statement_row_from_segment(
        overlapping_row
        + " ИТОГО за период с 29.06.2026 по 24.07.2026 "
        + "ИСХОДЯЩИЙ ОСТАТОК: 10.00",
        index=1,
        account_currency="RUB",
        account_number="40802810012480001092",
        section_id="section-a",
    )
    overlap_b = _statement_row_from_segment(
        overlapping_row
        + " ИТОГО за период с 20.06.2026 по 30.07.2026 "
        + "ИСХОДЯЩИЙ ОСТАТОК: 20.00",
        index=99,
        account_currency="RUB",
        account_number="40802810012480001092",
        section_id="section-b",
    )
    if overlap_a["semantic_operation_id"] != overlap_b["semantic_operation_id"]:
        raise AssertionError(
            "overlapping statement page trailers changed semantic identity"
        )
    statement_text = """
                                                       ВЫПИСКА за период с 29.06.2026 по 24.07.2026
 Счет 40802156616580000008 (Валюта 156, Китайский юань)
Владелец счета: Тест
Входящий остаток на 29.06.2026 CNY: 100.00
Дата № ВО Контрагент Обороты, RUR Обороты, CNY Назначение
30.06.2026 7 01 7702070139 VTBRCNSHXXX 40807156200610034920 SUPPLIER 686841.34 0.00 59921.25 0.00 ADV PMT FOR GOODS CONTRACT FR-001/26
20.07.2026 11 01 7702070139 VTBRCNSHXXX 40807156200610034920 SUPPLIER 3925309.26 0.00 339553.75 0.00 ADV PMT FOR GOODS CONTRACT FR-001/26
ИТОГО за период с 29.06.2026 по 24.07.2026
ИСХОДЯЩИЙ ОСТАТОК: CNY: 1.00
                                                         ВЫПИСКА за период с 29.06.2026 по 24.07.2026
 Счет 40802810012480001092 (Валюта 643, Российский рубль)
Владелец счета: Тест
Входящий остаток на 29.06.2026: 20000.00
Дата № ВО Контрагент Обороты, RUR Назначение
30.06.2026 130623 02 7702070139 044525187 30101810700000000187 БАНК ВТБ 948.60 0.00 Комиссия за ВК по платежу №7 на сумму 59921.25 CNY.
30.06.2026 443906 02 7702070139 044525187 30101810700000000187 БАНК ВТБ 13668.11 0.00 Комиссия за перевод (SWIFT) №7 на сумму 59921.25 CNY.
20.07.2026 50149 02 7702070139 044525187 30101810700000000187 БАНК ВТБ 4788.83 0.00 Комиссия за ВК по платежу №11 на сумму 339553.75 CNY.
20.07.2026 244189 02 7702070139 044525187 30101810700000000187 БАНК ВТБ 20000.00 0.00 Комиссия за перевод (SWIFT) №11 на сумму 339553.75 CNY.
21.07.2026 244189 02 7702070139 044525187 30101810700000000187 БАНК ВТБ 58113.66 0.00 Комиссия за перевод (SWIFT) №11 на сумму 339553.75 CNY.
30.06.2026 37 01 7728486029 044525092 40702810470010357554 ЛОГИСТ 5000.00 0.00 Счёт на оплату №121 от 29 июня 2026 г.
ИТОГО за период с 29.06.2026 по 24.07.2026
ИСХОДЯЩИЙ ОСТАТОК: 1539258.96
ФИЛИАЛ "ЦЕНТРАЛЬНЫЙ" БАНКА ВТБ (ПАО)
"""
    parsed = parse_financial_document_text(
        statement_text, filename="VTB_BankStatement_some_accounts.pdf"
    )
    normalized = dict(parsed.get("normalized_parse") or {})
    classification = dict(parsed.get("raw_parse") or {}).get("classification") or {}
    if (
        normalized.get("document_type") != "bank_fee_statement"
        or classification.get("status") != "classified"
        or len(normalized.get("account_sections") or []) != 2
        or [item.get("account_currency") for item in normalized["account_sections"]]
        != ["CNY", "RUB"]
    ):
        raise AssertionError(
            f"mixed VTB statement classification/sections changed: {parsed}"
        )
    preview = build_bank_fee_statement_import_preview(
        normalized,
        shipment={"header": {"invoice_no": "26GN527"}},
        payment_documents=[
            {
                "document_id": "payment-7",
                "document_number": "7",
                "cny_amount": "59921.25",
                "payment_details": "",
            },
            {
                "document_id": "payment-11",
                "document_number": "11",
                "cny_amount": "339553.75",
                "payment_details": "",
            },
        ],
    )
    fees = list(preview.get("matched_fee_rows") or [])
    amounts = sorted(str(item.get("amount") or "") for item in fees)
    selected = sorted(
        str(item.get("amount") or "")
        for item in fees
        if item.get("selected_by_default")
    )
    review = sorted(
        str(item.get("amount") or "")
        for item in fees
        if item.get("operation_status") == "needs_review"
    )
    if (
        amounts != sorted(["948.60", "13668.11", "4788.83", "20000", "58113.66"])
        or selected != sorted(["948.60", "13668.11", "4788.83"])
        or review != sorted(["20000", "58113.66"])
        or preview.get("fee_totals_by_currency", {}).get("RUB") != "97519.20"
    ):
        raise AssertionError(f"VTB exact preview changed: {preview}")
    operation_ids = {
        str(item.get("semantic_operation_id") or "") for item in fees
    }
    overlap = build_bank_fee_statement_import_preview(
        normalized,
        shipment={"header": {"invoice_no": "26GN527"}},
        payment_documents=[
            {"document_id": "payment-7", "document_number": "7", "cny_amount": "59921.25"},
            {"document_id": "payment-11", "document_number": "11", "cny_amount": "339553.75"},
        ],
        existing_operation_ids=operation_ids,
    )
    if not overlap.get("matched_fee_rows") or any(
        item.get("operation_status") != "already_imported"
        for item in overlap["matched_fee_rows"]
    ):
        raise AssertionError(
            f"overlapping statement operations must dedupe semantically: {overlap}"
        )
    conflict_row = next(
        item
        for item in fees
        if str(item.get("amount") or "") == "948.60"
    )
    conflict = build_bank_fee_statement_import_preview(
        normalized,
        shipment={"header": {"invoice_no": "26GN527"}},
        payment_documents=[
            {"document_id": "payment-7", "document_number": "7", "cny_amount": "59921.25"},
            {"document_id": "payment-11", "document_number": "11", "cny_amount": "339553.75"},
        ],
        existing_operation_index={
            _statement_reference_identity(conflict_row): {
                "bankop_existing_different_semantics"
            }
        },
    )
    conflicting = next(
        item
        for item in conflict["matched_fee_rows"]
        if str(item.get("amount") or "") == "948.60"
    )
    if (
        conflicting.get("operation_status") != "conflict"
        or conflicting.get("import_allowed") is not False
        or conflicting.get("selected_by_default") is not False
    ):
        raise AssertionError(f"semantic reference conflict must fail closed: {conflicting}")
    same_amount_different_dates = [
        item
        for item in normalized.get("fee_rows") or []
        if str(item.get("bank_document_number") or "") == "244189"
    ]
    if len({item.get("semantic_operation_id") for item in same_amount_different_dates}) != 2:
        raise AssertionError("same reference on different operation dates collapsed")
    with TemporaryDirectory(prefix="vtb-confirm-idempotency-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(tmp) / "runtime"
        )
        _seed_supplier_order(runtime)
        document_id = "fdoc_vtb_confirm_smoke"
        runtime.save_supplier_financial_document(
            document={
                "document_id": document_id,
                "supplier_order_id": "sup_financial",
                "document_type": "bank_fee_statement",
                "original_filename": "VTB_BankStatement_some_accounts.pdf",
                "stored_file_path": "",
                "file_content_type": "application/pdf",
                "file_sha256": "a" * 64,
                "uploaded_at": "2026-07-24T08:00:00Z",
                "updated_at": "2026-07-24T08:00:00Z",
                "parse_status": "parsed",
                "vendor": "ВТБ",
                "currency": "MIXED",
                "normalized_parse": {
                    **normalized,
                    "statement_import": {
                        **preview,
                        "import_status": "preview_pending",
                        "confirmed_at": "",
                    },
                },
                "parser_version": "supplier_vtb_bank_fee_statement_parser_v2",
            },
            expense_lines=[],
        )
        block = SupplierFinancialDocumentsBlock(
            runtime=runtime,
            timestamp_factory=lambda: "2026-07-24T09:00:00Z",
        )
        selected_ids = [
            str(item.get("semantic_operation_id") or "")
            for item in fees
            if item.get("selected_by_default")
        ]
        confirmed = block.confirm_bank_fee_statement_import(
            "sup_financial",
            document_id,
            selected_operation_ids=selected_ids,
        )
        repeated = block.confirm_bank_fee_statement_import(
            "sup_financial",
            document_id,
            selected_operation_ids=selected_ids,
        )
        stored = runtime.load_supplier_financial_document(
            supplier_order_id="sup_financial",
            document_id=document_id,
        ) or {}
        stored_preview = dict(
            dict(stored.get("normalized_parse") or {}).get(
                "statement_import"
            )
            or {}
        )
        imported_amounts = sorted(
            str(item.get("amount") or "")
            for item in stored.get("expense_lines") or []
        )
        review_after = sorted(
            str(item.get("amount") or "")
            for item in stored_preview.get("matched_fee_rows") or []
            if item.get("operation_status") == "needs_review"
        )
        if (
            imported_amounts != sorted(["948.6", "13668.11", "4788.83"])
            or review_after != sorted(["20000", "58113.66"])
            or not repeated.get("idempotent")
            or repeated.get("cny_fee_rows_for_ledger")
            or len(confirmed.get("expense_lines") or []) != 3
        ):
            raise AssertionError(
                "selected confirm must import only exact defaults and repeat as no-op: "
                + repr(
                    {
                        "confirmed": confirmed,
                        "repeated": repeated,
                        "imported_amounts": imported_amounts,
                        "review_after": review_after,
                    }
                )
            )


def _assert_parser_smoke() -> None:
    _assert_vtb_statement_parser_and_preview()
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
        or customs.get("goods_item_count") != 28
        or customs.get("goods_items_parser_version") != "supplier_customs_goods_items_v2"
        or customs.get("goods_items", [{}])[0].get("quantity") != 380.16
        or customs.get("goods_items", [{}])[0].get("unit") != "кг"
        or customs.get("goods_items", [{}])[0].get("quantity_evidence") != "dt_box_38_net_weight_kg"
        or customs.get("goods_items", [{}, {}])[1].get("identifiers", {}).get("customs_code") != "7020008000"
    ):
        raise AssertionError(f"customs parser fields mismatch: {customs}")
    customs_items = parse_financial_document_text(CUSTOMS_ITEMS_TEXT, filename="customs-items.txt")["normalized_parse"]
    if (
        customs_items.get("declaration_number") != customs.get("declaration_number")
        or customs_items.get("total_customs_payments_rub") != customs.get("total_customs_payments_rub")
        or customs_items.get("goods_item_count") != 2
        or customs_items.get("goods_items", [{}])[0].get("barcode") != "4600000000001"
        or customs_items.get("goods_items", [{}])[0].get("quantity") != 10.0
        or customs_items.get("goods_items", [{}, {}])[1].get("source_name") != "Exact Model Group"
    ):
        raise AssertionError(f"customs item parser must extend, not replace, aggregate contract: {customs_items}")

    annex_header = (
        f"{'Гр.':<7}{'Наименование':<48}{'Производитель':<29}"
        f"{'Марка':<15}{'Модель':<15}{'Кол-во':<14}{'Артикул'}"
    )
    annex_rows = _extract_customs_annex_rows_from_layout_pages(
        [
            "\n".join(
                (
                    annex_header,
                    f"{'1':<7}{'Sanitized Alpha':<48}{'Safe Factory':<29}{'Safe':<15}{'A-1':<15}{'10 ШТ':<14}{'ART-1'}",
                    f"{'2':<7}{'Sanitized Beta':<48}{'Safe Factory':<29}{'Safe':<15}{'B-2':<15}{'5 ШТ':<14}{'ART-2'}",
                )
            )
        ]
    )
    if (
        len(annex_rows) != 2
        or annex_rows[0].get("source_name") != "Sanitized Alpha"
        or annex_rows[0].get("quantity") != 10.0
        or annex_rows[0].get("unit") != "ШТ"
        or annex_rows[1].get("source_model") != "B-2"
    ):
        raise AssertionError(f"customs box-31 annex layout parser changed: {annex_rows}")
    annex_enriched = _enrich_customs_goods_items_from_annex_rows(
        {
            "normalized_parse": {
                "document_type": "customs_declaration",
                "total_goods_count": 1,
                "customs_gross_weight_kg": 2.0,
                "customs_net_weight_kg": 1.5,
                "total_customs_payments_rub": 123.45,
                "goods_items": [
                    {
                        "position_number": "1",
                        "source_name": "",
                        "quantity": 1.5,
                        "unit": "кг",
                        "identifiers": {"customs_code": "7020008000"},
                    }
                ],
            },
            "raw_parse": {},
        },
        annex_rows,
    )
    enriched_item = annex_enriched.get("normalized_parse", {}).get("goods_items", [{}])[0]
    projected_annex = annex_enriched.get("normalized_parse", {}).get("annex_items", [])
    if (
        enriched_item.get("source_name") != ""
        or enriched_item.get("quantity") != 1.5
        or enriched_item.get("unit") != "кг"
        or enriched_item.get("identifiers") != {"customs_code": "7020008000"}
        or len(projected_annex) != 2
        or projected_annex[0].get("parent_position_number") != "1"
        or projected_annex[0].get("annex_row_number") != "1"
        or projected_annex[0].get("article") != "ART-1"
        or projected_annex[0].get("source_model") != "A-1"
        or projected_annex[0].get("quantity") != 10.0
        or projected_annex[0].get("unit") != "ШТ"
        or projected_annex[0].get("identifiers", {}).get("customs_code") != "7020008000"
        or annex_enriched.get("normalized_parse", {}).get("annex_item_count") != 2
        or annex_enriched.get("normalized_parse", {}).get("annex_quantity_total") != 15.0
        or annex_enriched.get("normalized_parse", {}).get("annex_quantity_conserved") is not True
        or annex_enriched.get("normalized_parse", {}).get("annex_items_parser_version") != "supplier_customs_annex_items_v2"
        or annex_enriched.get("normalized_parse", {}).get("annex_parent_positions_complete") is not True
        or annex_enriched.get("normalized_parse", {}).get("total_goods_count") != 1
        or annex_enriched.get("normalized_parse", {}).get("customs_gross_weight_kg") != 2.0
        or annex_enriched.get("normalized_parse", {}).get("customs_net_weight_kg") != 1.5
        or annex_enriched.get("normalized_parse", {}).get("total_customs_payments_rub") != 123.45
        or annex_enriched.get("raw_parse", {}).get("customs_annex_row_count") != 2
    ):
        raise AssertionError(f"customs box-31 annex evidence was not projected deterministically: {annex_enriched}")

    customs_with_old_ref = parse_financial_document_text(
        CUSTOMS_WITH_REFERENCED_OLD_DECLARATION_TEXT,
        filename="GTD_10228010_030726_5211187.txt",
    )["normalized_parse"]
    if (
        customs_with_old_ref.get("document_type") != "customs_declaration"
        or customs_with_old_ref.get("declaration_number") != "10228010/030726/5211187"
        or customs_with_old_ref.get("document_date") != "2026-07-03"
        or customs_with_old_ref.get("declaration_date") != "2026-07-03"
    ):
        raise AssertionError(f"customs parser must prefer header declaration over old referenced DT: {customs_with_old_ref}")

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

    bank_control_columnar_payload = parse_financial_document_text(
        BANK_CONTROL_MULTI_PAYMENT_COLUMNAR_TEXT,
        filename="bank-control-multi-payment-columnar.txt",
    )
    bank_control_columnar = bank_control_columnar_payload["normalized_parse"]
    columnar_payment_operations = bank_control_columnar.get("payment_operations") or []
    if (
        len(columnar_payment_operations) != 2
        or columnar_payment_operations[0].get("row_index") != 1
        or columnar_payment_operations[0].get("operation_date") != "2026-06-11"
        or columnar_payment_operations[0].get("payment_amount") != 345337.5
        or columnar_payment_operations[0].get("expected_repatriation_date") != "2027-06-07"
        or columnar_payment_operations[1].get("row_index") != 2
        or columnar_payment_operations[1].get("operation_date") != "2026-06-30"
        or columnar_payment_operations[1].get("payment_amount") != 59921.25
        or columnar_payment_operations[1].get("expected_repatriation_date") != "2026-07-31"
        or bank_control_columnar.get("total_payment_operations_amount") != 405258.75
        or bank_control_columnar_payload.get("errors")
    ):
        raise AssertionError(f"columnar bank control parser fields mismatch: {bank_control_columnar_payload}")
    _assert_bank_control_saved_parse_refresh_smoke(bank_control_columnar)

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
    _assert_quote_cargo_value_currency_aliases_smoke()
    _assert_bad_quote_rate_guardrail_smoke()
    _assert_quote_parse_error_reason_smoke()
    _assert_registry_lead_time_rows_smoke()
    _assert_registry_production_lead_time_rows_smoke()
    _assert_registry_negative_customs_lead_time_smoke()
    _assert_registry_data_source_sections_smoke()
    _assert_approx_landed_cost_summary_smoke()
    _assert_incomplete_quote_summary_smoke()


def _assert_bank_control_saved_parse_refresh_smoke(bank_control: dict[str, Any]) -> None:
    block = SupplierFinancialDocumentsBlock.__new__(SupplierFinancialDocumentsBlock)
    current_document = {
        "document_type": "bank_control_statement",
        "parse_status": "parsed",
        "stored_file_path": "/tmp/vbc.pdf",
        "parser_version": FINANCIAL_DOCUMENT_PARSER_VERSION,
        "normalized_parse": bank_control,
    }
    if block._saved_document_needs_parse_refresh(current_document):  # noqa: SLF001
        raise AssertionError("current bank control parser version should not force refresh")
    stale_document = {
        **current_document,
        "parser_version": "supplier_financial_document_parser_v3",
        "normalized_parse": {
            **bank_control,
            "payment_operations": [],
        },
    }
    if not block._saved_document_needs_parse_refresh(stale_document):  # noqa: SLF001
        raise AssertionError("stale bank control parser_v3 should refresh to recover payment operations")
    stale_quote_missing_cargo = {
        "document_type": "logistics_quote",
        "parse_status": "parsed",
        "stored_file_path": "/tmp/quote.pdf",
        "parser_version": "supplier_financial_document_parser_v5",
        "normalized_parse": {"estimated_cargo_value_usd": None},
    }
    if not block._saved_document_needs_parse_refresh(stale_quote_missing_cargo):  # noqa: SLF001
        raise AssertionError("stale logistics quote without cargo USD should refresh after parser v6")
    stale_quote_with_cargo = {
        **stale_quote_missing_cargo,
        "normalized_parse": {"estimated_cargo_value_usd": 49333.93},
    }
    if block._saved_document_needs_parse_refresh(stale_quote_with_cargo):  # noqa: SLF001
        raise AssertionError("stale logistics quote with cargo USD should not refresh only because parser version changed")


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

    dated_without_date = parse_financial_document_text(
        BANK_TRANSFER_TEXT.replace(
            "CONTRACT 083/26 DD 13.05.2026",
            "ADVANCE PAYMENT FOR GOODS UNDER CONTRACT NO 082/26 DATED",
        ),
        filename="bank-transfer-contract-no-dated.txt",
    )
    if (
        dated_without_date.get("normalized_parse", {}).get("contract_number") != "082/26"
        or dated_without_date.get("normalized_parse", {}).get("contract_date")
    ):
        raise AssertionError(
            f"source without a contract date must preserve the missing source field: {dated_without_date}"
        )
    resolved = apply_supplier_order_document_match(
        {**dated_without_date, "parse_status": "needs_review"},
        {
            "contract_no": "082/26",
            "contract_date": "2026-04-04",
            "invoice_amount_total": 541962.5,
            "currency": "CNY",
        },
    )
    if (
        resolved.get("parse_status") != "parsed"
        or resolved.get("order_match_status") != "matched"
        or resolved.get("normalized_parse", {}).get("contract_date") != "2026-04-04"
        or resolved.get("normalized_parse", {}).get("contract_resolution", {}).get("source")
        != "canonical_invoice_contract_package"
    ):
        raise AssertionError(f"canonical linked contract fallback mismatch: {resolved}")
    if any("missing contract date" in warning for warning in resolved.get("warnings", [])):
        raise AssertionError(f"resolved contract date kept a stale warning: {resolved}")

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
    if "approx_landed_cost_per_unit_rub" in _registry_row_ids(registry, "cargo_value"):
        raise AssertionError(f"registry matrix must not expose approximate landed cost row: {registry}")


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


def _assert_quote_cargo_value_currency_aliases_smoke() -> None:
    original_line = "Оценочная стоимость груза, долл.                            49333,93"
    variants = [
        "Оценочная стоимость груза, долл. 49333,93",
        "Оценочная стоимость груза, долл 49333,93",
        "Оценочная стоимость груза, долларов 49333,93",
        "Оценочная стоимость груза, долл. США 49333,93",
        "Оценочная стоимость груза, долл. 49 333,93",
        "Оценочная стоимость груза, долл. 49333,93 USD",
    ]
    for variant in variants:
        payload = parse_financial_document_text(QUOTE_2026_06_09_TEXT.replace(original_line, variant), filename="quote-2026-06-09.txt")
        quote = payload["normalized_parse"]
        if quote.get("estimated_cargo_value_usd") != 49333.93:
            raise AssertionError(f"quote cargo USD alias must parse {variant!r}: {quote}")
        if quote.get("estimated_cargo_value_usd_source_status") != "parsed":
            raise AssertionError(f"quote cargo USD alias must expose parsed source status {variant!r}: {quote}")
        if quote.get("estimated_cargo_value_cny") is not None:
            raise AssertionError(f"quote cargo CNY must not be synthesized when raw КП has no CNY {variant!r}: {quote}")
        if quote.get("estimated_cargo_value_cny_source_status") != "missing":
            raise AssertionError(f"quote cargo CNY missing source status mismatch {variant!r}: {quote}")

    quote_payload = parse_financial_document_text(QUOTE_2026_06_09_TEXT, filename="quote-2026-06-09.txt")
    documents, lines = _summary_documents_and_lines_from_payloads([("quote-2026-06-09", quote_payload)])
    summary = build_financial_summary(documents, lines)
    quote = summary.get("quote") or {}
    quote_percent = summary.get("percent_of_value", {}).get("quote_cargo_value", {})
    if quote.get("estimated_cargo_value_usd") != 49333.93:
        raise AssertionError(f"26GN462-like quote cargo USD must reach summary: {summary}")
    if quote_payload["normalized_parse"].get("estimated_cargo_value_cny") is not None:
        raise AssertionError(f"26GN462-like quote cargo CNY must stay missing: {quote_payload}")
    if (
        not _approx(quote_percent.get("logistics_pct"), 20.79, tolerance=0.01)
        or not _approx(quote_percent.get("customs_pct"), 34.72, tolerance=0.01)
        or not _approx(quote_percent.get("delivery_customs_pct"), 55.51, tolerance=0.01)
    ):
        raise AssertionError(f"26GN462-like quote percent metrics mismatch: {quote_percent}")
    registry = build_supplier_shipment_registry(
        [
            {
                "shipment_id": "26GN462",
                "header": {"shipment_id": "26GN462", "product_qty_total": 55250},
                "lines": [],
                "documents": documents,
                "expense_lines": lines,
                "summary": summary,
            }
        ]
    )
    if _registry_cell_display(registry, "quote_logistics", "quote_cargo_usd", "26GN462") != "49 333.93 USD":
        raise AssertionError(f"26GN462-like quote cargo USD must render from КП: {registry}")
    cny_cell = _registry_cell(registry, "quote_logistics", "quote_cargo_cny", "26GN462")
    if cny_cell.get("display") != "нет в КП" or "CNY/юанях" not in str(cny_cell.get("note") or ""):
        raise AssertionError(f"26GN462-like missing quote cargo CNY must explain raw absence: {cny_cell}")
    if _registry_cell_display(registry, "quote_normalized", "quote_logistics_pct", "26GN462") != "20.79%":
        raise AssertionError(f"26GN462-like quote logistics percent must render: {registry}")
    usd_per_kg = {
        "quote_logistics_usd_per_quote_kg": "2.19 USD",
        "quote_customs_usd_per_quote_kg": "3.66 USD",
        "quote_total_usd_per_quote_kg": "5.85 USD",
    }
    for row_id, expected_display in usd_per_kg.items():
        if _registry_cell_display(registry, "quote_normalized", row_id, "26GN462") != expected_display:
            raise AssertionError(f"26GN462-like USD/kg row {row_id} must stay based on КП USD and КП weight: {registry}")


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
    quote_total_rub_cell = _registry_cell(registry, "quote_logistics", "quote_total_rub", "bad_rate")
    if quote_total_rub_cell.get("display") != "курс не подтверждён":
        raise AssertionError(f"bad-rate registry row quote_total_rub must render known rate guard status: {quote_total_rub_cell}")
    if quote_total_rub_cell.get("status") != "warning" or quote_total_rub_cell.get("source_status") != "quote_rate_unavailable":
        raise AssertionError(f"bad-rate registry row quote_total_rub must carry warning quote_rate_unavailable metadata: {quote_total_rub_cell}")
    if "sanity-check" not in str(quote_total_rub_cell.get("note") or ""):
        raise AssertionError(f"bad-rate registry must explain hidden quote RUB total: {registry}")
    usd_per_kg_expectations = {
        "quote_logistics_usd_per_quote_kg": "2.02 USD",
        "quote_customs_usd_per_quote_kg": "4.05 USD",
        "quote_total_usd_per_quote_kg": "6.07 USD",
    }
    for row_id, expected_display in usd_per_kg_expectations.items():
        cell = _registry_cell(registry, "quote_normalized", row_id, "bad_rate")
        if cell.get("display") != expected_display:
            raise AssertionError(f"bad-rate registry row {row_id} must calculate USD/кг without RUB rate: {cell}")
        if cell.get("status") in {"partial", "warning"} or cell.get("quality") == "partial_expenses":
            raise AssertionError(f"pure USD quote metric {row_id} must not inherit expenses/rate warning state: {cell}")
    for row_id in (
        "quote_total_rub_per_unit",
        "quote_logistics_rub_per_quote_kg",
        "quote_customs_rub_per_quote_kg",
        "quote_total_rub_per_quote_kg",
    ):
        cell = _registry_cell(registry, "quote_normalized", row_id, "bad_rate")
        if cell.get("display") != "ждём счета":
            raise AssertionError(f"bad-rate registry row {row_id} must render waiting-for-invoices status: {cell}")
        if cell.get("status") != "warning" or cell.get("source_status") != "quote_rate_unavailable":
            raise AssertionError(f"bad-rate registry row {row_id} must carry warning quote_rate_unavailable metadata: {cell}")
        note = str(cell.get("note") or "")
        if "sanity-check" not in note or "после загрузки всех счетов логиста" not in note:
            raise AssertionError(f"bad-rate registry row {row_id} must explain hidden quote RUB metric and invoice wait: {cell}")

    missing_registry = build_supplier_shipment_registry(
        [
            {
                "shipment_id": "missing_quote",
                "header": {"shipment_id": "missing_quote", "product_qty_total": 80250},
                "lines": [],
                "documents": [],
                "expense_lines": [],
                "summary": build_financial_summary([], [], shipment={"header": {"shipment_id": "missing_quote", "product_qty_total": 80250}, "lines": []}),
            }
        ]
    )
    missing_quote_cell = _registry_cell(missing_registry, "quote_normalized", "quote_total_rub_per_unit", "missing_quote")
    if missing_quote_cell.get("display") != "нет КП" or missing_quote_cell.get("source_status") != "quote_document_missing":
        raise AssertionError(f"true missing КП data must explain missing КП, not wait for invoices: {missing_quote_cell}")


def _assert_quote_parse_error_reason_smoke() -> None:
    documents = [
        {
            "document_id": "quote",
            "document_type": "logistics_quote",
            "parse_status": "parsed",
            "normalized_parse": {
                "document_type": "logistics_quote",
                "gross_weight_kg": 100.0,
                "estimated_cargo_value_usd": None,
                "estimated_cargo_value_usd_source_status": "parse_error",
                "quote_required_amounts_complete": True,
            },
            "total_amount": 30.0,
            "cbr_usd_rate_value": 78.0,
        }
    ]
    lines = [
        {
            "line_id": "delivery",
            "financial_document_id": "quote",
            "category": "delivery_cost",
            "currency": "USD",
            "amount": 10.0,
            "included_in_logistics_efficiency": True,
        },
        {
            "line_id": "customs",
            "financial_document_id": "quote",
            "category": "customs_payments_and_fees",
            "currency": "USD",
            "amount": 20.0,
            "included_in_logistics_efficiency": False,
            "included_in_customs_total": True,
        },
    ]
    summary = build_financial_summary(documents, lines)
    registry = build_supplier_shipment_registry(
        [
            {
                "shipment_id": "quote_parse_error",
                "header": {"shipment_id": "quote_parse_error"},
                "lines": [],
                "documents": documents,
                "expense_lines": lines,
                "summary": summary,
            }
        ]
    )
    cargo_cell = _registry_cell(registry, "quote_logistics", "quote_cargo_usd", "quote_parse_error")
    if cargo_cell.get("display") != "ошибка парсинга КП" or cargo_cell.get("status") != "warning":
        raise AssertionError(f"parser failure reason must render as warning status: {cargo_cell}")
    percent_cell = _registry_cell(registry, "quote_normalized", "quote_logistics_pct", "quote_parse_error")
    if percent_cell.get("display") != "нет стоимости груза в КП" or percent_cell.get("status") != "warning":
        raise AssertionError(f"dependent quote percent must explain missing cargo denominator: {percent_cell}")


def _assert_registry_data_source_sections_smoke() -> None:
    documents = [
        {
            "document_id": "packing",
            "document_type": "packing_list",
            "parse_status": "parsed",
            "normalized_parse": {
                "total_quantity": 100.0,
                "total_gross_weight_kg": 50.0,
                "total_volume_m3": 0.25,
            },
        },
        {
            "document_id": "quote",
            "document_type": "logistics_quote",
            "parse_status": "parsed",
            "normalized_parse": {
                "gross_weight_kg": 999.0,
                "volume_m3": 9.99,
                "estimated_cargo_value_usd": 1000.0,
                "estimated_cargo_value_cny": 7000.0,
                "delivery_days_min": 25,
                "delivery_days_max": 30,
            },
            "total_amount": 300.0,
            "cbr_usd_rate_value": 90.0,
        },
        {
            "document_id": "customs",
            "document_type": "customs_declaration",
            "parse_status": "parsed",
            "normalized_parse": {
                "gross_weight_kg": 40.0,
                "total_customs_value_rub": 9000.0,
            },
        },
    ]
    shipment = {
        "header": {
            "shipment_id": "source_sections",
            "product_qty_total": 200.0,
            "invoice_amount_total": 1000.0,
            "currency": "CNY",
            "cny_payment_currency_rub_cost": "13000",
            "cny_ledger_effective_rate": "13.0",
        },
        "lines": [],
    }
    summary = build_financial_summary(documents, [], shipment=shipment)
    registry = build_supplier_shipment_registry(
        [
            {
                "shipment_id": "source_sections",
                "header": shipment["header"],
                "lines": [],
                "documents": documents,
                "expense_lines": [],
                "summary": summary,
            }
        ]
    )
    if _registry_cell_display(registry, "cargo_physics", "packing_list_units", "source_sections") != "100":
        raise AssertionError(f"physics quantity must come from packing list, not invoice quantity: {registry}")
    if _registry_cell_display(registry, "cargo_physics", "packing_list_weight", "source_sections") != "50.00 кг":
        raise AssertionError(f"physics weight must come from packing list: {registry}")
    if _registry_cell_display(registry, "cargo_physics", "packing_list_volume", "source_sections") != "0.25 м³":
        raise AssertionError(f"physics volume must come from packing list: {registry}")
    if _registry_cell_display(registry, "cargo_physics", "packing_list_density", "source_sections") != "200.00 кг/м³":
        raise AssertionError(f"packing-list density mismatch: {registry}")
    if _registry_cell_display(registry, "cargo_physics", "units_per_customs_kg", "source_sections") != "2.50":
        raise AssertionError(f"DT units/kg must use packing-list quantity over DT weight: {registry}")
    if _registry_cell_display(registry, "cargo_value", "invoice_goods_value_rub_per_unit", "source_sections") != "130.00 ₽":
        raise AssertionError(f"invoice unit goods value must use matched CNY payment over packing/invoice qty: {registry}")
    if _registry_cell_display(registry, "cargo_value", "customs_value_rub", "source_sections") != "9 000.00 ₽":
        raise AssertionError(f"customs value must remain only a comparison reference: {registry}")
    if _registry_cell_display(registry, "fact_expenses", "expenses_completeness_status", "source_sections") != "Расходы не учтены полностью":
        raise AssertionError(f"expenses completeness default mismatch: {registry}")
    exact_cell = _registry_cell(registry, "cargo_value", "exact_landed_cost_per_unit_rub", "source_sections")
    if exact_cell.get("status") != "provisional":
        raise AssertionError(f"exact cost must be provisional until canonical certification: {exact_cell}")
    complete_header = {**shipment["header"], "shipment_id": "source_sections_done", "expenses_complete": True}
    complete_registry = build_supplier_shipment_registry(
        [
            {
                "shipment_id": "source_sections_done",
                "header": complete_header,
                "lines": [],
                "documents": documents,
                "expense_lines": [],
                "summary": {
                    **build_financial_summary(documents, [], shipment={"header": complete_header, "lines": []}),
                    "per_unit": {
                        **build_financial_summary(
                            documents,
                            [],
                            shipment={"header": complete_header, "lines": []},
                        ).get("per_unit", {}),
                        "exact_cost_status": "certified",
                    },
                },
            }
        ]
    )
    if _registry_cell(complete_registry, "cargo_value", "exact_landed_cost_per_unit_rub", "source_sections_done").get("status") != "certified":
        raise AssertionError(f"exact cost must be green only after canonical certification: {complete_registry}")
    _assert_registry_normalized_quality_smoke()


def _assert_registry_normalized_quality_smoke() -> None:
    quote_payload = parse_financial_document_text(QUOTE_TEXT, filename="quote.txt")
    documents, lines = _summary_fixture_documents_and_lines(quote_payload, include_customs=True)
    incomplete_shipment = {
        "header": {
            "shipment_id": "normalized_incomplete",
            "product_qty_total": 116250,
            "expenses_complete": False,
        },
        "lines": [],
    }
    complete_shipment = {
        "header": {
            "shipment_id": "normalized_complete",
            "product_qty_total": 116250,
            "expenses_complete": True,
        },
        "lines": [],
    }
    registry = build_supplier_shipment_registry(
        [
            {
                "shipment_id": "normalized_incomplete",
                "header": incomplete_shipment["header"],
                "lines": [],
                "documents": documents,
                "expense_lines": lines,
                "summary": build_financial_summary(documents, lines, shipment=incomplete_shipment),
            },
            {
                "shipment_id": "normalized_complete",
                "header": complete_shipment["header"],
                "lines": [],
                "documents": documents,
                "expense_lines": lines,
                "summary": build_financial_summary(documents, lines, shipment=complete_shipment),
            },
        ]
    )
    section_ids = [section.get("section_id") for section in registry.get("sections", [])]
    expected_sections = [
        "passport",
        "quote_logistics",
        "quote_normalized",
        "lead_times",
        "cargo_physics",
        "cargo_value",
        "fact_expenses",
        "fact_normalized",
        "documents",
    ]
    if section_ids != expected_sections:
        raise AssertionError(f"normalized registry section order mismatch: expected {expected_sections}, got {section_ids}")
    logistics_dependent = [
        ("quote_normalized", "fact_logistics_per_quote_kg"),
        ("quote_normalized", "fact_total_per_quote_kg"),
        ("fact_normalized", "fact_logistics_per_dt_kg"),
        ("fact_normalized", "fact_total_per_dt_kg"),
        ("fact_normalized", "fact_logistics_pct"),
        ("fact_normalized", "fact_total_pct"),
    ]
    for section_id, row_id in logistics_dependent:
        incomplete_cell = _registry_cell(registry, section_id, row_id, "normalized_incomplete")
        complete_cell = _registry_cell(registry, section_id, row_id, "normalized_complete")
        if incomplete_cell.get("status") != "partial" or incomplete_cell.get("quality") != "partial_expenses":
            raise AssertionError(f"{section_id}/{row_id} must be partial while expenses are incomplete: {incomplete_cell}")
        if complete_cell.get("status") == "partial" or complete_cell.get("quality") == "partial_expenses":
            raise AssertionError(f"{section_id}/{row_id} must return to normal color when expenses are complete: {complete_cell}")
    for section_id, row_id in [
        ("quote_normalized", "quote_logistics_pct"),
        ("quote_normalized", "quote_customs_pct"),
        ("quote_normalized", "quote_logistics_usd_per_quote_kg"),
        ("quote_normalized", "quote_customs_usd_per_quote_kg"),
        ("quote_normalized", "quote_total_usd_per_quote_kg"),
        ("quote_normalized", "fact_customs_per_quote_kg"),
        ("fact_normalized", "fact_customs_per_dt_kg"),
        ("fact_normalized", "fact_customs_without_vat_pct"),
        ("fact_normalized", "fact_customs_with_vat_pct"),
    ]:
        cell = _registry_cell(registry, section_id, row_id, "normalized_incomplete")
        if cell.get("status") == "partial" or cell.get("quality") == "partial_expenses":
            raise AssertionError(f"{section_id}/{row_id} must not become yellow from incomplete logistics expenses: {cell}")


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
    if "Срок до ДТ / таможни" not in labels:
        raise AssertionError(f"registry lead-times must expose Срок до ДТ: {labels}")
    if "Плановый срок производства" not in labels or "Фактический срок производства" not in labels:
        raise AssertionError(f"registry lead-times must expose production duration rows: {labels}")
    if "Фактический срок доставки" not in labels:
        raise AssertionError(f"registry lead-times must expose actual delivery days: {labels}")
    forbidden = " ".join(labels).lower()
    if "отклонение срока" in forbidden:
        raise AssertionError(f"registry lead-times must not expose misleading rows: {labels}")
    if _registry_cell_display(registry, "lead_times", "actual_delivery_days", "missing_dates") != "—":
        raise AssertionError(f"missing fact shipment dates must render actual delivery days as unavailable: {registry}")
    if _registry_cell_display(registry, "lead_times", "days_to_customs_declaration", "missing_dates") != "—":
        raise AssertionError(f"missing lead-time dates must render as unavailable: {registry}")
    if "approx_landed_cost_per_unit_rub" in _registry_row_ids(registry, "cargo_value"):
        raise AssertionError(f"registry cargo value must not expose approximate landed cost: {registry}")
    if (
        _registry_cell_display(registry, "lead_times", "shipment_date", "invalid_fact_dates") != "—"
        or _registry_cell_display(registry, "lead_times", "actual_shipment_date", "invalid_fact_dates") != "—"
        or _registry_cell_display(registry, "lead_times", "actual_ff_acceptance_date", "invalid_fact_dates") != "—"
        or _registry_cell_display(registry, "lead_times", "actual_delivery_days", "invalid_fact_dates") != "—"
    ):
        raise AssertionError(f"invalid registry dates must render as unavailable: {registry}")
    if len(registry.get("warnings", [])) < 3:
        raise AssertionError(f"invalid registry dates must surface warnings: {registry}")


def _assert_registry_production_lead_time_rows_smoke() -> None:
    registry = build_supplier_shipment_registry(
        [
            {
                "shipment_id": "production_invoice_start",
                "header": {
                    "shipment_id": "production_invoice_start",
                    "created_at": "2026-05-01T08:00:00Z",
                    "invoice_date": "2026-05-14",
                    "shipment_date": "2026-06-15",
                    "actual_shipment_date": "2026-07-25",
                },
                "lines": [],
                "documents": [],
                "expense_lines": [],
                "summary": {},
            },
            {
                "shipment_id": "production_created_fallback",
                "header": {
                    "shipment_id": "production_created_fallback",
                    "created_at": "2026-05-01T08:00:00Z",
                    "shipment_date": "2026-05-11",
                    "actual_shipment_date": "2026-05-15",
                },
                "lines": [],
                "documents": [],
                "expense_lines": [],
                "summary": {},
            },
            {
                "shipment_id": "production_missing",
                "header": {"shipment_id": "production_missing"},
                "lines": [],
                "documents": [],
                "expense_lines": [],
                "summary": {},
            },
            {
                "shipment_id": "production_negative",
                "header": {
                    "shipment_id": "production_negative",
                    "invoice_no": "PROD-NEG",
                    "invoice_date": "2026-06-20",
                    "shipment_date": "2026-06-15",
                    "actual_shipment_date": "2026-06-19",
                },
                "lines": [],
                "documents": [],
                "expense_lines": [],
                "summary": {},
            },
        ]
    )
    if _registry_cell_display(registry, "lead_times", "planned_production_days", "production_invoice_start") != "32 дн.":
        raise AssertionError(f"planned production must use invoice_date as start when available: {registry}")
    if _registry_cell_display(registry, "lead_times", "actual_production_days", "production_invoice_start") != "72 дн.":
        raise AssertionError(f"actual production must use invoice_date as start when available: {registry}")
    if _registry_cell_display(registry, "lead_times", "planned_production_days", "production_created_fallback") != "10 дн.":
        raise AssertionError(f"planned production must fall back to created_at start: {registry}")
    if _registry_cell_display(registry, "lead_times", "actual_production_days", "production_created_fallback") != "14 дн.":
        raise AssertionError(f"actual production must fall back to created_at start: {registry}")
    if _registry_cell_display(registry, "lead_times", "planned_production_days", "production_missing") != "—":
        raise AssertionError(f"missing production source dates must render blank: {registry}")
    negative_planned = _registry_cell(registry, "lead_times", "planned_production_days", "production_negative")
    negative_actual = _registry_cell(registry, "lead_times", "actual_production_days", "production_negative")
    if (
        negative_planned.get("display") != "—"
        or negative_planned.get("quality") != "suspicious_negative_duration"
        or negative_actual.get("display") != "—"
        or negative_actual.get("quality") != "suspicious_negative_duration"
    ):
        raise AssertionError(f"negative production durations must be warning blanks: {registry}")
    warning_text = " ".join(str(item) for item in registry.get("warnings", []))
    if "Плановый срок производства" not in warning_text or "Фактический срок производства" not in warning_text:
        raise AssertionError(f"negative production durations must surface registry warnings: {registry.get('warnings')}")


def _assert_registry_negative_customs_lead_time_smoke() -> None:
    fixed_customs_payload = parse_financial_document_text(
        CUSTOMS_WITH_REFERENCED_OLD_DECLARATION_TEXT,
        filename="GTD_10228010_030726_5211187.txt",
    )
    fixed_documents, fixed_lines = _summary_documents_and_lines_from_payloads([("customs", fixed_customs_payload)])
    fixed_shipment = {
        "header": {
            "shipment_id": "customs_header_date",
            "shipment_date": "2026-06-15",
            "invoice_date": "2026-05-14",
        },
        "lines": [],
    }
    fixed_registry = build_supplier_shipment_registry(
        [
            {
                "shipment_id": "customs_header_date",
                "header": fixed_shipment["header"],
                "lines": [],
                "documents": fixed_documents,
                "expense_lines": fixed_lines,
                "summary": build_financial_summary(fixed_documents, fixed_lines, shipment=fixed_shipment),
            }
        ]
    )
    if _registry_cell_display(fixed_registry, "lead_times", "days_to_customs_declaration", "customs_header_date") != "18 дн.":
        raise AssertionError(f"fixed customs parser must use 2026-07-03 header date for days-to-DT: {fixed_registry}")

    stale_customs_doc = {
        "document_id": "stale_customs",
        "document_type": "customs_declaration",
        "parse_status": "parsed",
        "document_date": "2026-02-13",
        "normalized_parse": {
            "document_type": "customs_declaration",
            "declaration_number": "10720010/130226/5011959",
            "document_date": "2026-02-13",
            "declaration_date": "2026-02-13",
        },
    }
    stale_registry = build_supplier_shipment_registry(
        [
            {
                "shipment_id": "customs_negative",
                "header": {
                    "shipment_id": "customs_negative",
                    "invoice_no": "26GN390",
                    "shipment_date": "2026-06-15",
                },
                "lines": [],
                "documents": [stale_customs_doc],
                "expense_lines": [],
                "summary": build_financial_summary([stale_customs_doc], [], shipment={"header": {"shipment_id": "customs_negative", "shipment_date": "2026-06-15"}, "lines": []}),
            }
        ]
    )
    negative_cell = _registry_cell(stale_registry, "lead_times", "days_to_customs_declaration", "customs_negative")
    if (
        negative_cell.get("display") != "—"
        or negative_cell.get("quality") != "suspicious_negative_duration"
        or "start=2026-06-15" not in str(negative_cell.get("note") or "")
    ):
        raise AssertionError(f"negative days-to-DT must be a warning blank, not a negative value: {stale_registry}")
    if not any("Срок до ДТ / таможни is negative" in str(warning) for warning in stale_registry.get("warnings", [])):
        raise AssertionError(f"negative days-to-DT must surface registry warning: {stale_registry.get('warnings')}")


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
        "quote-2026-06-09": 78.0,
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


def _assert_parser_reclassification_staging() -> None:
    with TemporaryDirectory(prefix="supplier-financial-reclassification-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        _seed_supplier_order(runtime)
        source_bytes = b"%PDF-1.4\n% VTB statement with invoice wording\n"
        archived_path = runtime_dir / "supplier_financial_documents" / "wrong.pdf"
        archived_path.parent.mkdir(parents=True, exist_ok=True)
        archived_path.write_bytes(source_bytes)
        archived_id = "fdoc_wrong_archived_bank_statement"
        runtime.save_supplier_financial_document(
            document={
                "document_id": archived_id,
                "supplier_order_id": "sup_financial",
                "document_type": "logistics_invoice",
                "original_filename": "vtb-reclassification.pdf",
                "stored_file_path": str(archived_path),
                "file_content_type": "application/pdf",
                "file_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "uploaded_at": "2026-06-19T07:00:00Z",
                "updated_at": "2026-06-19T07:00:00Z",
                "parse_status": "excluded",
                "document_number": "121",
                "currency": "RUB",
                "normalized_parse": {
                    "document_type": "logistics_invoice",
                    "invoice_number": "121",
                },
                "parser_version": "supplier_financial_document_parser_v7",
            },
            expense_lines=[],
        )
        block = SupplierFinancialDocumentsBlock(
            runtime=runtime,
            timestamp_factory=lambda: "2026-06-19T08:00:00Z",
            pdf_text_extractor=_fixture_text_extractor,
        )
        preview = block.preview_document_upload(
            "sup_financial",
            file_bytes=source_bytes,
            uploaded_filename="vtb-reclassification.pdf",
            uploaded_content_type="application/pdf",
        )
        if (
            preview.get("duplicate_action") != "parser_reclassification"
            or preview.get("document", {}).get("document_type")
            != "bank_fee_statement"
        ):
            raise AssertionError(
                f"parser correction must not restore stale classification: {preview}"
            )
        result = block.confirm_document_upload(
            "sup_financial",
            confirmation_token=str(preview["confirmation_token"]),
        )
        archived = runtime.load_supplier_financial_document(
            supplier_order_id="sup_financial",
            document_id=archived_id,
        )
        replacement = runtime.load_supplier_financial_document(
            supplier_order_id="sup_financial",
            document_id=str(result.get("document_id") or ""),
        )
        if (
            result.get("duplicate_action") != "parser_reclassification"
            or not result.get("preview_required")
            or replacement is None
            or replacement.get("document_type") != "bank_fee_statement"
            or str(replacement.get("document_id") or "") == archived_id
            or archived is None
            or archived.get("parse_status") != "excluded"
        ):
            raise AssertionError(
                "parser correction must stage a new bank preview and preserve archived audit: "
                + repr({"result": result, "archived": archived, "replacement": replacement})
            )
        repeat_preview = block.preview_document_upload(
            "sup_financial",
            file_bytes=source_bytes,
            uploaded_filename="vtb-reclassification.pdf",
            uploaded_content_type="application/pdf",
        )
        repeated = block.confirm_document_upload(
            "sup_financial",
            confirmation_token=str(repeat_preview["confirmation_token"]),
        )
        if (
            repeat_preview.get("duplicate_action") != "idempotent_active"
            or not repeated.get("idempotent")
            or repeated.get("document_id") != result.get("document_id")
            or not repeated.get("preview_required")
        ):
            raise AssertionError(
                f"re-uploaded staged bank statement must be an exact no-op: {repeated}"
            )


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
                preview_status, preview = _post_multipart(
                    collection_url,
                    ("%PDF-1.4\n% synthetic financial smoke " + filename + "\n").encode(),
                    filename=filename,
                )
                if (
                    preview_status != 200
                    or not preview.get("confirmation_token")
                    or preview.get("active_saved") is not False
                ):
                    raise AssertionError(
                        f"financial preview failed for {filename}: {preview_status} {preview}"
                    )
                status, payload = _post_json(
                    collection_url + "/confirm-upload",
                    {"confirmation_token": preview["confirmation_token"]},
                )
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
            expected_sections = [
                "passport",
                "quote_logistics",
                "quote_normalized",
                "lead_times",
                "cargo_physics",
                "cargo_value",
                "fact_expenses",
                "fact_normalized",
                "documents",
            ]
            if section_ids != expected_sections:
                raise AssertionError(f"shipment registry section order mismatch: expected {expected_sections}, got {section_ids}")
            if any(row_id.startswith("quote_") for row_id in _registry_row_ids(registry, "cargo_physics")):
                raise AssertionError(f"cargo physics must not expose quote/KP source rows: {registry}")
            if any(row_id.startswith("quote_") or row_id.startswith("approx_") for row_id in _registry_row_ids(registry, "cargo_value")):
                raise AssertionError(f"cargo value must not expose quote/KP or approximate rows: {registry}")
            lead_time_labels = _registry_row_labels(registry, "lead_times")
            if "Срок до ДТ / таможни" not in lead_time_labels:
                raise AssertionError(f"shipment registry lead-time row missing: {lead_time_labels}")
            if "Фактический срок доставки" not in lead_time_labels:
                raise AssertionError(f"shipment registry actual delivery row missing: {lead_time_labels}")
            forbidden_lead_time_labels = " ".join(lead_time_labels).lower()
            if "отклонение срока" in forbidden_lead_time_labels:
                raise AssertionError(f"shipment registry exposes misleading lead-time rows: {lead_time_labels}")
            if _registry_cell_display(registry, "lead_times", "actual_delivery_days", "sup_financial") != "17 дн.":
                raise AssertionError(f"registry actual delivery days mismatch: {registry}")
            if _registry_cell_display(registry, "lead_times", "days_to_customs_declaration", "sup_financial") != "8 дн.":
                raise AssertionError(f"registry days-to-customs-declaration mismatch: {registry}")
            if _registry_cell_display(registry, "quote_normalized", "quote_total_rub_per_unit", "sup_financial") != "35.17 ₽":
                raise AssertionError(f"registry quote ₽/шт mismatch: {registry}")
            if _registry_cell_display(registry, "fact_expenses", "fact_total_rub_per_unit", "sup_financial") != "35.34 ₽":
                raise AssertionError(f"registry fact ₽/шт mismatch: {registry}")
            if "approx_landed_cost_per_unit_rub" in _registry_row_ids(registry, "cargo_value"):
                raise AssertionError(f"registry must not expose approximate landed cost in cargo value: {registry}")
            if _registry_cell_display(registry, "cargo_physics", "customs_weight", "sup_financial") != "9 784.60 кг":
                raise AssertionError(f"registry customs weight mismatch: {registry}")
            if _registry_cell_display(registry, "fact_expenses", "expenses_completeness_status", "sup_financial") != "Расходы не учтены полностью":
                raise AssertionError(f"registry expenses completeness must default to incomplete: {registry}")
            completeness_status, completeness_payload = _patch_json(
                f"{base_url}{DEFAULT_SUPPLIER_SHIPMENTS_PATH}/sup_financial/expense-completeness",
                {"expenses_complete": True},
            )
            if completeness_status != 200 or completeness_payload.get("expenses_complete") is not True:
                raise AssertionError(f"expense completeness patch failed: {completeness_status} {completeness_payload}")
            patched_registry_status, patched_registry = _get_json(f"{base_url}{DEFAULT_SUPPLIER_SHIPMENT_REGISTRY_PATH}")
            if (
                patched_registry_status != 200
                or _registry_cell_display(patched_registry, "fact_expenses", "expenses_completeness_status", "sup_financial") != "Расходы учтены"
            ):
                raise AssertionError(f"patched expense completeness must persist into registry: {patched_registry_status} {patched_registry}")
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
            direct_delete_status, _ = _delete_json(f"{collection_url}/{quote_document_id}")
            if direct_delete_status != 409:
                raise AssertionError("direct financial DELETE must require confirmation")
            delete_preview_status, delete_preview = _post_json(
                f"{collection_url}/{quote_document_id}/delete-preview", {}
            )
            if delete_preview_status != 200 or not delete_preview.get("confirmation_token"):
                raise AssertionError(
                    f"financial delete preview failed: {delete_preview_status} {delete_preview}"
                )
            delete_status, delete_payload = _post_json(
                f"{collection_url}/{quote_document_id}/delete-confirm",
                {"confirmation_token": delete_preview["confirmation_token"]},
            )
            if delete_status != 200 or delete_payload.get("deleted") is not False or delete_payload.get("archived") is not True or delete_payload.get("file_deleted") is not False:
                raise AssertionError(f"financial archive failed: {delete_status} {delete_payload}")
            deleted_detail_status, deleted_detail = _get_json(f"{collection_url}/{quote_document_id}")
            if deleted_detail_status != 200 or deleted_detail.get("parse_status") != "excluded":
                raise AssertionError(f"archived financial detail must remain auditable: {deleted_detail_status} {deleted_detail}")
            deleted_list_status, after_delete = _get_json(collection_url)
            if (
                deleted_list_status != 200
                or len(after_delete.get("documents", [])) != 3
                or len(after_delete.get("archived_documents", [])) != 1
                or len(after_delete.get("expense_lines", [])) != 5
                or after_delete.get("summary", {}).get("quote", {}).get("logistics_usd") is not None
                or after_delete.get("summary", {}).get("quote_invoice_match", {}).get("implied_rate") is not None
            ):
                raise AssertionError(f"financial list after delete mismatch: {deleted_list_status} {after_delete}")
            preview_status, preview = _post_multipart(
                collection_url,
                b"%PDF-1.4\n% synthetic financial smoke quote.pdf\n",
                filename="quote.pdf",
            )
            if preview_status != 200 or preview.get("duplicate_action") != "restore_excluded":
                raise AssertionError(f"archived SHA must offer restore: {preview_status} {preview}")
            status, payload = _post_json(
                collection_url + "/confirm-upload",
                {"confirmation_token": preview["confirmation_token"]},
            )
            if status != 200 or payload.get("duplicate_action") != "restored_excluded":
                raise AssertionError(f"financial re-upload after archive failed: {status} {payload}")
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
            packing_preview_status, packing_preview = _post_multipart(
                collection_url,
                _packing_list_workbook_bytes(),
                filename="packing-list.xlsx",
            )
            packing_status, packing_payload = _post_json(
                collection_url + "/confirm-upload",
                {"confirmation_token": packing_preview.get("confirmation_token")},
            )
            if (
                packing_preview_status != 200
                or
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
                preview_status, preview = _post_multipart(
                    collection_url,
                    ("%PDF-1.4\n% synthetic bank smoke " + filename + "\n").encode(),
                    filename=filename,
                )
                status, payload = _post_json(
                    collection_url + "/confirm-upload",
                    {"confirmation_token": preview.get("confirmation_token")},
                )
                if preview_status != 200:
                    raise AssertionError(
                        f"bank document preview failed for {filename}: {preview_status} {preview}"
                    )
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
            def _unexpected_package_parse_refresh(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                raise AssertionError("package assembly must not refresh or persist parser state")

            entrypoint.supplier_financial_documents_block._refresh_saved_document_parses = (  # type: ignore[method-assign]
                _unexpected_package_parse_refresh
            )
            logistics_status, logistics_bytes, logistics_headers = _get_bytes(f"{documents_url}/logistics-package.zip")
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
            logistics_receipt = _package_receipt_header(logistics_headers)
            if (
                logistics_receipt.get("status") != "complete"
                or logistics_receipt.get("counts", {}).get("included") != len(logistics_manifest.get("included", []))
                or logistics_receipt.get("included") != logistics_manifest.get("included")
            ):
                raise AssertionError(f"logistics HTTP receipt must come from exact ZIP assembly: {logistics_receipt}")
            accounting_status, accounting_bytes, accounting_headers = _get_bytes(f"{documents_url}/accounting-package.zip")
            if accounting_status != 409:
                raise AssertionError(f"unreconciled accounting package must return controlled 409: {accounting_status}")
            accounting_blocked = json.loads(accounting_bytes.decode("utf-8"))
            if (
                accounting_blocked.get("contract_name") != "sheet_vitrina_v1_supplier_accounting_package_blocked"
                or accounting_blocked.get("status") != "blocked"
                or accounting_blocked.get("requires_review") is not True
                or not accounting_blocked.get("blocker_reasons")
                or accounting_bytes.startswith(b"PK")
                or "Content-Disposition" in accounting_headers
            ):
                raise AssertionError(f"accounting fail-closed contract mismatch: {accounting_blocked} {accounting_headers}")
            _make_http_accounting_fixture_ready(runtime)
            accounting_ok_status, accounting_ok_bytes, accounting_ok_headers = _get_bytes(
                f"{documents_url}/accounting-package.zip"
            )
            accounting_ok_receipt = _package_receipt_header(accounting_ok_headers)
            accounting_ok_manifest = _zip_manifest(accounting_ok_bytes)
            if (
                accounting_ok_status != 200
                or not accounting_ok_bytes.startswith(b"PK")
                or "attachment" not in accounting_ok_headers.get("Content-Disposition", "")
                or accounting_ok_receipt.get("status") != "complete"
                or accounting_ok_receipt.get("requires_review") is not False
                or accounting_ok_receipt.get("accounting_reconciliation", {}).get("package_ready") is not True
                or accounting_ok_receipt.get("accounting_reconciliation", {}).get("matched_count") != 1
                or accounting_ok_manifest.get("requires_review") is not False
            ):
                raise AssertionError(
                    "100% reconciled accounting HTTP response must be the only successful ZIP: "
                    f"{accounting_ok_status} {accounting_ok_receipt} {accounting_ok_manifest}"
                )
            all_status, all_bytes, _ = _get_bytes(f"{documents_url}/archive.zip")
            all_manifest = _zip_manifest(all_bytes)
            all_types = [item.get("document_type") for item in all_manifest.get("included", [])]
            if all_status != 200 or len(all_manifest.get("included", [])) != 9:
                raise AssertionError(f"all-documents package must contain active docs only: {all_status} {all_manifest}")
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


def _make_http_accounting_fixture_ready(runtime: RegistryUploadDbBackedRuntime) -> None:
    """Promote only the sanitized HTTP fixture to a fully proven accounting package."""

    shipment = runtime.load_supplier_shipment("sup_financial") or {}
    header = dict(shipment.get("header") or {})
    invoice_path = runtime.runtime_dir / str(header.get("source_file_path") or "")
    header.update(
        {
            "source_file_sha256": hashlib.sha256(invoice_path.read_bytes()).hexdigest(),
            "product_qty_total": 116250,
            "match_status": "all_matched",
            "warnings": [],
            "errors": [],
            "updated_at": "2026-06-19T08:00:00Z",
        }
    )
    runtime.save_supplier_shipment(
        header=header,
        lines=[
            {
                "line_id": "strict-http-product-1",
                "line_type": "product",
                "sort_order": 1,
                "source_no": "1",
                "barcode": "0000000000777",
                "product_type": "clean",
                "model_raw": "iPhone 13 Pro",
                "model_normalized": "iphone 13 pro",
                "internal_nm_id": 777,
                "internal_name": "Sanitized Clean iPhone 13 Pro",
                "qty": 116250,
                "match_status": "matched_by_barcode",
                "raw": {},
            }
        ],
    )
    runtime.save_nomenclature_item(
        {
            "item_id": "strict-http-nomenclature-777",
            "is_active": True,
            "is_hidden": False,
            "nm_id": 777,
            "barcode": "0000000000777",
            "barcodes": ["0000000000777"],
            "nomenclature_name": "Sanitized Clean iPhone 13 Pro",
            "product_type": "clean",
            "compatible_models_text": "iPhone 13 Pro",
            "compatible_model_keys": ["iphone_13_pro"],
            "created_at": "2026-06-19T08:00:00Z",
            "updated_at": "2026-06-19T08:00:00Z",
        }
    )
    customs = next(
        document
        for document in runtime.list_supplier_financial_documents("sup_financial")
        if document.get("document_type") == "customs_declaration"
    )
    customs["normalized_parse"] = {
        **dict(customs.get("normalized_parse") or {}),
        "goods_items": [
            {
                "position_number": "1",
                "source_name": "Sanitized canonical customs position",
                "quantity": 8806.18,
                "unit": "кг",
                "identifiers": {"customs_code": "7020008000"},
            }
        ],
        "goods_item_count": 1,
        "annex_items": [
            {
                "parent_position_number": "1",
                "annex_row_number": "1",
                "source_name": "Sanitized protective glass",
                "article": "13 Pro",
                "source_model": "iPhone 13 Pro",
                "quantity": 116250,
                "unit": "ШТ",
                "barcode": "",
                "identifiers": {
                    "article": "13 Pro",
                    "source_model": "iPhone 13 Pro",
                    "customs_code": "7020008000",
                },
            }
        ],
        "annex_item_count": 1,
        "annex_quantity_total": 116250,
        "annex_quantity_conserved": True,
        "annex_parent_position_count": 1,
        "annex_parent_positions_complete": True,
        "annex_items_parser_version": DT_ANNEX_ITEMS_PARSER_VERSION,
    }
    customs["parser_version"] = FINANCIAL_DOCUMENT_PARSER_VERSION
    runtime.save_supplier_financial_document(
        document=customs,
        expense_lines=list(customs.get("expense_lines") or []),
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


def _package_receipt_header(headers: Mapping[str, str]) -> dict[str, Any]:
    encoded = next(
        (str(value) for key, value in headers.items() if str(key).lower() == "x-wb-core-package-receipt"),
        "",
    )
    if not encoded:
        raise AssertionError(f"package response receipt header is missing: {headers}")
    encoding = next(
        (str(value) for key, value in headers.items() if str(key).lower() == "x-wb-core-package-receipt-encoding"),
        "",
    )
    if encoding != "deflate-base64url":
        raise AssertionError(f"package receipt must use bounded deflate encoding: {encoding!r}")
    padded = encoded + "=" * ((4 - len(encoded) % 4) % 4)
    return json.loads(zlib.decompress(base64.urlsafe_b64decode(padded)).decode("utf-8"))


def _registry_cell_display(registry: Mapping[str, Any], section_id: str, row_id: str, shipment_id: str) -> str:
    return str(_registry_cell(registry, section_id, row_id, shipment_id).get("display") or "")


def _registry_cell(registry: Mapping[str, Any], section_id: str, row_id: str, shipment_id: str) -> dict[str, Any]:
    for section in registry.get("sections", []):
        if section.get("section_id") != section_id:
            continue
        for row in section.get("rows", []):
            if row.get("row_id") == row_id:
                cell = row.get("cells", {}).get(shipment_id) or {}
                return dict(cell) if isinstance(cell, Mapping) else {}
    return {}


def _registry_row_labels(registry: Mapping[str, Any], section_id: str) -> list[str]:
    for section in registry.get("sections", []):
        if section.get("section_id") == section_id:
            return [str(row.get("label") or "") for row in section.get("rows", [])]
    return []


def _registry_row_ids(registry: Mapping[str, Any], section_id: str) -> list[str]:
    for section in registry.get("sections", []):
        if section.get("section_id") == section_id:
            return [str(row.get("row_id") or "") for row in section.get("rows", [])]
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


def _patch_json(url: str, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(dict(payload)).encode("utf-8"),
        method="PATCH",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib_request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib_request.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_json(url: str, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(dict(payload)).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
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
