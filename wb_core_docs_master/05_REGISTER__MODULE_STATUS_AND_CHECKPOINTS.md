---
title: "Register: module status and checkpoints"
doc_id: "WB-CORE-PROJECT-05-MODULE-STATUS"
doc_type: "register"
status: "active"
purpose: "Дать compact register смёрженных модулей и current checkpoints без чтения всех module docs подряд."
scope: "Семейства модулей, диапазоны `01–26`, текущий статус `main`, главный current checkpoint и открытые хвосты."
source_basis:
  - "docs/modules/00_INDEX__MODULES.md"
  - "README.md"
  - "docs/architecture/00_migration_charter.md"
  - "docs/architecture/01_target_architecture.md"
source_of_truth_level: "secondary_project_pack"
related_docs:
  - "docs/modules/00_INDEX__MODULES.md"
  - "README.md"
  - "docs/architecture/00_migration_charter.md"
  - "docs/architecture/01_target_architecture.md"
update_triggers:
  - "merge нового модуля"
  - "изменение main-confirmed checkpoint"
  - "смена статуса family/gap"
built_from_commit: "b76bd8145a18d0da45bc6018aea31ce373116173"
---

# Summary

На текущем `main` main-confirmed module set уже доходит до `26`.

Практически это значит:
- source/data foundation уже materialized;
- registry upload line уже замкнута до HTTP entrypoint;
- sheet-side line уже дошла до bounded MVP `prepare -> upload -> refresh -> load`.

# Current norm

| Range | Family | Current status |
| --- | --- | --- |
| `01–10` | `web-source` + `official-api` | смёржены в `main`, bounded source blocks подтверждены |
| `11–12` | `rule-based` | смёржены в `main` |
| `13–19` | `table-facing` / `projection` / `wide-matrix` / `sheet-side scaffold` | смёржены в `main` |
| `20–23` | `registry upload line` | смёржены в `main` до live HTTP entrypoint |
| `24–26` | `sheet-side operator line` | смёржены в `main` до первого bounded MVP |

## Current checkpoint ladder

1. `sku_display_bundle_block`
2. `table_projection_bundle_block`
3. `registry_pilot_bundle`
4. `wide_data_matrix_v1_fixture_block`
5. `wide_data_matrix_delivery_bundle_v1_block`
6. `sheet_vitrina_v1_scaffold_block`
7. `sheet_vitrina_v1_write_bridge_block`
8. `sheet_vitrina_v1_presentation_block`
9. `registry_upload_bundle_v1_block`
10. `registry_upload_file_backed_service_block`
11. `registry_upload_db_backed_runtime_block`
12. `registry_upload_http_entrypoint_block`
13. `sheet_vitrina_v1_registry_upload_trigger_block`
14. `sheet_vitrina_v1_registry_seed_v3_bootstrap_block`
15. `sheet_vitrina_v1_mvp_end_to_end_block`

## Operator-facing checkpoint

Current main-confirmed operator flow:
- `Подготовить листы CONFIG / METRICS / FORMULAS`
- `Отправить реестры на сервер`
- `POST /v1/sheet-vitrina-v1/refresh`
- `Загрузить таблицу`

Current sibling operator input flow:
- `Подготовить лист COST_PRICE`
- `Отправить себестоимости`
- flow обновляет separate `COST_PRICE` authoritative dataset, а existing refresh/read contour затем использует его server-side в current `DATA_VITRINA` / `STATUS`

Current repo-owned operator refresh surface:
- `GET /sheet-vitrina-v1/operator`
- page uses `POST /v1/sheet-vitrina-v1/refresh`, `POST /v1/sheet-vitrina-v1/load`, `GET /v1/sheet-vitrina-v1/status` and `GET /v1/sheet-vitrina-v1/job`
- page stays intentionally narrow: separate buttons `Загрузить данные` / `Отправить данные`, compact status, one compact `Сервер и расписание` block and one fixed-height scrollable `Лог` block with `Скачать лог`
- status/refresh responses drive the block through `server_context`, so timezone/scheduler wording is not hardcoded in UI; `Автообновление` now truthfully describes the full daily chain `Ежедневно в 11:00 Asia/Yekaterinburg: загрузка данных + отправка данных в таблицу`
- the same block shows backend-driven `Последний автозапуск`, `Статус последнего автозапуска` and `Последнее успешное автообновление`
- `refresh` и `load` не смешиваются: refresh materialize-ит ready snapshot only, load пишет only already prepared snapshot в live sheet
- job/log surface is detailed and machine-useful: source/module/adapter/endpoint steps, source result kinds/counts, metric batch summaries and bridge/write results stay server-driven and can be exported per completed run through `GET /v1/sheet-vitrina-v1/job?job_id=...&format=text&download=1`
- server-side business timezone = `Asia/Yekaterinburg` for default `as_of_date`, `today_current` and operator-facing freshness dates
- live daily auto-refresh = `wb-core-sheet-vitrina-refresh.timer` -> existing `POST /v1/sheet-vitrina-v1/refresh` at `11:00 Asia/Yekaterinburg` (`06:00 UTC` on current host) with `auto_load=true`, so the daily path now finishes as `refresh + load to live sheet`
- strict closed-day policy applies only to closed-day-capable bot/web-source families `seller_funnel_snapshot` and `web_source_snapshot`; current-only families `prices_snapshot`, `ads_bids`, `stocks` keep truthful `not_available` for `yesterday_closed`
- `today_current` for bot/web-source may stay provisional/incomplete, but `yesterday_closed` now accepts only `accepted_closed_day_snapshot`; invalid candidate goes to persisted `closure_retrying / closure_rate_limited / closure_exhausted` instead of silently inheriting same-day provisional values
- live retry completion is bounded by repo-owned runner `apps/sheet_vitrina_v1_temporal_closure_retry_live.py` plus host timer `wb-core-sheet-vitrina-closure-retry.timer`

