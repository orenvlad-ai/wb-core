---
title: "Паспорт проекта WebCore"
doc_id: "WB-CORE-PROJECT-01-PASSPORT"
doc_type: "project_passport"
status: "active"
purpose: "Дать retrieval-friendly summary текущего состояния `wb-core` без полного чтения всех authoritative docs."
scope: "Project identity, main-confirmed contour, текущие bounded capabilities, открытые gaps и жёсткие границы текущего checkpoint."
source_basis:
  - "README.md"
  - "docs/architecture/00_migration_charter.md"
  - "docs/architecture/01_target_architecture.md"
  - "docs/modules/00_INDEX__MODULES.md"
source_of_truth_level: "derived_secondary_project_pack"
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
  - "docs/modules/29_MODULE__WEB_VITRINA_VIEW_MODEL_BLOCK.md"
  - "docs/modules/30_MODULE__WEB_VITRINA_GRAVITY_TABLE_ADAPTER_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
related_paths:
  - "packages/"
  - "apps/"
  - "gas/sheet_vitrina_v1/ (legacy/export-only for current web-vitrina work)"
  - "registry/"
update_triggers:
  - "изменение current main-confirmed contour"
  - "merge нового bounded модуля"
  - "смена главного project gap"
built_from_commit: "c8faa36b1eec440925a8c98b5d87eb188e5e7492"
---

# Summary

`wb-core` — canonical target-core repo для controlled sidecar migration проекта WB.

Текущий main-confirmed checkpoint:
- source/data blocks `01–12` уже смёржены;
- table/projection/wide and archived sheet-side `13–19` уже смёржены;
- registry upload line `20–23` уже смёржена;
- sheet-side operator line `24–26` уже смёржена как legacy/export contour, но Google Sheets/GAS side is now `ARCHIVED / DO NOT USE`;
- bounded browser-capture collector `27` уже смёржен как repo-owned local promo XLSX runner с truthful sidecar contract;
- bounded live wiring `28` уже смёржен и переводит `promo_by_price` из blocked gap в current server-owned source seam внутри existing refresh/runtime/read-side contour.
- web-vitrina line `29–31` уже смёржена и является active user-facing surface: `/sheet-vitrina-v1/vitrina` + `/v1/sheet-vitrina-v1/web-vitrina`.
- current operator UI unified вокруг `/sheet-vitrina-v1/vitrina`: first tab `Витрина`, sibling tabs `Расчет поставок` и `Отчеты`; `/sheet-vitrina-v1/operator` остаётся compatibility entry на тот же shell.

# Current norm

## Project identity

- canonical repo: `wb-core`
- внешнее project label: `WebCore`
- legacy repos: `wb-table-audit`, `wb-ai-research`, `wb-web-bot`
- legacy status: maintenance-only

## Main-confirmed contour

Confirmed contour на текущем `main`:
- `sku_display -> table_projection -> registry_pilot -> wide_matrix -> delivery -> sheet_scaffold`
- `registry upload -> compact seed -> bounded refresh/read -> web_vitrina_contract -> view_model -> gravity_adapter -> page_composition`
- legacy/export contour remains only as archive/migration boundary: `live write -> presentation -> sheet trigger -> reverse-load` is guarded and must not be used as runtime/update/write/load/verify target.

## Что уже materialized

- registry upload bundle и validator;
- file-backed accept/store/activate;
- DB-backed runtime/current truth;
- live HTTP entrypoint;
- archived Apps Script trigger and compact seed bootstrap for `CONFIG / METRICS / FORMULAS`;
- server-side uploaded compact package/runtime state `33 / 102 / 7`;
- server-side refresh/read ready snapshot in repo-owned SQLite runtime contour; Google Sheets reverse-load is archived.
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
- unified web-vitrina/operator surface:
  - primary manual action `Загрузить и обновить` refreshes server-side ready snapshot without Google Sheets `/load`;
  - compact table toolbar combines period/search/filter/column/sort controls; default no-query history opens the latest four server-readable business dates ending on backend-owned `today_current_date` when available;
  - bottom `Загрузка данных` is lazy: initial state shows only `not_loaded` + `Загрузить`, then explicit read-only `surface=page_composition&include_source_status=1` loads grouped source status table (`WB API`, `Seller Portal / бот`, `Прочие источники`) with date-scoped `Обновить группу`;
  - `GET /v1/sheet-vitrina-v1/plan-report` adds read-only `Выполнение плана` over accepted closed-day `fin_report_daily.fin_buyout_rub` + `ads_compact.ads_sum`, H1/H2 plan params, per-block coverage and optional server-side monthly baseline;
  - plan-report baseline routes (`baseline-template.xlsx`, `baseline-upload`, `baseline-status`) store operator monthly aggregates in separate runtime SQLite state used only by the plan report;
  - one-off `apps/sheet_vitrina_v1_ready_fact_reconcile.py` can dry-run/apply missing accepted slots from already persisted ready snapshots without overwriting existing diffs or fabricating zeros;
  - `GET /v1/sheet-vitrina-v1/stock-report` remains read-only previous-closed stock report with current active SKU selector;
  - seller-funnel materialization filters raw rows to enabled/relevant `nm_ids` before strict field validation and logs ignored invalid non-relevant rows.
- User-facing `ЕБД` / `единая база данных` now means the shared server-side accepted truth/runtime layer behind web-vitrina, plan-report and future reports; it is not Google Sheets/GAS, browser UI/localStorage or a private report-only manual table.

## Authoritative source of truth

Нормы и статусы считаются canonical только если они зафиксированы в:
- `README.md`
- `docs/architecture/*`
- `docs/modules/*`
- `migration/*`

`wb_core_docs_master` лишь повторно упаковывает это знание в compact retrieval-oriented форму.

# Known gaps

- full legacy parity по всем historical metric sections и registry rows;
- repo-owned hosted deploy/probe contract around current website/operator runtime is documented, while actual deploy rights/publish hardening remain a separate completion boundary;
- окончательная судьба `AI_EXPORT` как compatibility contract;
- materialized `packages/domain`, `infra/`, `tests/`, `api/`, `jobs/`, `db/`.

# Not in scope

- перенос всего legacy 1:1;
- утверждение, что actual deploy rights или final production hardening уже materialized в repo;
- broad operator UX redesign beyond the current unified vitrina/supply/reports shell;
- полный architectural redesign `wb-core`.
