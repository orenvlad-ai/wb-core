---
title: "Индекс канонической модульной документации wb-core"
doc_id: "WB-CORE-MODULE-00-INDEX"
doc_type: "index"
status: "active"
purpose: "Дать единый navigation entrypoint для канонической модульной документации `wb-core`."
scope: "Папка `docs/modules/`, её naming rules, статус source of truth и полный список модульных документов `01–26`."
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
source_of_truth_level: "navigation_only"
update_note: "Обновлён под current `main`: индекс отражает комплект модулей `01–26`, включая смёрженный bounded end-to-end MVP `sheet_vitrina_v1_mvp_end_to_end_block`."
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

На текущем `main` main-confirmed модульные блоки доходят до `01–26`.

Подтверждённый main-confirmed contour:
- `sku_display_bundle_block`
- `table_projection_bundle_block`
- `registry_pilot_bundle`
- `wide_data_matrix_v1_fixture_block`
- `wide_data_matrix_delivery_bundle_v1_block`
- `sheet_vitrina_v1_scaffold_block`

Дополнительно в `main` уже есть:
- `sheet_vitrina_v1_write_bridge_block`;
- `sheet_vitrina_v1_presentation_block`;
- `registry_upload_bundle_v1_block` как первый artifact-backed upload bundle и local validator для V2-реестров.
- `registry_upload_file_backed_service_block` как первый file-backed accept/store/activate слой для V2-реестров.
- `registry_upload_db_backed_runtime_block` как первый DB-backed runtime ingest и current-truth слой для V2-реестров.
- `registry_upload_http_entrypoint_block` как первый live HTTP entrypoint для V2-реестров.
- `sheet_vitrina_v1_registry_upload_trigger_block` как первый operator-facing trigger отправки `CONFIG / METRICS / FORMULAS` в уже materialized HTTP entrypoint.
- `sheet_vitrina_v1_registry_seed_v3_bootstrap_block` как compact v3 bootstrap для `CONFIG / METRICS / FORMULAS`, сохраняющий service/status block и не ломающий existing upload trigger.

Главный незакрытый gap текущей линии:
- текущий `main` уже содержит upload line, compact v3 bootstrap и bounded refresh/read split для reverse-load обратно в `DATA_VITRINA`;
- operator-facing `DATA_VITRINA` теперь materialize-ит тот же server-driven current-truth row set как data-driven `date_matrix` с ростом дат вправо, без локального 7-metric reshape и без sheet-side truth path;
- незакрытым остаются full legacy parity, live numeric coverage для promo/cogs-backed metrics, stable hosted runtime URL, deploy/auth-hardening и production storage binding.

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
| `07_MODULE__STOCKS_BLOCK.md` | `stocks_block` | `official-api` | перенесён, подтверждён server-side, смёржен в `main` |
| `08_MODULE__SALES_FUNNEL_HISTORY_BLOCK.md` | `sales_funnel_history_block` | `official-api` | перенесён, подтверждён server-side, смёржен в `main` |
| `09_MODULE__ADS_COMPACT_BLOCK.md` | `ads_compact_block` | `official-api` | перенесён, подтверждён server-side, смёржен в `main` |
| `10_MODULE__FIN_REPORT_DAILY_BLOCK.md` | `fin_report_daily_block` | `official-api` | перенесён, подтверждён server-side, смёржен в `main` |
| `11_MODULE__PROMO_BY_PRICE_BLOCK.md` | `promo_by_price_block` | `rule-based` | перенесён, проверен, смёржен в `main` |
| `12_MODULE__COGS_BY_GROUP_BLOCK.md` | `cogs_by_group_block` | `rule-based` | перенесён, проверен, смёржен в `main` |
| `13_MODULE__SKU_DISPLAY_BUNDLE_BLOCK.md` | `sku_display_bundle_block` | `table-facing` | перенесён, проверен, смёржен в `main` |
| `14_MODULE__TABLE_PROJECTION_BUNDLE_BLOCK.md` | `table_projection_bundle_block` | `projection` | перенесён, проверен, смёржен в `main` |
| `15_MODULE__WIDE_DATA_MATRIX_V1_FIXTURE_BLOCK.md` | `wide_data_matrix_v1_fixture_block` | `wide-matrix` | перенесён, проверен, смёржен в `main` |
| `16_MODULE__WIDE_DATA_MATRIX_DELIVERY_BUNDLE_V1_BLOCK.md` | `wide_data_matrix_delivery_bundle_v1_block` | `delivery` | перенесён, проверен, смёржен в `main` |
| `17_MODULE__SHEET_VITRINA_V1_SCAFFOLD_BLOCK.md` | `sheet_vitrina_v1_scaffold_block` | `sheet-side` | перенесён, проверен, смёржен в `main` |
| `18_MODULE__SHEET_VITRINA_V1_WRITE_BRIDGE_BLOCK.md` | `sheet_vitrina_v1_write_bridge_block` | `sheet-side` | live bridge подтверждён, смёржен в `main` |
| `19_MODULE__SHEET_VITRINA_V1_PRESENTATION_BLOCK.md` | `sheet_vitrina_v1_presentation_block` | `sheet-side` | live formatting подтверждён, смёржен в `main` |
| `20_MODULE__REGISTRY_UPLOAD_BUNDLE_V1_BLOCK.md` | `registry_upload_bundle_v1_block` | `registry` | artifact-backed bundle и validator подтверждены, смёржены в `main` |
| `21_MODULE__REGISTRY_UPLOAD_FILE_BACKED_SERVICE_BLOCK.md` | `registry_upload_file_backed_service_block` | `registry` | file-backed accept/store/activate/result подтверждены, смёржены в `main` |
| `22_MODULE__REGISTRY_UPLOAD_DB_BACKED_RUNTIME_BLOCK.md` | `registry_upload_db_backed_runtime_block` | `registry` | DB-backed runtime ingest и current truth подтверждены, смёржены в `main` |
| `23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md` | `registry_upload_http_entrypoint_block` | `registry` | live HTTP entrypoint и thin runtime wiring подтверждены, смёржены в `main` |
| `24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md` | `sheet_vitrina_v1_registry_upload_trigger_block` | `sheet-side` | operator-facing bundle upload trigger подтверждён, смёржен в `main` |
| `25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md` | `sheet_vitrina_v1_registry_seed_v3_bootstrap_block` | `sheet-side` | compact v3 bootstrap подтверждён, смёржен в `main` |
| `26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md` | `sheet_vitrina_v1_mvp_end_to_end_block` | `sheet-side` | первый bounded end-to-end MVP подтверждён и смёржен в `main` |

# 5. Как эта папка используется дальше

- при добавлении нового модульного документа обновлять этот файл вместе с соответствующим `NN_MODULE__*.md`;
- считать этот файл navigation entrypoint для пакета `docs/modules/`;
- не документировать здесь как часть `main` блоки, которые существуют только на незамёрженных ветках или ещё не дошли до merge;
- не дублировать полный канонический модульный текст в других местах репозитория.