Current additional operator supply flow on the same page:
- top-level tab `Расчёт поставок` keeps the existing page pattern and now materializes two bounded sibling blocks: `Заказ на фабрике` and `Поставка на Wildberries`
- operator vocabulary inside these sibling blocks is unified around `period average / lead times / safety / batch / cycle`; factory now materializes `cycle_order_days`, while WB regional keeps the same math under `cycle_supply_days`
- current operator UX uses auto-upload after file selection, subtle delete icons for current uploaded files and a clickable `sheet_vitrina_v1` link to the bound live spreadsheet
- `Остатки ФФ` is a shared server-owned dataset block for both calculations; the same uploaded workbook/state is reused, not duplicated
- all operator XLSX templates in this block use Russian headers; backend keeps the machine mapping server-side
- `Остатки ФФ` is prefilled from current active SKU truth and requires exactly one row per active SKU
- `Товары в пути от фабрики` and `Товары в пути от ФФ на Wildberries` are compact event-based templates: one row = one expected inbound, duplicate `nmId` is allowed there and summed only when the planned arrival date falls within the planning horizon
- current repo had no other authoritative source for legacy parity term `FF -> WB inbound`, so the bounded flow uses a separate operator upload contract instead of silently dropping that coverage component
- operator-facing label for batch size = `Кратность штук в коробке`
- inbound files are optional for calculation; when absent or deleted, both inbound coverage terms truthfully become `0`
- each upload block now surfaces the current uploaded filename plus download/delete actions from backend state
- current UI no longer hard-caps `sales_avg_period_days`; authoritative `orderCount` history now lives server-side in `temporal_source_snapshots[source_key=sales_funnel_history]`, so any positive covered window is allowed and blocker appears only when requested range falls outside runtime coverage
- live `DATA_VITRINA` may be used only as one-time migration input for bounded historical reconcile window `2026-03-01..2026-04-18`; sheet does not become a permanent source of truth
- future exact-date sales history continues to materialize through existing refresh/runtime flow, so the historical bootstrap is bounded and not a recurring operator step
- XLSX generation is hardened so operator templates and recommendation files open as standard XLSX workbooks without a repair path
- calculation, result XLSX and `Общее количество / Расчётный вес / Расчётный объём` summary stay fully server-driven
- regional block adds server-side district allocation with truthful `deficit = full_recommendation - allocated_qty`, a compact per-district summary table and one separate XLSX file per canonical district key
- current bounded regional methodology uses total SKU `orderCount` plus current district stocks and then applies legacy box allocation against shared `stock_ff`; inbound `ФФ -> WB` is intentionally not materialized for this block in the current checkpoint

Current main-confirmed counts для этого flow:
- prepare/upload package = `33 / 102 / 7`
- current truth / ready snapshot displayed metrics = `95`
- refresh materialize-ит date-aware ready snapshot `yesterday_closed + today_current`
- operator-facing `DATA_VITRINA` = server-driven two-day `date_matrix` `1698` rendered rows / `95` metric keys (`1631` source rows, `34` blocks)
- operator-facing `STATUS` = per-source/per-slot freshness surface; current-only sources (`stocks`, `prices_snapshot`, `ads_bids`) показывают `not_available` для `yesterday_closed`, а не backfill
- bot/web-source family (`seller_funnel_snapshot`, `web_source_snapshot`) uses bounded `explicit-date -> latest-if-date-matches` reads for `today_current`; exact-date runtime cache may truthfully surface previous captured day as next `yesterday_closed`, with explicit `STATUS` note instead of slot-copying
- if exact-date `today_current` snapshot is still missing for bot/web-source family, refresh may bounded-trigger server-local same-day capture in `/opt/wb-web-bot` plus `/opt/wb-ai/run_web_source_handoff.py` before final read-side fetch
- sibling `COST_PRICE` contour = отдельный sheet/menu/upload path и separate runtime current-state seam вне compact bundle
- current operator-facing cost overlay = server-side `cost_price_rub`, `avg_cost_price_rub`, `total_proxy_profit_rub`, `proxy_margin_pct_total` с resolution `group + latest effective_from <= slot_date`

This is the first bounded MVP checkpoint, not final production parity.

# Known gaps

- full legacy parity beyond current main-confirmed sheet/upload dictionary;
- live numeric fill для promo-backed metrics и других bounded long-tail rows beyond current `COST_PRICE` overlay;
- отдельный bounded fix по любому remaining non-district / foreign stocks residual, если одной truthful `STATUS` note окажется недостаточно для operator flow;
- production hardening around runtime/deploy/auth;
- generic orchestration platform beyond current one daily timer;
- actual deploy rights/publish wiring for hosted contour;
- unresolved long-tail compatibility around `AI_EXPORT`.

# Not in scope

- Полные module doc narratives.
- Artifact/evidence details по каждому модулю.
- Hidden operational deploy knowledge вместо repo-owned contract.
