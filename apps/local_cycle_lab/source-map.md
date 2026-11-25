# Карта входов локального стенда и полного цикла

Сверено 25 ноября 2026 с кодом `origin/main` 31d1e0e4. Карта описывает
кодовую конфигурацию, не подтверждает выбранные настройки рабочего сервера.
Источник истины о группах — `packages/application/sheet_vitrina_v1_source_groups.py`;
о фактических клиентах — конструкторы `SheetVitrinaV1LivePlanBlock` и
`RegistryUploadHttpEntrypoint`, а не подпись группы в UI.

Обозначения покрытия: **HTTP** — штатный клиент читает синтетическое HTTP-тело;
**transport** — штатный Finance client с поданной функцией транспорта, без проверки
его URL/заголовков/реальной сериализации; **блок** — готовый legacy payload на
входе штатного application block; **локально** — штатная запись/чтение отдельной
базы; **нет** — данный путь не запускается. Ни один пример пока не сверялся
с реальным ответом сервера WB/1С; «сверен» ниже означает исполненный путь кода.

## Внешнее получение для витрины

Потребитель каждой строки — соответствующий application block и
`sheet_vitrina_v1_live_plan.py::_load_live_sources`. Scope — enabled CONFIG,
кроме оговорённого полного кабинета и специальных складских scope. Текущая дата
определяется штатным календарём Asia/Yekaterinburg, не календарём хоста.

