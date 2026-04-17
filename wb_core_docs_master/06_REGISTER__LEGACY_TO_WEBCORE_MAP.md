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
source_of_truth_level: "secondary_project_pack"
related_docs:
  - "README.md"
  - "docs/architecture/00_migration_charter.md"
  - "docs/architecture/01_target_architecture.md"
  - "docs/modules/13_MODULE__SKU_DISPLAY_BUNDLE_BLOCK.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
update_triggers:
  - "перенос новой legacy capability"
  - "изменение migration boundary"
  - "закрытие крупного compatibility gap"
built_from_commit: "0b9cd8078fca3f3f4ad7325768fef4b31cb87c7e"
---

# Summary

Этот register нужен не для переноса legacy-кода, а для ответа на вопрос:
"где теперь живёт смысл старой функции и что ещё не перенесено?"

# Current norm

| Legacy surface | Current owner в `wb-core` | Current status | Boundary note |
| --- | --- | --- | --- |
| `wb-table-audit` Apps Script operator shell | `gas/sheet_vitrina_v1/*` | частично перенесён | новый sheet-side contour materialized, но не весь legacy UI |
| legacy `CONFIG` | `sheet_vitrina_v1_registry_seed_v3_bootstrap_block` + `registry_upload_bundle_v1_block` | перенесён в compact V2/V3 form | не равен full legacy `CONFIG` 1:1 |
| legacy `METRICS` | `sheet_vitrina_v1_registry_seed_v3_bootstrap_block` + `sheet_vitrina_v1_mvp_end_to_end_block` | uploaded compact package перенесён | sheet/upload dictionary materialize-ит `102` rows; current truth / server plan держат `95` enabled+show_in_data metrics, а operator-facing `DATA_VITRINA` materialize-ит тот же server-driven set как thin `date_matrix` |
| legacy `FORMULAS` | `sheet_vitrina_v1_registry_seed_v3_bootstrap_block` + `registry_upload_bundle_v1_block` | current uploaded set перенесён | sheet-side seed и upload bundle держат `7` formulas rows, нужных authoritative `metrics_v2` |
| legacy `DATA`/vitrina readback | `sheet_vitrina_v1_mvp_end_to_end_block` | bounded replacement есть | rows materialize-ятся по uploaded package; `COST_PRICE` overlay уже server-side integrated, open gap остаётся у promo-backed и других long-tail live rows |
| legacy `AI_EXPORT` | отдельного полного replacement пока нет | open gap | compatibility boundary ещё не закрыт |
| `wb-ai-research` ingest/runtime вокруг registry | `registry_upload_file_backed_service_block`, `registry_upload_db_backed_runtime_block`, `registry_upload_http_entrypoint_block` | перенесено bounded chain-ом | repo-owned deploy/probe contract есть, actual deploy rights/hardening остаются отдельно |
| `wb-ai-research` snapshot consumers | source/data blocks `01–10` | largely migrated | current repo owns contracts/artifacts/smokes |
| `wb-web-bot` browser web-source capture | `web_source_snapshot_block` consumer boundary | thin adapter boundary only | browser internals не перенесены как domain logic |

## Boundary rules

- `wb-core` не обязан копировать legacy 1:1, если bounded replacement уже зафиксирован contract-first образом.
- Legacy knowledge сохраняется здесь как map, а не как code dump.
- Если legacy surface пока не replaced, это должно фиксироваться как `open gap`, а не маскироваться под "уже перенесено".

# Known gaps

- full parity beyond current uploaded compact package и long-tail registry rows;
- live numeric fill для promo-backed metrics и других bounded long-tail rows beyond current `COST_PRICE` overlay;
- окончательная судьба `AI_EXPORT`;
- actual production-grade rights/wiring/hardening вокруг уже repo-owned hosted deploy contract.

# Not in scope

- Перенос legacy source files.
- Полный audit legacy-репозиториев.
- Runtime archaeology по reference repos вне уже зафиксированных migration boundaries.
