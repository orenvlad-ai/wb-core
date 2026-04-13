---
title: "Индекс канонической модульной документации wb-core"
doc_id: "WB-CORE-MODULE-00-INDEX"
doc_type: "index"
status: "active"
purpose: "Дать единый navigation entrypoint для канонической модульной документации `wb-core`."
scope: "Папка `docs/modules/`, её naming rules, статус source of truth и полный список модульных документов `01–20`."
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
source_of_truth_level: "navigation_only"
update_note: "Обновлён под фактическое состояние `origin/main` после merge `registry_upload_bundle_v1_block`: индекс отражает полный комплект смёрженных модулей `01–20`, current main-confirmed contour и не смешивает уже смёрженный upload bundle с ещё не собранным server-side ingest runtime."
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

На текущем `origin/main` смёржены канонические модульные блоки `01–20`.

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

Главный незакрытый gap текущего `main`:
- artifact-backed upload bundle уже находится в `main`;
- server-side ingest, version storage и activation runtime для registry upload ещё не входят в текущий `main`.

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

# 5. Как эта папка используется дальше

- при добавлении нового модульного документа обновлять этот файл вместе с соответствующим `NN_MODULE__*.md`;
- считать этот файл navigation entrypoint для пакета `docs/modules/`;
- не документировать здесь как часть `main` блоки, которые существуют только на незамёрженных ветках или ещё не дошли до merge;
- не дублировать полный канонический модульный текст в других местах репозитория.
