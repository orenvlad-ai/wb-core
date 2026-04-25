---
title: "Register: legacy to WebCore map"
doc_id: "WB-CORE-PROJECT-06-LEGACY-MAP"
doc_type: "register"
status: "active"
purpose: "Сохранить тонкую карту происхождения функций и migration boundaries между legacy-контурами и `wb-core`."
scope: "Legacy repos, ключевые sheet/server surfaces, current owner в `wb-core`, текущий статус переноса и explicit gaps."
source_basis:
  - "README.md"
  - "docs/architecture/00_migration_charter.md"
  - "docs/architecture/01_target_architecture.md"
  - "docs/modules/13_MODULE__SKU_DISPLAY_BUNDLE_BLOCK.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/27_MODULE__PROMO_XLSX_COLLECTOR_BLOCK.md"
  - "docs/modules/28_MODULE__PROMO_LIVE_SOURCE_WIRING_BLOCK.md"
source_of_truth_level: "derived_secondary_project_pack"
related_docs:
  - "README.md"
  - "docs/architecture/00_migration_charter.md"
  - "docs/architecture/01_target_architecture.md"
  - "docs/modules/13_MODULE__SKU_DISPLAY_BUNDLE_BLOCK.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/27_MODULE__PROMO_XLSX_COLLECTOR_BLOCK.md"
  - "docs/modules/28_MODULE__PROMO_LIVE_SOURCE_WIRING_BLOCK.md"
update_triggers:
  - "перенос новой legacy capability"
  - "изменение migration boundary"
  - "закрытие крупного compatibility gap"
built_from_commit: "c8faa36b1eec440925a8c98b5d87eb188e5e7492"
---

# Summary

Этот register нужен не для переноса legacy-кода, а для ответа на вопрос:
"где теперь живёт смысл старой функции и что ещё не перенесено?"

# Current norm

| Legacy surface | Current owner в `wb-core` | Current status | Boundary note |
| --- | --- | --- | --- |
| `wb-table-audit` Apps Script operator shell | `gas/sheet_vitrina_v1/*` + website/operator `sheet_vitrina_v1` | archived for Google Sheets; current operator is unified website/public web-vitrina | former sheet-side contour is migration evidence with archive guards, not active UI/update/write/verify target; `/sheet-vitrina-v1/vitrina` is the primary current UI and `/sheet-vitrina-v1/operator` is compatibility entry |
| legacy `CONFIG` | `sheet_vitrina_v1_registry_seed_v3_bootstrap_block` + `registry_upload_bundle_v1_block` | перенесён в compact V2/V3 form | не равен full legacy `CONFIG` 1:1 |
| legacy `METRICS` | `sheet_vitrina_v1_registry_seed_v3_bootstrap_block` + `sheet_vitrina_v1_mvp_end_to_end_block` | uploaded compact package перенесён | historical sheet/upload dictionary materialized `102` rows; current truth / server plan держат `95` enabled+show_in_data metrics, а website/operator web-vitrina reads the same server-driven ready snapshot |
| legacy `FORMULAS` | `sheet_vitrina_v1_registry_seed_v3_bootstrap_block` + `registry_upload_bundle_v1_block` | current uploaded set перенесён | historical sheet-side seed and upload bundle держат `7` formulas rows, нужных authoritative `metrics_v2`; Google Sheets seed is archived |
| legacy `DATA`/vitrina readback | `sheet_vitrina_v1_mvp_end_to_end_block` + `promo_live_source_wiring_block` | bounded replacement есть | rows materialize-ятся по uploaded package; `COST_PRICE` overlay и promo-backed `promo_by_price` rows уже server-side integrated в current refresh/runtime/read-side contour |
| legacy report historical fact gaps | accepted temporal slots + `manual_monthly_plan_report_baseline` + one-off ready-fact reconcile | bounded server-side replacement есть | plan-report may use controlled monthly XLSX baseline only for full-month aggregates; ready snapshots may be one-off reconcile input for missing accepted `fin_report_daily` / `ads_compact` slots; neither path revives Google Sheets/GAS as report truth |
| legacy `AI_EXPORT` | отдельного полного replacement пока нет | open gap | compatibility boundary ещё не закрыт |
| `wb-ai-research` ingest/runtime вокруг registry | `registry_upload_file_backed_service_block`, `registry_upload_db_backed_runtime_block`, `registry_upload_http_entrypoint_block` | перенесено bounded chain-ом | repo-owned deploy/probe contract есть, actual deploy rights/hardening остаются отдельно |
| `wb-ai-research` snapshot consumers | source/data blocks `01–10` | largely migrated | current repo owns contracts/artifacts/smokes |
| `wb-web-bot` browser web-source capture | `web_source_snapshot_block` consumer boundary + `promo_xlsx_collector_block` precursor + `promo_live_source_wiring_block` | bounded thin adapter boundary materialized | wb-core now owns canonical hydration/modal/drawer semantics, sidecar contract, workbook inspection and live promo source wiring, but not the whole browser runtime |

## Boundary rules

- `wb-core` не обязан копировать legacy 1:1, если bounded replacement уже зафиксирован contract-first образом.
- Legacy knowledge сохраняется здесь как map, а не как code dump.
- Если legacy surface пока не replaced, это должно фиксироваться как `open gap`, а не маскироваться под "уже перенесено".

# Known gaps

- full parity beyond current uploaded compact package и long-tail registry rows;
- repo-owned promo collector output уже wire-ится в current live metric/read-side line for `promo_by_price`; open tail остаётся только beyond the current wired promo-backed metric subset and beyond current `COST_PRICE` overlay;
- current reports are website/server-owned: `daily-report`, `stock-report` and `plan-report` are not a revived sheet-side reporting truth layer; user-facing `ЕБД` means the shared server-side accepted truth/runtime layer, not Google Sheets/GAS or browser-local state;
- окончательная судьба `AI_EXPORT`;
- actual production-grade rights/wiring/hardening вокруг уже repo-owned hosted deploy contract.

# Not in scope

- Перенос legacy source files.
- Полный audit legacy-репозиториев.
- Runtime archaeology по reference repos вне уже зафиксированных migration boundaries.
