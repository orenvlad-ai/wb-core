---
title: "Модуль: onec_stocks_block"
doc_id: "WB-CORE-MODULE-33-ONEC-STOCKS-BLOCK"
doc_type: "module"
status: "bounded_source_candidate"
purpose: "Зафиксировать bounded-интеграцию 1C/Soykasoft API остатков и себестоимости WB без подключения к финальным расчетам."
scope: "Новый source/adapter/parser/normalizer для `/hs/soykasoft/stocks_wb`, fixture-backed smoke, optional live smoke и runtime env contract."
source_basis:
  - "packages/contracts/onec_stocks_block.py"
  - "packages/adapters/onec_stocks_block.py"
  - "packages/application/onec_stocks_block.py"
  - "apps/onec_stocks_block_smoke.py"
  - "apps/onec_stocks_block_live_smoke.py"
  - "artifacts/onec_stocks_block/source/success__stocks_wb__fixture.json"
  - "artifacts/onec_stocks_block/evidence/initial__onec-stocks__evidence.md"
related_modules:
  - "packages/contracts/onec_stocks_block.py"
  - "packages/adapters/onec_stocks_block.py"
  - "packages/application/onec_stocks_block.py"
related_tables: []
related_endpoints:
  - "/hs/soykasoft/stocks_wb"
related_runners:
  - "apps/onec_stocks_block_smoke.py"
  - "apps/onec_stocks_block_live_smoke.py"
related_docs:
  - "docs/modules/07_MODULE__STOCKS_BLOCK.md"
  - "docs/modules/12_MODULE__COGS_BY_GROUP_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Создан как bounded source checkpoint для 1C/Soykasoft stocks+cost source; не заменяет `stocks_block` или `cogs_by_group_block`."
---

# 1. Идентификатор и статус

- `module_id`: `onec_stocks_block`
- `family`: `external-1c-source`
- `status_transfer`: bounded source path добавлен в `wb-core`
- `status_verification`: fixture-backed smoke подтверждает parser/normalizer; live smoke optional и env-guarded
- `status_main`: candidate до прохождения production-lane gates

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
- confirmed query shape: `account_id=<id>&nmId=<nmId>`
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

Canonical mapping is only a boundary for future acceptance config. Supported canonical codes are:

- `CN_TO_RU_TRANSIT`
- `FF_TO_WB_TRANSIT`
- `FF_STOCK`
- `WB_STOCK`
- `CN_PRODUCTION_PAID`

Current normalization flattens source stage rows and may annotate a row with `canonical_stage_code` if explicit mapping config is supplied. It does not aggregate rows by canonical code and does not infer canonical stages from Russian stage text. This prevents silently combining distinct operational stages or warehouses when 1C section names are reused.

# 5. Code parts

- contracts: `packages/contracts/onec_stocks_block.py`
- adapters: `packages/adapters/onec_stocks_block.py`
- application: `packages/application/onec_stocks_block.py`
- fixture: `artifacts/onec_stocks_block/source/success__stocks_wb__fixture.json`
- offline smoke: `apps/onec_stocks_block_smoke.py`
- optional live smoke: `apps/onec_stocks_block_live_smoke.py`

# 6. What is not wired

This checkpoint does not:

- replace existing WB official `stocks_block`;
- replace or feed `cogs_by_group_block`;
- connect 1C values to web-vitrina, ready snapshots, final financial calculations or sheet/export surfaces;
- define production mapping config storage;
- confirm multi-`nmId` live batching;
- promote any source data into accepted truth.

# 7. Known gaps and next step

Next step for ingestion is an explicit acceptance layer that owns:

- where stage mapping config is stored;
- whether unmapped 1C stages block ingestion or remain source-only diagnostics;
- how duplicate/reused 1C section names are reviewed before canonical mapping;
- how normalized rows are joined with SKU/config truth;
- whether live batching is supported by 1C or should remain one request per `nmId`.