| Вход / поставщик | Дата, область и обязательные поля | Страницы и критерий полноты | Имитация и проверенное покрытие |
| --- | --- | --- | --- |
| `seller_funnel_snapshot` / кабинет продавца → web bot → `GET /v1/seller-funnel/snapshot` | Бизнес-день; карточки. `date`, `count`, `items[].nm_id`, views/open/CTR, `source_fetched_at` | Получение браузером отдельно от чтения сохранённого HTTP snapshot. Нужны все запрошенные карточки, точная дата и post-close время для вчера | **блок**. Две даты, 120 просмотров / 24 открытия. Браузер и материализация web bot не проверены |
| `web_source_snapshot` / кабинет продавца → web bot → `GET /v1/search-analytics/snapshot` | Период; выдача всего кабинета с отбором CONFIG. `date_from/to`, items, views/CTR/orders/position, fetched_at | Полный сохранённый период и закрытое наблюдение; отсутствующие строки не являются нулём | **блок**. 120 просмотров, 2 заказа, позиция 6. `ensure_*` проверяет допустимость даты, не выполняет shell/browser |
| `sales_funnel_history` / WB seller-analytics API `/api/analytics/v3/sales-funnel/products/history` | День/интервал и nmIds. Дата, nmId, open/cart/orderCount/orderSum/buyoutCount/buyoutPercent | Штатные батчи SKU/дней, сбор всех ответов. Отдельный mature-buyout путь может использовать CSV `DETAIL_HISTORY_REPORT` (downloads create/status/file) | **блок**. Две дневные колонки + зрелое окно; 24 открытия, 2 заказа, 800 ₽. Ни JSON HTTP batching, ни CSV download не покрыты |
| `sf_period` / WB seller-analytics `/api/analytics/v3/sales-funnel/products` | Дневной selectedPeriod, nmIds, product.nmId, statistic.selected.localizationPercent | limit/offset; сопоставление карточек и периода | **HTTP**. 1 карточка/ответ, обе даты; localization 70%. Многостраничный ответ не покрыт |
| `card_rating` / WB seller-analytics `/api/analytics/v2/item-rating` | Наблюдение только сегодня; запрос последних 7 закрытых дней. nmId, feedbackRating.current, observed_at, request_period | Батчи 50 nmIds, limit/offset до короткой страницы; уникальный запрошенный nmId; повторная проверка бизнес-дня | **HTTP**. Рейтинг 4,6 → 4,8; 2 отдельных текущих сбора и штатный rollover. Разрешённая квота клиента сохранена |
| `prices_snapshot` / WB discounts-prices `/api/v2/list/goods/filter` | Только текущий snapshot. nmID, sizes[].price/discountedPrice; nmList | Батчи запроса клиента; покрытие всех nmIds; zero-fill не принимается как реальный снимок | **HTTP**. Цена 380 → 400 ₽; предыдущая дата из собственного accepted-current |
| `spp` / либо кабинет продавца discountOnSite, либо WB statistics `/api/v1/supplier/sales` | Режим `seller_portal_discount_on_site` — текущая карточка; `statistics_sales_avg` — среднее по продажам даты. nmId, spp_avg/count | `WB_CORE_SPP_SOURCE_MODE`; auto выбирает portal при существующем storage state. Область и временная возможность зависят от режима; проверяется покрытие nmIds | **блок**. SPP 10 → 20%. Ни browser goods extraction, ни statistics HTTP не проверены. Группа wb_api сама по себе не доказывает поставщика |
| `spp_proxy` / публичная карточка WB HTML + card.wb.ru fallback | Текущая карточка и destination. nmId, public_buyer_price и реальная seller discounted price | По карточке; успешный парсинг/anti-bot и пригодная пара цен; неполное покрытие сохраняется явно | **блок**. 342/380 и 320/400 → 10/20%. HTTP, HTML parser и destination не проверены |
| `wb_buyer_authenticated` / авторизованный профиль покупателя | Бизнес-день и immutable roster. run_id, nmId, wallet/normal price, measured/session timestamps, proof, destination/profile | Коллектор вне полного цикла; завершённый подтверждённый run, session proof и roster; отсутствие наблюдения не ноль | **локально**. `begin_run/append_observation/finish_run` и штатная projection, 320/400 ₽. Браузер/сессия не проверены |
| `ads_bids` / WB advert promotion/count + `/api/advert/v2/adverts` | Текущий каталог кампаний, nm settings, placements, bids_kopecks | Полный каталог по статусам, батчи adverts; карточки/ставки без выдуманного zero-fill | **HTTP**. 1 кампания 601, search bid 140 → 150 ₽; rollover |
| `ads_compact` / WB advert promotion/count + `/adv/v3/fullstats` | День; кампания/app/nmId. views/clicks/atbs/orders/sum/sum_price и иерархия дат | Батчи ID, проверка структуры/сумм/полученных кампаний. Текущий каталог не доказывает полный датированный roster: production partial policy сохраняется | **HTTP**. Обе даты; normal/zero/missing за вчера; суммы, DRR, пустые отношения, partial presentation и retry. `zero` — наблюдаемые нули, не полный ноль кабинета |
| `fin_report_daily` / WB finance `/api/finance/v1/sales-reports/detailed`, period=daily | Точная дата, **весь кабинет до проекции CONFIG**. rrdId, rrDate, nmId, operation identity, денежные поля FIN_FIELDS | rrdId pagination до 204; строгая report basis/projection, finite money и сохранение aggregate storage nmId=0 | **transport**. 2 строки (101/999), 2 страницы + 204 на день. Выручка 101=400, доставка=4, acquiring=3; хранение **2+9=11**. Штатные клиент, report basis и расчёт; не canonical daily ledger |
| `stocks` / исторический WB seller-analytics CSV `STOCK_HISTORY_DAILY_CSV` | Закрытый день; карточка/склад/регион. nmId, qty, точная дата, warehouse identities/completeness | create/status/file download + полный CSV/набор дат. Текущая дата в этом display-пути не поддерживается; aggregate не доказывает разрез складов | **блок**. Вчера aggregate 12; warehouse granularity неполна, региональные ячейки пустые; сегодня unavailable. CSV и worker не проверены |
| `onec_stocks` / 1С `/hs/soykasoft/stocks_wb` | account_id, marketplace WB, date/generated_at/currency; nmId, stage qty/unit_cost/total | При нескольких nmIds сначала account snapshot, затем per-SKU/fallback; exact date, account, stage mapping и покрытие. Нет стадий не всегда означает ноль | **блок**. Транзит 2 × 100 = 200 ₽; HTTP/auth/fallback и остальные стадии не проверены |
| `promo_by_price` / кабинет продавца, promo XLSX collector | Текущие кампании, дата, nmIds, скачать XLSX; campaign membership и entry price | Полный collector manifest, файлы/хеши и native archive/materializer; вчера — accepted snapshot по собственной политике | **collector_result**. Только пустой список кампаний через настоящий PromoLiveSourceBlock, нулевые promo count. Непустые кампании требуют XLSX и сейчас явно отклоняются |

Клиенты находятся в `packages/adapters/{seller_funnel_snapshot_block,
web_source_snapshot_block,sales_funnel_history_block,sf_period_block,card_rating,
prices_snapshot_block,spp_block,spp_proxy_block,ads_bids_block,ads_compact_block,
fin_report_daily_block,stocks_block,onec_stocks_block}.py`. Buyer:
`packages/application/wb_buyer_authenticated_observations.py`; promo:
`packages/application/promo_live_source.py`.

## Остальные внешние входы полного цикла

Столбец покрытия ниже относится к проверке источников на Mac. Подключение тех же входов к контрольному циклу на Linux описано в конце карты.

Потребитель — методы `_cycle_*` и `_handle_owned_warehouse_manual_sync_request`
в `packages/application/registry_upload_http_entrypoint.py`, не только витрина.

