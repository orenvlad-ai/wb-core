---
title: "Glossary: terms, aliases and canonical names"
doc_id: "WB-CORE-PROJECT-03-GLOSSARY"
doc_type: "glossary"
status: "active"
purpose: "Зафиксировать компактный словарь терминов и canonical names для project retrieval без путаницы между legacy и `wb-core`."
scope: "Имена проекта, sheet-side сущности, registry terms, repo aliases и naming rules."
source_basis:
  - "README.md"
  - "docs/architecture/01_target_architecture.md"
  - "docs/modules/00_INDEX__MODULES.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
source_of_truth_level: "derived_secondary_project_pack"
related_docs:
  - "README.md"
  - "docs/architecture/01_target_architecture.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
update_triggers:
  - "изменение canonical naming"
  - "появление нового публичного термина"
  - "изменение operator-visible labels"
built_from_commit: "c8faa36b1eec440925a8c98b5d87eb188e5e7492"
---

# Summary

Этот glossary нужен для трёх вещей:
- не путать `wb-core` и внешнее label `WebCore`;
- не смешивать legacy sheet names и current canonical names;
- держать retrieval запросы в одном словаре.

# Current norm

| Canonical name | Допустимые aliases | Норма использования |
| --- | --- | --- |
| `wb-core` | `WebCore`, `WB Core` | repo и canonical project id |
| `wb_core_docs_master` | `docs master`, `project pack` | derived secondary compact project-pack |
| `VB-Core Витрина V1` | `WB Core Vitrina V1`, `sheet_vitrina_v1` | legacy Google Sheets contour; archived / do not use |
| `sheet_vitrina_v1` | `sheet_vitrina`, `Vitrina V1` | current website/operator/web-vitrina server contour; GAS part is archive-only |
| `/sheet-vitrina-v1/vitrina` | `Витрина`, `unified UI` | primary current user-facing website entrypoint for vitrina, supply and reports tabs |
| `/sheet-vitrina-v1/operator` | `operator compatibility entry` | compatibility route rendering the same unified shell, not a separate current truth owner |
| `CONFIG / METRICS / FORMULAS` | `operator sheets`, `registry sheets` | former sheet-side input реестры; archive/migration-only |
| `config_v2 / metrics_v2 / formulas_v2` | `CONFIG_V2 / METRICS_V2 / FORMULAS_V2` | canonical bundle/runtime arrays |
| `uploaded compact package` | `current authoritative registry package`, `uploaded package` | canonical repo-owned input для текущего набора `33 / 102 / 7` |
| `RegistryUploadResult` | `upload result` | canonical response shape upload path |
| `current truth` | `current state`, `active registry state` | current server-side accepted registry version |
| `DATA_VITRINA` | `vitrina data sheet` | former Google Sheets readback sheet; archive/migration-only |
| `STATUS` | `status sheet` | former Google Sheets freshness/source sheet; archive/migration-only |
| `refresh -> web-vitrina read` | `current operator flow`, `current web-vitrina flow` | canonical current bounded operator/server scenario |
| `group-refresh` | `Обновить группу`, `source group refresh` | date-scoped `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh` for one source group and one selected date |
| `plan-report` | `Выполнение плана` | read-only operator report over persisted closed-day facts, H1/H2 plan values and optional server-side monthly baseline |
| `manual_monthly_plan_report_baseline` | `Исторические данные для отчёта`, `baseline` | separate runtime SQLite source used only by plan-report for full-month operator XLSX aggregates; not a general historical backfill |
| `ЕБД` | `единая база данных` | user-facing alias for shared server-side accepted truth/runtime layer `wb-core`; not Google Sheets/GAS, browser UI, localStorage or report-private manual state |
| `stock-report` | `Отчёт по остаткам` | read-only previous-closed stock report with active SKU selector |
| `prepare -> upload -> refresh -> load` | `MVP flow`, `end-to-end flow` | historical bounded Google Sheets scenario; archived / do not use |
| `ready snapshot` | `materialized snapshot`, `persisted sheet plan` | persisted server-side read-model for `DATA_VITRINA` / `STATUS` |
| `ready-fact reconcile` | `historical report reconcile` | one-off repo-owned dry-run/apply helper that inserts missing accepted `fin_report_daily` / `ads_compact` slots from already persisted ready snapshots without overwrites or fake zeros |
| `yesterday_closed / today_current` | `temporal slots`, `date columns` | server-owned bounded two-day temporal slots inside current `sheet_vitrina_v1` ready snapshot, counted in canonical business timezone `Asia/Yekaterinburg` |
| `AI_EXPORT` | `legacy export` | compatibility/open-gap term, не новый canonical target |

## Naming notes

- Для файлов и ids используется ASCII и machine-friendly style.
- Для пользовательских sheet/menu labels допустим русский UI-text.
- Для module ids canonical форма всегда snake_case + `_block`.
- `openCount` и `open_card_count` — разные canonical metric keys; auto-merge между ними запрещён.

# Known gaps

- Final production naming для будущих hosted/runtime/deploy слоёв ещё не зафиксирован.
- Текущий main-confirmed uploaded package уже фиксируется как `102` metrics rows / `95` enabled+show_in_data metric keys в current truth; operator-facing `DATA_VITRINA` при этом materialize-ит тот же server-driven row set как thin two-day `date_matrix` (`1631` source rows -> `1698` rendered rows на `yesterday_closed + today_current`) без локального subset path.
- User-facing labels for current web-vitrina are now centralized around `Витрина`, `Загрузить и обновить`, `Загрузка данных`, `Обновить группу`, `Отчёты`, `Отчёт по остаткам`, `Выполнение плана`, `Исторические данные для отчёта` and `ЕБД`.

# Not in scope

- Полный словарь каждого internal field.
- Исторические aliases из всего legacy-корпуса.
