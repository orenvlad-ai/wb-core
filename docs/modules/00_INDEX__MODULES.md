---
title: "Индекс канонической модульной документации wb-core"
doc_id: "WB-CORE-MODULE-00-INDEX"
doc_type: "index"
status: "active"
purpose: "Дать единый navigation entrypoint для канонической модульной документации `wb-core`."
scope: "Папка `docs/modules/`, её naming rules, статус source of truth и полный список модульных документов `01–32`."
source_basis:
  - "docs/modules/01_MODULE__WEB_SOURCE_SNAPSHOT_BLOCK.md"
  - "docs/modules/02_MODULE__SELLER_FUNNEL_SNAPSHOT_BLOCK.md"
  - "docs/modules/03_MODULE__PRICES_SNAPSHOT_BLOCK.md"
  - "docs/modules/04_MODULE__SF_PERIOD_BLOCK.md"
  - "docs/modules/05_MODULE__SPP_BLOCK.md"
  - "docs/modules/06_MODULE__ADS_BIDS_BLOCK.md"
  - "docs/modules/07_MODULE__STOCKS_BLOCK.md"
  - "docs/modules/08_MODULE__SALES_FUNNEL_HISTORY_BLOCK.md"
  - "docs/modules/09_MODULE__ADS_COMPACT_BLOCK.md"
  - "docs/modules/10_MODULE__FIN_REPORT_DAILY_BLOCK.md"
  - "docs/modules/11_MODULE__PROMO_BY_PRICE_BLOCK.md"
  - "docs/modules/12_MODULE__COGS_BY_GROUP_BLOCK.md"
  - "docs/modules/13_MODULE__SKU_DISPLAY_BUNDLE_BLOCK.md"
  - "docs/modules/14_MODULE__TABLE_PROJECTION_BUNDLE_BLOCK.md"
  - "docs/modules/15_MODULE__WIDE_DATA_MATRIX_V1_FIXTURE_BLOCK.md"
  - "docs/modules/16_MODULE__WIDE_DATA_MATRIX_DELIVERY_BUNDLE_V1_BLOCK.md"
  - "docs/modules/17_MODULE__SHEET_VITRINA_V1_SCAFFOLD_BLOCK.md"
  - "docs/modules/18_MODULE__SHEET_VITRINA_V1_WRITE_BRIDGE_BLOCK.md"
  - "docs/modules/19_MODULE__SHEET_VITRINA_V1_PRESENTATION_BLOCK.md"
  - "docs/modules/20_MODULE__REGISTRY_UPLOAD_BUNDLE_V1_BLOCK.md"
  - "docs/modules/21_MODULE__REGISTRY_UPLOAD_FILE_BACKED_SERVICE_BLOCK.md"
  - "docs/modules/22_MODULE__REGISTRY_UPLOAD_DB_BACKED_RUNTIME_BLOCK.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/27_MODULE__PROMO_XLSX_COLLECTOR_BLOCK.md"
  - "docs/modules/28_MODULE__PROMO_LIVE_SOURCE_WIRING_BLOCK.md"
  - "docs/modules/29_MODULE__WEB_VITRINA_VIEW_MODEL_BLOCK.md"
  - "docs/modules/30_MODULE__WEB_VITRINA_GRAVITY_TABLE_ADAPTER_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "docs/modules/32_MODULE__RESEARCH_SKU_GROUP_COMPARISON_BLOCK.md"
related_modules: []
related_tables: []
related_endpoints: []
related_runners: []
related_docs:
  - "01_MODULE__WEB_SOURCE_SNAPSHOT_BLOCK.md"
  - "02_MODULE__SELLER_FUNNEL_SNAPSHOT_BLOCK.md"
  - "03_MODULE__PRICES_SNAPSHOT_BLOCK.md"
  - "04_MODULE__SF_PERIOD_BLOCK.md"
  - "05_MODULE__SPP_BLOCK.md"
  - "06_MODULE__ADS_BIDS_BLOCK.md"
  - "07_MODULE__STOCKS_BLOCK.md"
  - "08_MODULE__SALES_FUNNEL_HISTORY_BLOCK.md"
  - "09_MODULE__ADS_COMPACT_BLOCK.md"
  - "10_MODULE__FIN_REPORT_DAILY_BLOCK.md"
  - "11_MODULE__PROMO_BY_PRICE_BLOCK.md"
  - "12_MODULE__COGS_BY_GROUP_BLOCK.md"
  - "13_MODULE__SKU_DISPLAY_BUNDLE_BLOCK.md"
  - "14_MODULE__TABLE_PROJECTION_BUNDLE_BLOCK.md"
  - "15_MODULE__WIDE_DATA_MATRIX_V1_FIXTURE_BLOCK.md"
  - "16_MODULE__WIDE_DATA_MATRIX_DELIVERY_BUNDLE_V1_BLOCK.md"
  - "17_MODULE__SHEET_VITRINA_V1_SCAFFOLD_BLOCK.md"
  - "18_MODULE__SHEET_VITRINA_V1_WRITE_BRIDGE_BLOCK.md"
  - "19_MODULE__SHEET_VITRINA_V1_PRESENTATION_BLOCK.md"
  - "20_MODULE__REGISTRY_UPLOAD_BUNDLE_V1_BLOCK.md"
  - "21_MODULE__REGISTRY_UPLOAD_FILE_BACKED_SERVICE_BLOCK.md"
  - "22_MODULE__REGISTRY_UPLOAD_DB_BACKED_RUNTIME_BLOCK.md"
  - "23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md"
  - "26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "27_MODULE__PROMO_XLSX_COLLECTOR_BLOCK.md"
  - "28_MODULE__PROMO_LIVE_SOURCE_WIRING_BLOCK.md"
  - "29_MODULE__WEB_VITRINA_VIEW_MODEL_BLOCK.md"
  - "30_MODULE__WEB_VITRINA_GRAVITY_TABLE_ADAPTER_BLOCK.md"
  - "31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "32_MODULE__RESEARCH_SKU_GROUP_COMPARISON_BLOCK.md"
