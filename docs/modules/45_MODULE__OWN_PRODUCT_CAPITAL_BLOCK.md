---
title: "Модуль: own_product_capital_block"
doc_id: "WB-CORE-MODULE-45-OWN-PRODUCT-CAPITAL-BLOCK"
doc_type: "module"
status: "active_event_projection_facade"
purpose: "Зафиксировать canonical event input и compatibility read projection товарного капитала внутри active functional engine шести складов."
scope: "Canonical source events/revisions, public SKU/TOTAL metric keys, Decimal aggregate semantics, functional warehouse quantities/capital и legacy pre-projection audit boundary."
source_basis:
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "docs/modules/36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "docs/modules/40_MODULE__OUR_WB_COST_MODEL_BLOCK.md"
  - "docs/modules/43_MODULE__FF_STOCK_LEDGER_BLOCK.md"
  - "docs/modules/48_MODULE__WAREHOUSE_STOCKS_BLOCK.md"
related_modules:
  - "packages/application/warehouse_functional.py"
  - "packages/application/own_product_capital.py"
  - "packages/application/sheet_vitrina_v1_own_product_capital.py"
related_tables:
  - "sheet_vitrina_v1_warehouse_functional_versions"
  - "sheet_vitrina_v1_warehouse_functional_balances"
  - "sheet_vitrina_v1_warehouse_functional_documents"
  - "sheet_vitrina_v1_own_capital_events (canonical source-event input)"
  - "sheet_vitrina_v1_own_capital_daily_state (bounded functional projection input)"
  - "sheet_vitrina_v1_own_capital_* (other legacy audit/read support)"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/warehouses"
  - "GET /v1/sheet-vitrina-v1/product-capital/status"
source_of_truth_level: "module_canonical"
update_note: "Active vitrina содержит только quantity/WAC/capital для шести стадий и три общих итога; paid-equivalent, coverage, confirmation, underaccepted, Proxy 2, старые 1C totals и inventory return переведены в technical archive."
---

# 1. Единственные шесть стадий

Товарный капитал состоит ровно из шести взаимоисключающих stage totals:

1. `На производстве` (`production`);
2. `Китай → FF` (`china_to_ff`);
3. `Склад FF` (`ff`);
4. `FF → WB` (`ff_to_wb`);
5. `Склад WB` (`wb`);
6. `Расхождения приёмки WB` (`wb_acceptance_discrepancy`).

Supplier registry не является седьмым складом. Он хранит состояние invoice/order и stable `supplier_flow_id`; warehouse projection агрегирует все активные flows по SKU. На FF одинаковые SKU смешиваются moving WAC. После смешивания downstream identity принадлежит WB supply.

# 2. Количество и капитал

Production quantity равно нулю до первого confirmed supplier payment. Первый платёж, включая 15%, активирует полный physical invoice composition. Последующие платежи увеличивают capital/WAC, но не quantity. Legacy `paid_equivalent_qty` остаётся только audit/diagnostic field и не участвует в public warehouse quantity, WAC или общем капитале.

Physical source по стадиям:

- production/China → FF — active supplier flows и factual shipment dates;
- FF — existing append-only FF ledger; functional projection не создаёт второй FF ledger;
- FF → WB — `max(packed - accepted, 0)` до final acceptance;
- WB — только complete official WB contour snapshot `quantity + inWayToClient + inWayFromClient`;
- discrepancies — positive final `packed - accepted`, уменьшенное pooled doprinato matching строго по SKU.

Accepted supply quantity не добавляется поверх WB snapshot. Пока приёмка не final, open quantity остаётся только в FF → WB. После final acceptance positive difference перемещается только в discrepancy; она не остаётся в FF → WB. Transitional unmatched doprinato — audit registry с нулевым warehouse quantity/capital.

Capital следует тому же physical layer. В каждой стадии и по каждому SKU:

`WAC = capital / quantity`.

Обычное proportional movement переносит exact quantity и proportional capital без изменения unit WAC. Все intermediate calculations используют `Decimal`; округляется только UI.

Каждый новый `FF → WB` debit переносит immutable line-level FF cost snapshot
из active same-SKU FF WAC. Отмена/доказанное исчезновение возвращает только
непринятый остаток и ровно тот же original debit capital; текущий/будущий WB
WAC, transit add-ons, cross-SKU/warehouse average and zero fallback запрещены.
Настоящая inventory adjustment замораживает единый pre-adjustment FF basis на
business date: exact source cost, same-date FF WAC, last earlier FF WAC,
certified inbound landed FF cost, затем только отдельная versioned approved
estimate. Отсутствующая basis блокирует строку. Receipt/writeoff and their T1
compensation carry signed Decimal quantity/capital in audit provenance, а
последующий targeted replay обновляет derived stage/WB/Vitrina/Finance values
without a second physical movement.

# 3. Active vitrina and TOTAL semantics

Для каждой из шести стадий active catalog содержит ровно три пользовательские строки: quantity, `Средневзвешенная себестоимость, ₽/шт`, `Товарный капитал, ₽`. Для пяти стадий quantity сохраняет подпись `Количество, шт`; WB SKU и TOTAL используют точную подпись `Склад WB: весь контур, шт`, потому что эта строка показывает полный контур `quantity + inWayToClient + inWayFromClient`, а не только физический компонент `На складах WB` или отдельную метрику `Остаток всего`. Общий блок содержит `Всего единиц`, `Общий товарный капитал`, `Общая средневзвешенная себестоимость, ₽/шт`. Пустой склад показывает quantity/capital `0`, а WAC — единообразно `—`.

