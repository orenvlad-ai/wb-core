---
title: "Модуль: table_projection_bundle_block"
doc_id: "WB-CORE-MODULE-14-TABLE-PROJECTION-BUNDLE-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `table_projection_bundle_block`."
scope: "Upstream source basis, target contract, артефакты, кодовые части, подтверждённый composition smoke и границы первого рабочего checkpoint."
source_basis:
  - "migration/65_new_table_minimum_data_contract.md"
  - "migration/70_table_projection_bundle_block_contract.md"
  - "migration/73_table_projection_bundle_block_source_note.md"
  - "artifacts/table_projection_bundle_block/evidence/initial__table-projection-bundle__evidence.md"
  - "apps/table_projection_bundle_block_smoke.py"
  - "apps/table_projection_bundle_block_composed_smoke.py"
related_modules:
  - "packages/contracts/table_projection_bundle_block.py"
  - "packages/adapters/table_projection_bundle_block.py"
  - "packages/application/table_projection_bundle_block.py"
related_tables: []
related_endpoints: []
related_runners:
  - "apps/table_projection_bundle_block_smoke.py"
  - "apps/table_projection_bundle_block_composed_smoke.py"
related_docs:
  - "migration/70_table_projection_bundle_block_contract.md"
  - "migration/71_table_projection_bundle_block_parity_matrix.md"
  - "migration/72_table_projection_bundle_block_evidence_checklist.md"
  - "migration/73_table_projection_bundle_block_source_note.md"
  - "artifacts/table_projection_bundle_block/evidence/initial__table-projection-bundle__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Создан как канонический модульный документ в рамках первого bounded checkpoint для server-side composition/projection блока `table_projection_bundle_block`."
---

# 1. Идентификатор и статус

- `module_id`: `table_projection_bundle_block`
- `family`: `projection`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Для этого блока используется `reference source`, а не обычный legacy RAW-path.
- Upstream/source basis фиксируется как:
  - `sku_display_bundle_block`
  - `web_source_snapshot_block`
  - `seller_funnel_snapshot_block`
  - `prices_snapshot_block`
  - `sf_period_block`
  - `spp_block`
  - `ads_bids_block`
  - `stocks_block`
  - `ads_compact_block`
  - `fin_report_daily_block`
  - `sales_funnel_history_block`
  - `migration/65_new_table_minimum_data_contract.md`
- Семантика модуля: не тянуть новые данные, а склеить один table-facing projection bundle поверх уже подтверждённых bounded outputs.

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `as_of_date`
  - `count`
  - `items[]`
  - `source_statuses[]`
- Empty shape:
  - `kind = "empty"`
  - `count = 0`
  - `items = []`
  - `source_statuses[]`
- Внутри row сохраняются:
  - базовые SKU/display поля;
  - summaries по `web_source`;
  - summaries по `official_api`;
  - linked `history_summary`.
- Целевой смысл блока: первый реальный server-side projection для новой витрины без full inline history и без новой таблицы.

# 4. Артефакты по модулю

- input bundle:
  - `artifacts/table_projection_bundle_block/input_bundle/normal__template__input-bundle__fixture.json`
  - `artifacts/table_projection_bundle_block/input_bundle/minimal__template__input-bundle__fixture.json`
- reference:
  - `artifacts/table_projection_bundle_block/reference/normal__template__module-output-map.json`
  - `artifacts/table_projection_bundle_block/reference/minimal__template__module-output-map.json`
- target:
  - `artifacts/table_projection_bundle_block/target/normal__template__target__fixture.json`
  - `artifacts/table_projection_bundle_block/target/minimal__template__target__fixture.json`
- parity:
  - `artifacts/table_projection_bundle_block/parity/normal__template__reference-vs-target__comparison.md`
  - `artifacts/table_projection_bundle_block/parity/minimal__template__reference-vs-target__comparison.md`
- evidence:
  - `artifacts/table_projection_bundle_block/evidence/initial__table-projection-bundle__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/table_projection_bundle_block.py`
- adapters: `packages/adapters/table_projection_bundle_block.py`
- application: `packages/application/table_projection_bundle_block.py`
- artifact-backed smoke: `apps/table_projection_bundle_block_smoke.py`
- bundle-composition smoke: `apps/table_projection_bundle_block_composed_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/table_projection_bundle_block_smoke.py`.
- Bundle-composition smoke подтверждён через `apps/table_projection_bundle_block_composed_smoke.py`.
- Для этого типа модуля canonical checkpoint определяется корректной композицией merged module outputs, а не live WB API smoke.

# 7. Что уже доказано по модулю

- Первый server-side projection bundle для новой витрины зафиксирован без новой таблицы и без новых data sources.
- SKU/display rows, source statuses, freshness и coverage собираются в один честный table-facing контракт.
- History-блок безопасно свёрнут до linked `history_summary`.
- Компоновать projection можно как из frozen input bundle, так и напрямую из уже существующих module fixtures.

# 8. Что пока не является частью финальной production-сборки

- новая таблица;
- полный read-model layer всей витрины;
- перепроектирование `CONFIG`, `METRICS`, `FORMULAS`, `DAILY RUN`;
- supply/report orchestration;
- jobs/API/deploy.