| Вход / поставщик | Дата, область и обязательные поля | Страницы и критерий полноты | Покрытие стенда |
| --- | --- | --- | --- |
| Canonical Finance daily/weekly / тот же WB finance API | Полный кабинет, daily closed days и собственная due-week; rrdId/отчёт/операции/деньги | До 204, raw pointers, full report proofs, outbox и projection revisions. Дневной и недельный отчёты имеют собственные состояния | **transport-проба** weekly (2 строки), daily только для витрины. `WbFinanceDailyBlock.tick`, `WbFinanceWeeklyBlock.sync_week`, SPP refresh, outbox/cost replay **не выполнены** |
| Content catalog / WB content `/content/v2/get/cards/list` | Весь кабинет; nmID, vendorCode, updatedAt, sizes.chrtID/skus | cursor updatedAt/nmID/limit до терминальной страницы, уникальные identity; для FBS до/после и стабильный active/retained-hidden scope | **HTTP-проба** 1 карточки/1 страницы. Content source реален; generation/сохранение/проверка стабильности FBS не выполнены |
| Seller FBS warehouse/offices/stocks / WB marketplace `/api/v3/warehouses`, `/offices`, `/stocks/{warehouse}` | Кабинет → seller warehouse ↔ facility; chrtId, amount, office metadata, снимок текущего бизнес-дня | Полный стабильный реестр/каталог до и после, все батчи chrtIds/склады, подтверждающий stock read. FBS generation должен быть новый и complete | **HTTP-пробы** 1 склада/офиса и размер 9001=7. `WbFbsWarehouseRegistry.collect` и canonical admission пока **нет** |
| FBW supplies / WB supplies API `/api/v1/supplies`, details/goods/package, warehouses | Поставки кабинета, supply/preorder IDs, даты/статус, nmId/qty, warehouse identity | limit/offset и все товары каждой относящейся поставки; revisions и сохранённые документы; stable scope для расчёта | **нет**. Владелец `WbSuppliesBlock.sync_functional_sources` и адаптер `wb_supplies.py` |
| Transit costs и справочники WB / supplies transit-tariffs, common tariffs | Due transit supplies; маршрут, сумма, дата/warehouse tariff identity | Все due поставки; собственные source receipts и materialization. Какие справочники вызываются, зависит от пути и сохранённого состояния | **нет**. `collect_all_due_transit_costs`, `WbSuppliesBlock`; это не storage 2+9 из daily |
| Официальный текущий WB stock функционального склада / seller-analytics warehouses inventory | Текущий день; **полный canonical catalog**, nmId, warehouse identity, qty | Все страницы/батчи и полный согласованный snapshot; новый source digest, дата и catalog fence. TTL reuse=0 в функциональном entrypoint | **нет**. `WarehouseFunctionalBlock.build_sync_plan`, `StocksBlock(HttpBackedStocksSource)`; не путать с историческим display-stock 12 |

FBS collector заказов (пятиминутный, orders/supplies по API) — отдельный контур,
не зависимость registry generation; нельзя добавлять его вызовы в объём полного
цикла без фактического исполнения. Аналогично buyer collector, autoanswers,
stock observers и самостоятельные расписания не запускаются этим стендом.

## Локальные сохранённые входы и внутренние вычисления

В этой таблице указано покрытие CLI источников и ручного интерфейса на Mac.

| Вход | Кто читает, дата/область и обязательные признаки | Полнота / имитация |
| --- | --- | --- |
| Registry bundle/config/metrics/formulas/sections | Runtime и live-plan; активная bundle_version, enabled CONFIG и определения метрик | **локально**: штатный `ingest_bundle` с fixture registry + 1 enabled SKU. Это не выгрузка актуального каталога бизнеса |
| `cost_price` / загруженный файл себестоимости | Live-plan и cost resolvers; SKU/group, effective dates, стоимость | Файл/approved current state, применимая дата и единицы. **нет**: стоимость отсутствует, зависимые ячейки остаются пустыми |
| `sku_action_events` / журнал ручных действий | Live-plan; карточка/день/действие, реальная сохранённая identity | **локальное чтение пустого журнала**, создание пользовательского действия не проверено |
| Calculation/proxy parameters, mature buyout | CalculationParametersBlock / ProxyV4ParametersBlock; подтверждённое окно/формула/метод/дата | Штатные defaults и частичный maturity capture работают. Нет подтверждённой полноты unit economics без остальных операндов |
| Temporal accepted/current/closed snapshots и closure states | Live-plan и ClosedBacklog; source/date/role, captured_at, exact payload proof/retry | **локально**: первый current capture 23-го, rollover 24-го, две даты. Неизвестный/failed payload не превращается в 0; повтор читает сохранённое состояние |
| Buyer roster/runs/observations | Authenticated buyer daily projection; immutable day/roster и session proof | **локально**: seed через доменный API, не через браузер. Это независимое сохранённое наблюдение, не live внешний запрос цикла |
| Документы поставщика, оплаты, поступления/перемещения/накладные расходы и файлы | Supplier/warehouse/FF ledgers, operator journals; document identity, quantity/value, business date/status/approval | **штатный UI и локальная БД** для ручного FF-расхода 123,45 ₽. Проверяются accepted-квитанция, повторное подтверждение того же запроса, перезапуск и архив после сброса. Проводка, распределение, остатки, файлы и остальные типы документов ещё не покрыты |
| FBS/FF opening quantities, valuation/cost layers и снапшоты бухгалтерии | Warehouse + fbs_snapshot_accounting + canonical cost resolver; полные qty/source versions/date | **нет**. Полученные probe 7 единиц не являются opening snapshot или ledger |
| `own_product_capital` / внутренние проекции функционального склада | OwnProductCapitalBlock; бизнес-день, warehouse version, этапы/qty/cost/evidence | **локальное чтение без функционального снимка**, полноценный капитал не подтверждён |
| Ready / History, receipts, registry/schema/source/material versions | CAS publisher, history worker, partner report; pins и exact date/bundle | Ready **реальная публикация и readback**. Owned History, 14-дневная история и экран/Excel партнёрского отчёта **не выполнены** |