Legacy `paid_equivalent_qty`, cost coverage/confirmation, old `Недопринято WB`, inventory-capital return, duplicate 1C capital rows, Proxy 2 and `our_wb_cost_confirmed_share_pct` remain source/audit fields only. Central public filter excludes them from normal live-plan materialization, web contract, filter controls and activity metric labels; an archived-only source cannot run an active group refresh or downgrade public `/status`, while its raw result remains technical evidence. Guarded economics backfill removes their stable `scope|metric` materializations from every persisted ready snapshot, including snapshots whose columns are entirely before the 2026-07-01 economics boundary, without deleting primary source tables. If the functional cutover is absent or rolled back, the canonical compatibility reader still calculates every public SKU/stage WAC as capital divided by physical quantity; paid-equivalent remains diagnostic only.

- stage quantity = сумма positive SKU quantities;
- stage capital = сумма SKU capital;
- stage WAC = `SUM(capital) / SUM(quantity)`;
- overall quantity = сумма шести stage quantities;
- overall capital = сумма шести stage capitals;
- overall WAC = `SUM(all stage capital) / SUM(all stage quantity)`;
- internal coverage/confirmation diagnostics — ratio of quantity/capital aggregates по фактическим quality buckets, не average SKU percentages; они не являются active user metrics.

Каждая физическая единица входит ровно в один stage total. WB physical/in-way/return quantities сначала суммируются внутри WB contour и не являются дополнительными stages. Discrepancy не прибавляется второй раз к FF → WB.

# 4. Cost boundary

На производстве capital включает factual CNY supplier payments по weighted RUB cost списанного CNY и относящиеся direct bank fees ровно один раз. В China → FF добавляются confirmed logistics/customs components с canonical allocation rules. FF receipt переносит exact supplier-flow capital. FF → WB добавляет confirmed FF services, storage и transit по packed quantity. Paid WB acceptance относится только к actually accepted units и не входит в discrepancy cost.

WB использует periodic/snapshot WAC: official snapshot задаёт quantity, accepted supplies добавляют доказанный inbound capital, current day provisional, closed days versioned. Zero-stock SKU сохраняет last valid WAC. `Себестоимость WB наша` является direct read projection этого canonical WB WAC.

# 5. Historical and migration boundary

`warehouse_opening_v1`, legacy own-capital events/daily rows и прежние canonical-cost baseline rows immutable audit-only. Они не суммируются с active functional state. Полная warehouse history начинается с production timestamp `warehouse_functional_cutover_v1`; текущий snapshot назад не копируется.

До functional cutover разрешена только bounded cost projection с `2026-07-01` для `our_wb_unit_cost_rub`, Proxy 3 и direct consumers. Она использует frozen opening map от доказанной accepted-on-FF поставки около 24.06, persisted historical quantities из exact business-date columns и confirmed downstream expenses. Outer ready-snapshot date не подменяет дату колонки; current snapshot не копируется назад. Positive quantity не получает silent zero/NULL cost; fallback всегда имеет explicit quality/provenance.

Source change/archive не удаляет ledger evidence: он сбрасывает certification и ставит targeted replay по stable flow/supply/SKU/effective date. Failed candidate сохраняет last good active version.

## CNY document exclusion and relink

An operator CNY document is durable audit evidence; exclusion never deletes its file or source row. Canonical replay removes the derived supplier-payment layer and its capital events for an excluded document, rebases remaining cumulative payment shares, and recalculates capital. Relink first compensates the old derived layer, atomically changes the document shipment context under `BEGIN IMMEDIATE`, then rebuilds exactly one posted ledger operation and one payment layer for the target shipment. Old and target expense certification are reset and one combined targeted warehouse request covers both SKU sets. Repeating the confirmation or bounded recovery is a no-op and cannot create a second CNY document, ledger operation or capital layer.

Archiving a logistics/customs/bank-fee financial source likewise compensates every derived `cost_payment:financial_expense:<document_id>:*` event before recalculation. The financial document, expense lines and file remain audit evidence, but none of their derived capital survives in the active chain; restore rematerializes the original source once.

## Business-time projection seam

The public facade still does not own an independent warehouse baseline.
Canonical source revisions and event evidence feed the functional
warehouse/product-capital calculation; `warehouse_business_projection_v1`
publishes only its bounded exact-date owned metric rows for read consumers.
Web Vitrina is not a calculator and cannot write to source/event tables.

Functional versions now separate `business_effective_date` from
`published_at`. Historical selection requires exact snapshot date and
business-effective eligibility; the technical publication timestamp only
orders competing revisions. Same-day event replay uses explicit source-credit
then physical-stage order, never `created_at`.

Cost-only events preserve all quantity keys byte-for-byte and change
capital/WAC once from the source business date. Physical movements carry exact
or proportional capital with conservation. Event insert/delete,
certification and official WB/WAC changes enqueue the projection in the same
transaction. A complete candidate atomically replaces only its date/SKU/TOTAL
rows; failure preserves last-good current rows.

Migration 127 publishes exact July history through that same owned seam. For
`2026-07-19..29` the six-stage SKU/TOTAL rows come from exact functional
versions. For `2026-07-01..18` only persisted WB quantity/WAC/capital is
available: the other five stages and all-stage totals carry explicit
unavailable presentation/provenance and remain blank. The facade never infers
them from a neighboring or current snapshot and never converts missing to
zero.