source_of_truth_level: "navigation_only"
update_note: "Обновлён под Google Sheets decommission: modules 17/18/19/24/25 are archive/migration-only, module 26 current contour is website/operator/web-vitrina, and Google Sheets/GAS is no longer an active runtime/update/write/load/verify target."
---

# 1. Назначение индекса

`docs/modules/` — это канонический source of truth для модульной документации `wb-core`.

Полные модульные документы живут здесь. В других местах репозитория могут оставаться:
- migration contracts;
- parity/checklist документы;
- evidence;
- короткие указатели.

Но канонический свод по модулю должен отражаться в `docs/modules/`.

# 1.1 Текущий Checkpoint Main

На текущем `main` main-confirmed модульные блоки доходят до `01–32`.

Подтверждённый main-confirmed contour:
- `sku_display_bundle_block`
- `table_projection_bundle_block`
- `registry_pilot_bundle`
- `wide_data_matrix_v1_fixture_block`
- `wide_data_matrix_delivery_bundle_v1_block`
- `sheet_vitrina_v1_scaffold_block`

Дополнительно в `main` уже есть:
- archived `sheet_vitrina_v1_write_bridge_block`;
- archived `sheet_vitrina_v1_presentation_block`;
- `registry_upload_bundle_v1_block` как первый artifact-backed upload bundle и local validator для V2-реестров.
- `registry_upload_file_backed_service_block` как первый file-backed accept/store/activate слой для V2-реестров.
- `registry_upload_db_backed_runtime_block` как первый DB-backed runtime ingest и current-truth слой для V2-реестров.
- `registry_upload_http_entrypoint_block` как первый live HTTP entrypoint для V2-реестров and repo-owned hosted public route allowlist/deploy publication boundary.
- archived `sheet_vitrina_v1_registry_upload_trigger_block` как former Apps Script trigger отправки `CONFIG / METRICS / FORMULAS`.
- archived `sheet_vitrina_v1_registry_seed_v3_bootstrap_block` как former compact v3 bootstrap для Google Sheets.
- `promo_xlsx_collector_block` как bounded repo-owned local collector precursor для promo XLSX + metadata sidecar.
- `promo_live_source_wiring_block` как bounded live wiring этого precursor обратно в current `sheet_vitrina_v1` refresh/runtime/read-side contour.
- `web_vitrina_view_model_block` как bounded phase-2 presentation-domain слой между stable `web_vitrina_contract` и concrete grid/page layers.
- `web_vitrina_gravity_table_adapter_block` как bounded phase-3 concrete adapter для `@gravity-ui/table` над stable `view_model`.
- `web_vitrina_page_composition_block` как bounded phase-4 live page composition на `/sheet-vitrina-v1/vitrina` с existing read route, вкладкой `Отзывы` поверх read-only WB feedbacks route, transient AI prompt/analyze flow, read-only Seller Portal complaint scout + no-submit matching replay runners и minimal inline client island.
- `research_sku_group_comparison_block` как первый read-only MVP-контур вкладки `Исследования`: ретроспективное сравнение двух непересекающихся групп SKU по non-financial метрикам поверх persisted ready snapshots, с candidate-only chip `Товар в акции`, compact date-range period controls и scrollable table/grid result.

Главный незакрытый gap текущей линии:
- текущий `main` уже содержит server upload line and bounded refresh/read split for website/operator web-vitrina;
- former reverse-load обратно в Google Sheets `DATA_VITRINA` is archived/do-not-use;
- bounded promo collector contour теперь уже repo-owned и live-wired обратно в current refresh/read-side contour;
- незакрытым остаются full legacy parity, plus actual hosted deploy rights/publish wiring, final auth-hardening и production storage binding вокруг уже repo-owned deploy contract.