## Что зависит от профиля и текущего состояния

`business-data-cycle-3h-v1` задаёт 9 этапов: closed_sources, api_sources,
finance_sources, fbs_generation, warehouse, daily_projection, closed_ready,
final_ready, rolling14. FBS собирается каждый цикл; 9 часов — допустимый возраст,
не период сбора. Raw owner intent не сужает full auto_daily; обязательный владелец
false блокирует запуск. Без selector действуют legacy таймеры. Штатный ручной
full-cycle использует те же этапы. У стенда **нет выбранного schedule profile**,
и запуск collect/derive/Ready нельзя назвать прохождением 9 этапов.

Закрытые долги/retry/backoff, current-only rollover, Finance due-week, warm caches,
изменение каталога и due transit enrichment меняют фактическое число обращений.
SPP source mode, buyer destination/profile и manual source settings меняют
поставщика/семантику конкретного входа. Production selector, raw intent, cookies,
tokens, opening ledgers и размеры кабинета здесь не читались.

## Измерение объёма

CLI возвращает per-source calls/pages/rows/payload_json_bytes/request_body_bytes,
границу каждой подмены, суммы по фазам, fixture_bytes и state_bytes. `setup` —
сохранённые buyer наблюдения; `bootstrap` — предыдущий текущий день;
`adapter_probes` — Content/FBS/weekly; `collect_and_publish` — текущий сбор/Ready.
Поданные напрямую строки не учитываются как реальные HTTP-страницы. Терминальный
204 Finance считается страницей; сериализованное пустое значение `[]` — 2 байта
по методу имитатора, не реальное HTTP-тело 204.

Фактические запросы/страницы/строки/байты и занятое место **полного production
цикла неизвестны**. Также неизвестны объём браузерного трафика, CSV/XLSX/ZIP,
число складских страниц и срок выполнения полной цепочки. Контрольные цифры
публикуются с результатом локального запуска, отдельно от предположений о сервере.

На Mac проверена локальная WBC под sandbox-exec. В тесте один составной этап
`sources_ready`; попытки, их входная БД и результаты сохраняются по поколениям.
Следующий этап — контрольный полный цикл и проверка распределения ручного
расхода. Повтор отдельных стадий потребует сохранённых входов и проверки их
зависимостей. Для owned History сохраняется требование Linux; этот worker
в приёмку интерфейса на Mac не входит.


## Контроль расхода 123,45 ₽ на Linux

| Вход или результат | Путь проверки |
| --- | --- |
| Финансы WB | Штатные daily и weekly читают синтетические ответы через транспорт Finance API. Сохраняются raw-строки, статусы и проекции. Предварительный дневной отчёт сохраняет свой статус. |
| Каталог, остатки, заказы, поставки | Штатные HTTP-клиенты читают локальные ответы. Один SKU 101, привязанный склад FBS с 7 единицами, склад WB с 12 единицами. Ответы поставок указывают пустые списки. |
| Начальное состояние | Исторические закупочные и складские записи синтетические. Функциональный склад, переход на пообъектный учёт FF и начальная книга FBS готовятся доменными операциями. |
| Ручной расход | Операторская форма в Chromium принимает один расход FBS. На одной и той же базе проверяются проводка, количество и капитал после двух самостоятельных запусков. |
| Витрина и история | Ручной цикл запускает девять штатных этапов. Сценарий читает опубликованный Ready, числовые ячейки операторской витрины и сохранённые ячейки текущей редакции History за бизнес-день расхода. |

[Запуск и файлы проверки](README.md#контроль-расхода-в-полном-цикле-на-linux).

Для склада и 14-дневной истории доступны [отдельные прогоны из сохранённых входов](README.md#повтор-отдельного-этапа). У каждой попытки собственная рабочая копия и квитанция.
