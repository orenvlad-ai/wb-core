---
title: "Модуль: our_wb_cost_model"
doc_id: "WB-CORE-MODULE-40-OUR-WB-COST-MODEL-BLOCK"
doc_type: "module"
status: "active_read_side_facade"
purpose: "Зафиксировать compatibility/read-side boundary `Себестоимость WB наша` поверх единого canonical cost engine."
scope: "Public metric keys, proxy profit/margin 3 и legacy audit tables; отдельная физическая или baseline-модель модулю больше не принадлежит."
source_basis:
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "docs/modules/36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "docs/modules/39_MODULE__FULFILLMENT_SERVICES_BLOCK.md"
  - "docs/modules/43_MODULE__FF_STOCK_LEDGER_BLOCK.md"
  - "docs/modules/45_MODULE__OWN_PRODUCT_CAPITAL_BLOCK.md"
related_modules:
  - "packages/application/canonical_cost_engine.py"
  - "packages/application/our_wb_costs.py"
  - "packages/application/sheet_vitrina_v1_our_wb_costs.py"
  - "apps/canonical_cost_engine_backfill.py"
related_tables:
  - "sheet_vitrina_v1_canonical_cost_components"
  - "sheet_vitrina_v1_canonical_cost_movement_layers"
  - "sheet_vitrina_v1_canonical_cost_daily_state"
  - "sheet_vitrina_v1_supplier_ff_cost_layers (source cost-layer evidence)"
  - "sheet_vitrina_v1_wb_cost_daily_state (legacy audit)"
related_endpoints:
  - "POST /v1/sheet-vitrina-v1/wb-cost/recalculate"
  - "GET /v1/sheet-vitrina-v1/wb-cost/status"
source_of_truth_level: "module_canonical"
update_note: "С 2026-07-01 module 40 является read-side facade единого canonical cost engine; собственные opening/rolling rows не являются live truth."
---

# 1. Boundary

С `2026-07-01` `our_wb_unit_cost_rub`, confirmed-share, `proxy_profit_3_rub` и `proxy_margin_3_pct` читают recognized WB projection из `packages/application/canonical_cost_engine.py`. Module 40 сохраняет публичные metric keys и endpoint compatibility, но не владеет отдельным baseline, physical quantity или rolling methodology.

Legacy `sheet_vitrina_v1_wb_opening_baseline`, `sheet_vitrina_v1_wb_supply_cost_layers` и `sheet_vitrina_v1_wb_cost_daily_state` сохраняются как migration evidence/audit. После guarded baseline apply runtime сначала читает canonical `WB` daily rows. До apply переключение не активируется.

# 2. Unified components and projections

Canonical component содержит component type, shipment/supply/SKU, recognized amount/date, paid amount/date, allocation method, source document/line, provenance, confirmation status, fingerprint/version. Один component graph строит:

- recognized projection для WB cost, COGS, Finance/P&L и proxy3;
- paid projection для товарного капитала.

Поздний документ сохраняет factual effective date, создаёт новую immutable component version и инвалидирует replay от самой ранней затронутой recognized/paid date. Upload time не становится business date.

# 3. FF and WB rolling

FF receipt меняет SKU moving WAC. FF writeoff сохраняет immutable recognized/paid snapshot конкретного ledger debit. WB supply связывается с этим snapshot по source operation/supply identity; более новый FF receipt не может изменить старую WB supply cost. Transit, accepted FF service и allocated storage добавляются как supply-specific canonical components.

WB daily quantity берётся только из official persisted WB stock. Eligible inbound требует accepted status, final accepted quantity и factual accepted date. Stock reduction сохраняет WAC. Unexplained growth использует существующий WAC только с quality `unexplained_growth_existing_wac`; без предыдущей стоимости остаётся coverage gap.

# 4. Opening baseline

Единственный baseline принадлежит canonical engine. Primary source автоматически обнаруживается среди fully matched/certified `accepted_ff` shipment в окне `2026-06-21..2026-06-24`, с количеством не менее 100 000, confirmed FF layer, reconciliation `ok` и weighted landed cost `111.181389 ± 0.01 ₽/шт`.

Для отсутствующей SKU используется ближайший назад `onec_FF_STOCK_unit_cost_rub`, строго `<= 2026-05-16`, с exact bundle/date/metric provenance и quality `legacy_1c_fallback`. `near_future_proxy`, 1С после cutoff, WB-stage 1C cost, future shipment, zero и hidden last-known fallback запрещены. Любая missing owned SKU блокирует весь baseline; coverage должна быть 100%.

# 5. Finance compatibility

Finance/P&L применяет canonical recognized WB projection с `2026-07-01`; даты раньше cutover остаются legacy. Existing `our_wb_unit_cost_rub` contract сохраняется. TOTAL cost и confirmation считаются ratio of Decimal aggregates, не средним SKU averages.

# 6. Migration and non-goals

`apps/canonical_cost_engine_backfill.py` — единственный apply-capable runner. Он default dry-run, требует scope `2026-07-01..current`, stable fingerprint, explicit backup directory, coherent SQLite backup `0600`, `integrity_check=ok`, `BEGIN IMMEDIATE`, optimistic recheck и in-place apply без `os.replace`/force/partial mode. Apply разрешается только отдельным human gate.

Не являются целями: бухгалтерский FIFO, изменение 1С/proxy2, Google Sheets/GAS, browser truth, ad-hoc SQL или server-only fixes.
