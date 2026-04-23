---
title: "Паспорт проекта WebCore"
doc_id: "WB-CORE-PROJECT-01-PASSPORT"
doc_type: "project_passport"
status: "active"
purpose: "Дать retrieval-friendly summary текущего состояния `wb-core` без полного чтения всех primary docs."
scope: "Project identity, main-confirmed contour, текущие bounded capabilities, открытые gaps и жёсткие границы текущего checkpoint."
source_basis:
  - "README.md"
  - "docs/architecture/00_migration_charter.md"
  - "docs/architecture/01_target_architecture.md"
  - "docs/modules/00_INDEX__MODULES.md"
source_of_truth_level: "secondary_project_pack"
related_docs:
  - "README.md"
  - "docs/architecture/00_migration_charter.md"
  - "docs/architecture/01_target_architecture.md"
  - "docs/modules/00_INDEX__MODULES.md"
related_modules:
  - "docs/modules/20_MODULE__REGISTRY_UPLOAD_BUNDLE_V1_BLOCK.md"
  - "docs/modules/21_MODULE__REGISTRY_UPLOAD_FILE_BACKED_SERVICE_BLOCK.md"
  - "docs/modules/22_MODULE__REGISTRY_UPLOAD_DB_BACKED_RUNTIME_BLOCK.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/27_MODULE__PROMO_XLSX_COLLECTOR_BLOCK.md"
  - "docs/modules/28_MODULE__PROMO_LIVE_SOURCE_WIRING_BLOCK.md"
related_paths:
  - "packages/"
  - "apps/"
  - "gas/sheet_vitrina_v1/"
  - "registry/"
update_triggers:
  - "изменение current main-confirmed contour"
  - "merge нового bounded модуля"
  - "смена главного project gap"
built_from_commit: "967edcc2059b36db36a3846d9f773c0b90e20f90"
---

# Summary

`wb-core` — canonical target-core repo для controlled sidecar migration проекта WB.

Текущий main-confirmed checkpoint:
- source/data blocks `01–12` уже смёржены;
- table/projection/wide/sheet read-side `13–19` уже смёржены;
- registry upload line `20–23` уже смёржена;
- sheet-side operator line `24–26` уже смёржена, включая первый bounded MVP `prepare -> upload -> refresh -> load`;
- bounded browser-capture collector `27` уже смёржен как repo-owned local promo XLSX runner с truthful sidecar contract;
- bounded live wiring `28` уже смёржен и переводит `promo_by_price` из blocked gap в current server-owned source seam внутри existing refresh/runtime/read-side contour.

# Current norm

## Project identity

- canonical repo: `wb-core`
- внешнее project label: `WebCore`
- legacy repos: `wb-table-audit`, `wb-ai-research`, `wb-web-bot`
- legacy status: maintenance-only

## Main-confirmed contour

Confirmed contour на текущем `main`:
- `sku_display -> table_projection -> registry_pilot -> wide_matrix -> delivery -> sheet_scaffold`
- `live write -> presentation -> registry upload -> sheet trigger -> compact seed -> bounded refresh/read reverse-load`

## Что уже materialized

- registry upload bundle и validator;
- file-backed accept/store/activate;
- DB-backed runtime/current truth;
- live HTTP entrypoint;
- Apps Script trigger `Отправить реестры на сервер`;
- compact seed bootstrap для `CONFIG / METRICS / FORMULAS`;
- выравнивание sheet/upload/runtime под uploaded compact package `33 / 102 / 7`;
- bounded refresh/read reverse-load в `DATA_VITRINA` и `STATUS`, где ready snapshot хранится в repo-owned SQLite runtime contour.
- repo-owned bounded `promo_xlsx_collector_block`:
  - canonical `direct_open -> cookie -> hydrated DOM -> optional modal close`
  - canonical drawer reset inside `#Portal-drawer`
  - truthful `metadata.json` for every promo
  - archive-first workbook reuse for unchanged campaigns
  - workbook inspection and export-kind classification for downloaded/reused promo XLSX.
- repo-owned bounded `promo_live_source_wiring_block`:
  - `promo_by_price[today_current]` materialize-ится через repo-owned archive-first collector run
  - `promo_by_price[yesterday_closed]` на corrective refresh сначала пересчитывается server-side interval replay из archived campaign artifacts
  - accepted/runtime-cached exact-date promo truth используется только как fallback, если replay не дал exact `success`
  - invalid later attempts do not overwrite accepted current/closed promo truth
  - low-confidence cross-year labels keep `promo_start_at/end_at = null`

## Primary source of truth

Нормы и статусы считаются canonical только если они зафиксированы в:
- `README.md`
- `docs/architecture/*`
- `docs/modules/*`
- `migration/*`

`wb_core_docs_master` лишь повторно упаковывает это знание в compact retrieval-oriented форму.

# Known gaps

- full legacy parity по всем historical metric sections и registry rows;
- repo-owned hosted deploy/probe contract вокруг upload/load runtime;
- окончательная судьба `AI_EXPORT` как compatibility contract;
- materialized `packages/domain`, `infra/`, `tests/`, `api/`, `jobs/`, `db/`.

# Not in scope

- перенос всего legacy 1:1;
- утверждение, что actual deploy rights или final production hardening уже materialized в repo;
- operator UX redesign;
- полный architectural redesign `wb-core`.
