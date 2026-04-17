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
related_paths:
  - "packages/"
  - "apps/"
  - "gas/sheet_vitrina_v1/"
  - "registry/"
update_triggers:
  - "изменение current main-confirmed contour"
  - "merge нового bounded модуля"
  - "смена главного project gap"
built_from_commit: "0b9cd8078fca3f3f4ad7325768fef4b31cb87c7e"
---

# Summary

`wb-core` — canonical target-core repo для controlled sidecar migration проекта WB.

Текущий main-confirmed checkpoint:
- source/data blocks `01–12` уже смёржены;
- table/projection/wide/sheet read-side `13–19` уже смёржены;
- registry upload line `20–23` уже смёржена;
- sheet-side operator line `24–26` уже смёржена, включая первый bounded MVP `prepare -> upload -> refresh -> load`.

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

## Primary source of truth

Нормы и статусы считаются canonical только если они зафиксированы в:
- `README.md`
- `docs/architecture/*`
- `docs/modules/*`
- `migration/*`

`wb_core_docs_master` лишь повторно упаковывает это знание в compact retrieval-oriented форму.

# Known gaps

- full legacy parity по всем historical metric sections и registry rows;
- live numeric fill для promo-backed metrics и других bounded long-tail gaps beyond current `COST_PRICE` overlay;
- repo-owned hosted deploy/probe contract вокруг upload/load runtime;
- окончательная судьба `AI_EXPORT` как compatibility contract;
- materialized `packages/domain`, `infra/`, `tests/`, `api/`, `jobs/`, `db/`.

# Not in scope

- перенос всего legacy 1:1;
- утверждение, что actual deploy rights или final production hardening уже materialized в repo;
- operator UX redesign;
- полный architectural redesign `wb-core`.
