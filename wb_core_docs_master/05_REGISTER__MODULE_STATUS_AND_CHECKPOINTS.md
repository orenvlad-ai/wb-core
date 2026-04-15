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
built_from_commit: "23491a0b8313e45403ac6b4afdb8f7bd0a178134"
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

Current repo-owned operator refresh surface:
- `GET /sheet-vitrina-v1/operator`
- page uses `POST /v1/sheet-vitrina-v1/refresh` and `GET /v1/sheet-vitrina-v1/status`
- page stays intentionally narrow: one button `Загрузить данные`, compact status, minimal result/log

Current main-confirmed counts для этого flow:
- prepare/upload package = `33 / 102 / 7`
- current truth / ready snapshot displayed metrics = `95`
- refresh materialize-ит date-aware ready snapshot `yesterday_closed + today_current`
- operator-facing `DATA_VITRINA` = server-driven two-day `date_matrix` `1698` rendered rows / `95` metric keys (`1631` source rows, `34` blocks)
- operator-facing `STATUS` = per-source/per-slot freshness surface; current-only sources (`stocks`, `prices_snapshot`, `ads_bids`) показывают `not_available` для `yesterday_closed`, а не backfill

This is the first bounded MVP checkpoint, not final production parity.

# Known gaps

- full legacy parity beyond current main-confirmed sheet/upload dictionary;
- live numeric fill для promo/cogs-backed metrics до появления live HTTP adapters;
- отдельный bounded fix по распределению `stocks` в Южный / Северо-Кавказский ФО, если проблема сохранится после date-aware model;
- production hardening around runtime/deploy/auth;
- unresolved long-tail compatibility around `AI_EXPORT`.

# Not in scope

- Полные module doc narratives.
- Artifact/evidence details по каждому модулю.
- Operational deploy status как canonical repo fact.
