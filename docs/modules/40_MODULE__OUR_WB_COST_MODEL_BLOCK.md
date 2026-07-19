---
title: "Модуль: our_wb_cost_model"
doc_id: "WB-CORE-MODULE-40-OUR-WB-COST-MODEL-BLOCK"
doc_type: "module"
status: "active_read_side_facade"
purpose: "Зафиксировать direct projection `Себестоимость WB наша` и versioned Proxy 3 поверх canonical functional warehouse/cost engine."
scope: "Public metric keys, daily WB WAC, Proxy profit/margin 3, calculation parameters and legacy audit boundary."
source_basis:
  - "docs/modules/36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "docs/modules/44_MODULE__WB_FINANCE_WEEKLY_REPORT_BLOCK.md"
  - "docs/modules/45_MODULE__OWN_PRODUCT_CAPITAL_BLOCK.md"
  - "docs/modules/48_MODULE__WAREHOUSE_STOCKS_BLOCK.md"
related_modules:
  - "packages/application/warehouse_functional.py"
  - "packages/application/calculation_parameters.py"
  - "packages/application/our_wb_costs.py"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
  - "packages/application/warehouse_functional_economics_backfill.py"
  - "packages/application/sheet_vitrina_v1_proxy_margin_3_historical_backfill.py"
related_tables:
  - "sheet_vitrina_v1_warehouse_functional_balances"
  - "sheet_vitrina_v1_warehouse_wb_daily_cost"
  - "sheet_vitrina_v1_calculation_parameter_versions"
  - "sheet_vitrina_v1_wb_cost_daily_state (legacy audit/fallback before functional apply only)"
related_endpoints:
  - "GET|POST /v1/sheet-vitrina-v1/settings/calculation-parameters"
  - "POST /v1/sheet-vitrina-v1/settings/calculation-parameters/preview"
  - "GET /v1/sheet-vitrina-v1/warehouses"
source_of_truth_level: "module_canonical"
update_note: "С 2026-07-01 active Proxy 3 читает versioned settings и exact-date canonical daily WB WAC; Proxy 2 и подтверждённость себестоимости остаются только technical archive и не выходят в active vitrina."
---

# 1. Canonical WB WAC

`our_wb_unit_cost_rub` / `total_our_wb_unit_cost_rub` (`Себестоимость WB наша`) — direct read projection canonical daily WB WAC. Отдельная formula/baseline в module 40 запрещена.

WB quantity задаёт только complete official contour snapshot:

`quantity + inWayToClient + inWayFromClient`.

Accepted WB supply добавляет доказанный inbound capital, но не quantity поверх snapshot. Periodic WAC сохраняет last valid cost при zero stock; late cost evidence запускает targeted replay от effective business date. TOTAL cost:

`SUM(WB contour capital) / SUM(WB contour quantity)`.

Для SKU/дат `2026-07-01..functional cutover` loader сначала читает frozen functional historical cost projection. Она построена из frozen 24.06 opening map, persisted historical quantities и known downstream costs. Если ready snapshot содержит период, lookup выбирает только колонку точной business date, даже когда внешний `snapshot.as_of_date` новее. Fallback на предыдущий/current snapshot и копирование текущего остатка назад запрещены. После cutover loader читает active functional daily/current state. Legacy WB daily tables остаются audit и не являются параллельным active source.

# 2. Versioned calculation parameters

`Настройки → Справочник пользователя → Расчётные параметры` хранит immutable versions с effective date, revision, author/time, exact fingerprint и diff preview. Initial version effective `2026-07-01`:

- buyout rate — `91%`;
- tax — `6%`;
- WB agent/other expenses — `38%`;
- acquiring/logistics/storage/penalties/other — `0%`;
- total included expenses — `44%`;
- retained share — `56%`.

Validation требует каждый процент в `0..100%` и total expenses `<100%`. Save создаёт новую version и targeted Proxy recalculation от effective date; physical warehouses не пересчитываются.

Reference table использует три последние полностью закрытые недели WB finance reports. Каждая expense category делится на ту же canonical gross buyout revenue base (`net_revenue`) и three-week value считается как `SUM(expense) / SUM(gross buyout revenue)`, а не arithmetic mean weekly percentages. Paid acceptance показана только как reference: она не входит в Proxy expense rate, потому что капитализируется в WB cost. `ads_sum` вычитается отдельно; FF → WB transit уже находится в cost.

# 3. Proxy 3 formula

Public Proxy 3 contract начинается `2026-07-01`. Legacy Proxy 2 definitions до этой границы сохраняются только как внутренний evaluator/source audit и не являются строками active web-vitrina. С `2026-07-01` для SKU/date:

```text
expected_buyout_revenue = orderSum × buyout_rate
expected_buyout_qty     = orderCount × buyout_rate
included_expense_rate   = SUM(enabled versioned expense rates)
proxy_profit_3           = expected_buyout_revenue × (1 − included_expense_rate)
                           − expected_buyout_qty × canonical_WB_WAC
                           − ads_sum
proxy_margin_3           = proxy_profit_3 / expected_buyout_revenue
```

Advertising is not multiplied by buyout rate. Missing required operand remains NULL; it does not become zero. Zero expected revenue returns NULL margin. TOTAL:

- profit = `SUM(SKU proxy profit)`;
- expected revenue = `SUM(SKU expected buyout revenue)`;
- margin = `total profit / total expected revenue`;
- SKU margins are never averaged.

Public keys remain `our_wb_unit_cost_rub`, `proxy_profit_3_rub`, `proxy_margin_3_pct` and their existing TOTAL keys. `our_wb_cost_confirmed_share_pct`, Proxy 2 and old inventory-return metrics are archived at both catalog/read-contract boundaries; persisted legacy rows may remain only as technical evidence and are removed by the guarded economics cutover.

# 4. Quality and consumers

Daily cost stores quality/provenance (`direct 24.06`, `same purchase price`, `interpolation`, `extrapolation`, `fallback average`, confirmed downstream layers). Vitrina does not invent a value when a required persisted source is truly absent. All direct consumers, including товарный капитал, его рентабельность, web-vitrina and `Управление SKU`, resolve the same functional daily projection from `2026-07-01`; hidden fallback to 1C/legacy cost after activation is prohibited.

# 5. Migration boundary

Legacy module-40 opening/supply/daily rows and the separate canonical-cost baseline stay immutable audit evidence. `warehouse_functional_cutover_v1` activates the single warehouse/cost engine and initial settings version atomically. The bounded historical backfill may rewrite only `our_wb_unit_cost_rub`, Proxy 3 and direct dependent read models from `2026-07-01`; it removes only the centrally enumerated archived metric rows, preserves every other non-target snapshot cell/digest, pins the exact ready-snapshot manifest and is idempotent.

Non-goals: accounting FIFO, event-based WB customer movements, Proxy 2 rewrite before the boundary, marketing as a percentage, transit double count, Google Sheets/GAS truth or ad-hoc production SQL.
