# Минимальный контракт данных для первой рабочей версии новой таблицы

## 1. Назначение новой таблицы на первом этапе

На первом этапе новая таблица должна быть **thin read-side витриной**, а не новым вычислительным центром.

Она должна уметь:
- показывать состояние по `date + nmId` на основе уже перенесённых bounded-блоков `wb-core`;
- давать оператору один понятный слой чтения по SKU, web-source и official API данным;
- показывать freshness и статус каждого блока.

Она пока не должна:
- заменять `DATA` как полный исторический слой Google Sheets;
- переносить в себя `METRICS`, `FORMULAS`, `CONFIG` и `DAILY RUN`;
- брать на себя applied/report logic и операторские расчётные сценарии.

## 2. Минимальные блоки данных, которые должен отдавать `wb-core`

### 2.1 Тонкий SKU/display bundle

Минимально нужны:
- `nmId` как главный ключ;
- отображаемое имя SKU;
- при наличии: `vendor_code`, `group`;
- явный набор активных SKU для витрины.

Это не полный перенос `CONFIG`, а только минимальный каталог, без которого витрина не сможет собрать строки и фильтры.

### 2.2 Web-source read-side

Нужны два уже перенесённых блока:
- `web_source_snapshot_block` — search analytics snapshot;
- `seller_funnel_snapshot_block` — seller funnel daily snapshot.

### 2.3 Official API snapshot layer

Для первой рабочей витрины минимально нужны:
- `prices_snapshot_block`;
- `sf_period_block`;
- `spp_block`;
- `ads_bids_block`;
- `stocks_block`;
- `ads_compact_block`;
- `fin_report_daily_block`.

### 2.4 Historical fact layer

Нужен `sales_funnel_history_block` как нормализованный исторический слой вида:
- `date`;
- `nmId`;
- `metric`;
- `value`.

### 2.5 Служебный слой

Для каждого блока витрине нужны:
- `snapshot_date` или `date_from/date_to`;
- `kind/status` (`success`, `empty`, `not_found`, `incomplete`);
- freshness / last successful update;
- coverage requested `nmId`, если блок работает по набору SKU.

## 3. Что уже покрыто текущими перенесёнными модулями

Уже покрыт основной факт-слой, который нужен витрине для первого чтения:
- web-source read-side snapshots уже вынесены в отдельные contracts и подтверждены smoke-path;
- official API блоки уже покрывают цены, seller funnel period/history, рекламные данные, остатки и daily finance;
- по этим блокам уже есть артефакты, evidence и рабочие checkpoint'ы в `main`.

Отдельно важно:
- `promo_by_price_block` и `cogs_by_group_block` уже перенесены, но для **первой** витрины не являются обязательным минимумом, потому что тянут за собой table-side rule/config semantics, которые сейчас сознательно не разворачиваются.

## 4. Чего ещё не хватает для первой рабочей витрины

Реальные data gaps сейчас такие:
- нет тонкого канонического SKU/display bundle для активного ассортимента;
- нет одного явного table-facing bundle/projection контракта, который собирает блоки в одну витринную выдачу;
- нет единого слоя freshness/status по всем блокам;
- не зафиксированы правила джойна snapshot-блоков и history-блока в одну операторскую плоскость.

Именно это сейчас мешает запуску первой новой витрины сильнее, чем отсутствие новых расчётных модулей.

## 5. Что пока остаётся жить в Google Sheets

Пока в Google Sheets остаются:
- расчётные скрипты;
- applied/report logic;
- operator-level обработка;
- полная semantics `CONFIG`, `METRICS`, `FORMULAS`;
- `DAILY RUN`;
- table-side rule maintenance для ручных правил.

## 6. Что сознательно откладывается на поздний этап

Сознательно откладываются:
- перепроектирование `METRICS`;
- перепроектирование `DAILY RUN`;
- перенос расчётных supply/report скриптов на сервер;
- финальная orchestration-логика.

## 7. Следующий практический шаг

Следующий шаг: отдельно зафиксировать и перенести **тонкий SKU/display bundle** для витрины (`nmId`, display name, optional `group/vendor_code`, active set, freshness), не трогая полный перенос `CONFIG`.