# 2. Naming rules комплекта

Все файлы пакета именуются по шаблону:

`NN_MODULE__MODULE_NAME.md`

Где:
- `NN` — двузначный порядок внутри каталога;
- `MODULE` — фиксированный doc-class для модульных документов;
- `MODULE_NAME` — machine-friendly идентификатор модуля в ASCII и uppercase.

Каждый файл использует общий YAML front matter:
- `title`
- `doc_id`
- `doc_type`
- `status`
- `purpose`
- `scope`
- `source_basis`
- `related_*`
- `source_of_truth_level`
- `update_note`

Для модульных документов в `wb-core` используется:
- `doc_type: "module"`

# 3. Что фиксирует каждый модульный документ

Каждый файл в этом пакете должен фиксировать:
- идентификатор модуля и его текущий статус;
- семейство модуля;
- legacy-source и legacy semantics;
- target contract и смысл результата;
- состав артефактов;
- кодовые части `contracts / adapters / application / smoke`;
- подтверждённый тип smoke;
- что уже доказано;
- что ещё не является частью финальной production-сборки.

# 4. Список уже задокументированных модулей

| Файл | Модуль | Семейство | Короткий статус |
| --- | --- | --- | --- |
| `01_MODULE__WEB_SOURCE_SNAPSHOT_BLOCK.md` | `web_source_snapshot_block` | `web-source` | перенесён, проверен, смёржен в `main` |
| `02_MODULE__SELLER_FUNNEL_SNAPSHOT_BLOCK.md` | `seller_funnel_snapshot_block` | `web-source` | перенесён, проверен, смёржен в `main` |
| `03_MODULE__PRICES_SNAPSHOT_BLOCK.md` | `prices_snapshot_block` | `official-api` | перенесён, подтверждён server-side, смёржен в `main` |
| `04_MODULE__SF_PERIOD_BLOCK.md` | `sf_period_block` | `official-api` | перенесён, подтверждён server-side, смёржен в `main` |
| `05_MODULE__SPP_BLOCK.md` | `spp_block` | `official-api` | перенесён, подтверждён server-side, смёржен в `main` |
| `06_MODULE__ADS_BIDS_BLOCK.md` | `ads_bids_block` | `official-api` | перенесён, подтверждён server-side, смёржен в `main` |
| `07_MODULE__STOCKS_BLOCK.md` | `stocks_block` | `official-api` | перенесён, dual current+historical checkpoint подтверждён server-side, смёржен в `main` |
| `08_MODULE__SALES_FUNNEL_HISTORY_BLOCK.md` | `sales_funnel_history_block` | `official-api` | перенесён, подтверждён server-side, смёржен в `main` |
| `09_MODULE__ADS_COMPACT_BLOCK.md` | `ads_compact_block` | `official-api` | перенесён, подтверждён server-side, смёржен в `main` |
| `10_MODULE__FIN_REPORT_DAILY_BLOCK.md` | `fin_report_daily_block` | `official-api` | перенесён, подтверждён server-side, смёржен в `main` |
| `11_MODULE__PROMO_BY_PRICE_BLOCK.md` | `promo_by_price_block` | `rule-based` | перенесён, проверен, смёржен в `main` |
| `12_MODULE__COGS_BY_GROUP_BLOCK.md` | `cogs_by_group_block` | `rule-based` | перенесён, проверен, смёржен в `main` |
| `13_MODULE__SKU_DISPLAY_BUNDLE_BLOCK.md` | `sku_display_bundle_block` | `table-facing` | перенесён, проверен, смёржен в `main` |
| `14_MODULE__TABLE_PROJECTION_BUNDLE_BLOCK.md` | `table_projection_bundle_block` | `projection` | перенесён, проверен, смёржен в `main` |
| `15_MODULE__WIDE_DATA_MATRIX_V1_FIXTURE_BLOCK.md` | `wide_data_matrix_v1_fixture_block` | `wide-matrix` | перенесён, проверен, смёржен в `main` |
| `16_MODULE__WIDE_DATA_MATRIX_DELIVERY_BUNDLE_V1_BLOCK.md` | `wide_data_matrix_delivery_bundle_v1_block` | `delivery` | перенесён, проверен, смёржен в `main` |
| `17_MODULE__SHEET_VITRINA_V1_SCAFFOLD_BLOCK.md` | `sheet_vitrina_v1_scaffold_block` | `sheet-side` | archived / do not use; former scaffold retained as migration evidence |
| `18_MODULE__SHEET_VITRINA_V1_WRITE_BRIDGE_BLOCK.md` | `sheet_vitrina_v1_write_bridge_block` | `sheet-side` | archived / do not use; former live bridge retained as migration evidence |
| `19_MODULE__SHEET_VITRINA_V1_PRESENTATION_BLOCK.md` | `sheet_vitrina_v1_presentation_block` | `sheet-side` | archived / do not use; former live formatting retained as migration evidence |
| `20_MODULE__REGISTRY_UPLOAD_BUNDLE_V1_BLOCK.md` | `registry_upload_bundle_v1_block` | `registry` | artifact-backed bundle и validator подтверждены, смёржены в `main` |
| `21_MODULE__REGISTRY_UPLOAD_FILE_BACKED_SERVICE_BLOCK.md` | `registry_upload_file_backed_service_block` | `registry` | file-backed accept/store/activate/result подтверждены, смёржены в `main` |
| `22_MODULE__REGISTRY_UPLOAD_DB_BACKED_RUNTIME_BLOCK.md` | `registry_upload_db_backed_runtime_block` | `registry` | DB-backed runtime ingest, role-aware temporal slot cache и persisted closure-retry state подтверждены, смёржены в `main` |
| `23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md` | `registry_upload_http_entrypoint_block` | `registry` | live HTTP entrypoint, hosted public route allowlist/deploy publication, strict feedbacks load/export/AI prompt-model discovery routes, thin runtime wiring, source-aware web-source closed-day acceptance/retry, explicit seller-session probe и permanent operator-facing seller-session block (`session-check/start/status/stop/launcher`, safe stop, per-run `run_id`/final outcome, hardened noVNC launcher) подтверждены, смёржены в `main` |
| `24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md` | `sheet_vitrina_v1_registry_upload_trigger_block` | `sheet-side` | archived / do not use; former Apps Script upload trigger retained as migration evidence |
| `25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md` | `sheet_vitrina_v1_registry_seed_v3_bootstrap_block` | `sheet-side` | archived / do not use; former compact v3 bootstrap retained as migration evidence |
| `26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md` | `sheet_vitrina_v1_mvp_end_to_end_block` | `web/operator` | current website/operator/web-vitrina contour active, including compact supply district XLSX actions and read-only `Отзывы` tab; former Google Sheets load/write path archived |
| `27_MODULE__PROMO_XLSX_COLLECTOR_BLOCK.md` | `promo_xlsx_collector_block` | `browser-capture` | первый repo-owned bounded promo XLSX collector contour: canonical hydration/modal/drawer seams, truthful sidecar contract и bounded live integration smoke подтверждены, смёржен в `main` |
| `28_MODULE__PROMO_LIVE_SOURCE_WIRING_BLOCK.md` | `promo_live_source_wiring_block` | `browser-capture/live-source` | bounded live wiring promo collector output обратно в current refresh/runtime/read-side contour подтверждён, смёржен в `main` |
| `29_MODULE__WEB_VITRINA_VIEW_MODEL_BLOCK.md` | `web_vitrina_view_model_block` | `web-vitrina` | phase-2 library-agnostic `view_model` layer поверх stable `web_vitrina_contract`, с canonical mapper/filter/sort/formatter/state schema, подтверждён и смёржен в `main` |
| `30_MODULE__WEB_VITRINA_GRAVITY_TABLE_ADAPTER_BLOCK.md` | `web_vitrina_gravity_table_adapter_block` | `web-vitrina` | phase-3 concrete `@gravity-ui/table` adapter поверх stable `view_model`, с isolated Gravity-specific columns/rows/renderers/options/state surface, подтверждён и смёржен в `main` |
| `31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md` | `web_vitrina_page_composition_block` | `web-vitrina` | phase-4 server-driven sibling page composition поверх existing read route, truthful reporting blocks, read-only feedbacks tab with snapshot-independent bounded period picker, strict period/star load diagnostics, Excel export, transient AI prompt/model discovery flow, read-only Seller Portal complaint scout + no-submit matching replay runners, resizable columns и browser island подтверждены, смёржены в `main` |
| `32_MODULE__RESEARCH_SKU_GROUP_COMPARISON_BLOCK.md` | `research_sku_group_comparison_block` | `web/operator/research` | read-only MVP вкладки `Исследования`: SKU group comparison over accepted truth / ready snapshots, non-financial metrics only, promo candidate chip, compact period pickers and scrollable result grid |

# 5. Как эта папка используется дальше

- при добавлении нового модульного документа обновлять этот файл вместе с соответствующим `NN_MODULE__*.md`;
- считать этот файл navigation entrypoint для пакета `docs/modules/`;
- не документировать здесь как часть `main` блоки, которые существуют только на незамёрженных ветках или ещё не дошли до merge;
- не дублировать полный канонический модульный текст в других местах репозитория.
