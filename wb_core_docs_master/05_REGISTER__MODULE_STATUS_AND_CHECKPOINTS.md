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
built_from_commit: "5db3548de01b2299c4f003ad43074f367d3050c8"
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
- page uses `POST /v1/sheet-vitrina-v1/refresh` and `GET /v1/sheet-vitrina-v1/status`
- page stays intentionally narrow: one button `Загрузить данные`, compact status, minimal result/log
- server-side business timezone = `Asia/Yekaterinburg` for default `as_of_date`, `today_current` and operator-facing freshness dates
- live daily auto-refresh = `wb-core-sheet-vitrina-refresh.timer` -> existing `POST /v1/sheet-vitrina-v1/refresh` at `11:00 Asia/Yekaterinburg` (`06:00 UTC` on current host)

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
- unresolved long-tail compatibility around `AI_EXPORT`.

# Not in scope

- Полные module doc narratives.
- Artifact/evidence details по каждому модулю.
- Operational deploy status как canonical repo fact.
