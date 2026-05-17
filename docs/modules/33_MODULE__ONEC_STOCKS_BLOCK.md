---
title: "Модуль: onec_stocks_block"
doc_id: "WB-CORE-MODULE-33-ONEC-STOCKS-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать bounded-интеграцию 1C/Soykasoft API остатков, себестоимости WB, товарного капитала и связанных расчётных метрик web-vitrina."
scope: "Source/adapter/parser/normalizer для `/hs/soykasoft/stocks_wb`, date-specific historical load через `date=YYYY-MM-DD`, web-vitrina metric wiring, fixture-backed smokes, optional live smoke и runtime env contract."
source_basis:
  - "packages/contracts/onec_stocks_block.py"
  - "packages/adapters/onec_stocks_block.py"
  - "packages/application/onec_stocks_block.py"
  - "packages/application/sheet_vitrina_v1_onec_stocks.py"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
  - "apps/onec_stocks_block_smoke.py"
  - "apps/sheet_vitrina_v1_onec_stocks_wiring_smoke.py"
  - "apps/onec_stocks_block_live_smoke.py"
  - "artifacts/onec_stocks_block/source/success__stocks_wb__fixture.json"
  - "artifacts/onec_stocks_block/evidence/initial__onec-stocks__evidence.md"
related_modules:
  - "packages/contracts/onec_stocks_block.py"
  - "packages/adapters/onec_stocks_block.py"
  - "packages/application/onec_stocks_block.py"
  - "packages/application/sheet_vitrina_v1_onec_stocks.py"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
related_tables: []
related_endpoints:
  - "/hs/soykasoft/stocks_wb"
related_runners:
  - "apps/onec_stocks_block_smoke.py"
  - "apps/sheet_vitrina_v1_onec_stocks_wiring_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_group_coverage_smoke.py"
  - "apps/onec_stocks_block_live_smoke.py"
related_docs:
  - "docs/modules/07_MODULE__STOCKS_BLOCK.md"
  - "docs/modules/12_MODULE__COGS_BY_GROUP_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "1C source теперь date-specific для истории и подключён к web-vitrina как source group `onec_product_capital`; расчётные метрики `proxy_profit_2_rub`, `proxy_margin_2_pct` и `inventory_capital_return_pct` используют 1C WB unit cost и 1C товарный капитал."
---

# 1. Идентификатор и статус

- `module_id`: `onec_stocks_block`
- `family`: `external-1c-source`
- `status_transfer`: bounded source path и web-vitrina metric wiring добавлены в `wb-core`
- `status_verification`: fixture-backed source smoke, wiring smoke и group-coverage smoke подтверждают parser/normalizer, date-specific snapshots, source group wiring и расчётные метрики; live smoke optional и env-guarded
- `status_main`: active/current in repo; optional live smoke remains env-guarded

# 2. Runtime contract

Live adapter не хранит и не печатает секреты. Для live smoke/runtime нужны env:

- `ONEC_STOCKS_BASE_URL`
- `ONEC_STOCKS_BASIC_USER`
- `ONEC_STOCKS_BASIC_PASSWORD`
- `ONEC_STOCKS_TOKEN`
- optional `ONEC_STOCKS_TIMEOUT_SECONDS`
- optional smoke params: `ONEC_STOCKS_SMOKE_ACCOUNT_ID`, `ONEC_STOCKS_SMOKE_NM_ID`

HTTP contract:

- method path: `/hs/soykasoft/stocks_wb`
- auth: HTTP Basic auth from env plus HTTP header `token` from env
- confirmed current/historical query shape: `account_id=<id>&date=<YYYY-MM-DD>&nmId=<nmId>`
- historical loads must pass `date=<requested_date>` and accept a snapshot only when `payload.meta.date == requested_date`
- current live adapter is intentionally bounded to exactly one `nmId` per request until the 1C batch contract is explicitly confirmed.

# 3. Source payload fields

Parser supports:

- `meta.version`
- `meta.marketplace`
- `meta.account_id`
- `meta.date`
- `meta.generated_at`
- `meta.currency`
- `items[].nmId`
- `items[].product_1c_id`
- `items[].vendor_code`
- `items[].name`
- `items[].stages`
- `stage.qty`
- `stage.unit_cost_rub`
- `stage.cost_total_rub`
- optional `items[].sizes[]` as opaque object rows when present

# 4. Stage semantics

`items[].stages` keys are dynamic 1C section names, not a fixed enum. Parser preserves any non-empty stage name from the response.

Canonical mapping is only a boundary for future acceptance config. Contract-supported canonical codes are:

- `CHINA_TO_FF`
- `CN_TO_RU_TRANSIT`
- `FF_TO_WB`
- `FF_TO_WB_TRANSIT`
- `FF_STOCK`
- `WB_STOCK`
- `CN_PRODUCTION_PAID`

Current normalization flattens source stage rows and may annotate a row with `canonical_stage_code` if explicit mapping config is supplied. It does not aggregate rows by canonical code and does not infer canonical stages from Russian stage text. This prevents silently combining distinct operational stages or warehouses when 1C section names are reused.

Current web-vitrina metric wiring uses four stage buckets:

- `CHINA_TO_FF`
- `FF_STOCK`
- `FF_TO_WB`
- `WB_STOCK`

Runtime default mapping folds source/contract aliases such as `CN_TO_RU_TRANSIT` into `CHINA_TO_FF` and `FF_TO_WB_TRANSIT` into `FF_TO_WB`.

# 5. Code parts

- contracts: `packages/contracts/onec_stocks_block.py`
- adapters: `packages/adapters/onec_stocks_block.py`
- application: `packages/application/onec_stocks_block.py`
- fixture: `artifacts/onec_stocks_block/source/success__stocks_wb__fixture.json`
- offline smoke: `apps/onec_stocks_block_smoke.py`
- optional live smoke: `apps/onec_stocks_block_live_smoke.py`

# 6. Current boundaries

This module is wired into current web-vitrina metrics, but it still does not:

- replace existing WB official `stocks_block`;
- replace or feed `cogs_by_group_block`;
- define production mapping config storage;
- confirm multi-`nmId` live batching;

# 7. Web-vitrina metrics

The active web-vitrina source group is:

- `source_group_id = onec_product_capital`
- `source_key = onec_stocks`
- label = `1С / товарный капитал`

Source metric ids used by derived calculations:

- `onec_WB_STOCK_unit_cost_rub` = SKU-level `1С WB: себестоимость за ед., руб`
- `onec_total_cost_rub` = SKU-level `1С товарный капитал всего, руб`
- `total_onec_total_cost_rub` = TOTAL `1С: товарный капитал всего, руб`

Derived metrics:

- `proxy_profit_2_rub` / `total_proxy_profit_2_rub`: current proxy-profit formula with only `cost_price_rub` replaced by `onec_WB_STOCK_unit_cost_rub`
- `proxy_margin_2_pct` / `proxy_margin_2_pct_total`: SKU `proxy_profit_2_rub / orderSum`, TOTAL `SUM(proxy_profit_2_rub) / SUM(orderSum)`
- `inventory_capital_return_pct` / `inventory_capital_return_pct_total`: SKU `proxy_profit_2_rub / onec_total_cost_rub`, TOTAL `SUM(proxy_profit_2_rub) / SUM(onec_total_cost_rub)`

Percent totals are ratio-of-aggregates and are not row averages. Zero denominators return `0.0` when numerator data is present, matching the existing proxy margin protection.

# 8. Known gaps and next step

Next step for ingestion is an explicit acceptance layer that owns:

- where stage mapping config is stored;
- whether unmapped 1C stages block ingestion or remain source-only diagnostics;
- how duplicate/reused 1C section names are reviewed before canonical mapping;
- how normalized rows are joined with SKU/config truth;
- whether live batching is supported by 1C or should remain one request per `nmId`.
