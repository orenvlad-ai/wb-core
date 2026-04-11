---
title: "Индекс канонической модульной документации wb-core"
doc_id: "WB-CORE-MODULE-00-INDEX"
doc_type: "index"
status: "active"
purpose: "Дать единый navigation entrypoint для канонической модульной документации `wb-core`."
scope: "Папка `docs/modules/`, её naming rules, статус source of truth и полный список модульных документов `01–13`."
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
source_of_truth_level: "navigation_only"
update_note: "Обновлён после добавления `sku_display_bundle_block`; теперь индекс отражает полный комплект канонических модульных документов `01–13` и остаётся единым source of truth для `docs/modules/`."
---

# 1. Назначение индекса

`docs/modules/` — это канонический source of truth для модульной документации `wb-core`.

Полные модульные документы живут здесь. В других местах репозитория могут оставаться:
- migration contracts;
- parity/checklist документы;
- evidence;
- короткие указатели.

Но канонический свод по модулю должен отражаться в `docs/modules/`.

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
| `13_MODULE__SKU_DISPLAY_BUNDLE_BLOCK.md` | `sku_display_bundle_block` | `table-facing` | перенесён, проверен, checkpoint PR |

# 5. Как эта папка используется дальше

- при добавлении нового модульного документа обновлять этот файл вместе с соответствующим `NN_MODULE__*.md`;
- считать этот файл navigation entrypoint для пакета `docs/modules/`;
- не дублировать полный канонический модульный текст в других местах репозитория.
