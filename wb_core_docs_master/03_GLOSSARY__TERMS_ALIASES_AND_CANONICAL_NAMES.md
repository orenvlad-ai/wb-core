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
built_from_commit: "e65dc30240e49651c2c660b179acbbd6b2accbd1"
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
| `/sheet-vitrina-v1/vitrina` | `Витрина`, `unified UI` | primary current user-facing website entrypoint for vitrina, supply, reports, feedbacks and research tabs |
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
| `feedbacks` | `Отзывы`, `sheet_vitrina_v1_feedbacks` | read-only official WB feedbacks route/tab; not accepted truth persistence and not complaint submission |
| `feedbacks AI` | `AI анализ отзывов`, `feedbacks/ai-prompt`, `feedbacks/ai-analyze` | transient operator review aid over loaded feedback rows via server-side prompt + OpenAI call; not `AI_EXPORT`, not ЕБД and not complaint automation |
| `feedbacks complaints` | `Жалобы`, `feedbacks/complaints`, `complaint journal` | nested `Отзывы` runtime journal/status-sync contour for complaint evidence/status; read/status routes only, not public complaint submit UI |
| `Seller Portal complaint submit` | `guarded complaint submit`, `complaint CLI runner` | CLI-only guarded real submit lane with exact match, hard caps and confirmation/detail probes; not web UI, not auto-submit and not accepted truth persistence |
| `owner runtime API` | `wb-ai-api.service`, `localhost owner API`, `127.0.0.1:8000` | EU host-local owner runtime for bot-backed web-source/seller-funnel handoff; not public nginx route and not `api.selleros.pro` surface |
| `research_sku_group_comparison` | `Исследования`, `Сравнение групп SKU` | read-only retrospective comparison of two SKU groups over persisted ready snapshots; no causal/statistical claims |
| `promo current invariant smoke` | `promo invariant guard` | read-only live/public guard for current promo row visibility and expected ended/no-download artifact handling |
| `normalized promo archive` | `campaign_rows.jsonl`, `campaign_rows_manifest.json` | normalized campaign-row truth for historical promo replay without permanent raw workbook dependency |
| `promo_refresh_light_gc_v1` | `promo artifact light GC`, `refresh-integrated GC` | bounded refresh-time cleanup after normalized archive + ready snapshot persistence; protects current/unknown/replay-critical artifacts |
| `public route allowlist` | `nginx allowlist`, `managed public routes` | repo-owned hosted nginx route publication manifest for the current HTTPS wb-core hosted contour |
| `EU hosted runtime target` | `wb-core-eu-root`, `89.191.226.88`, `current live target` | current primary hosted runtime target for deploy/probe/GC/runtime writes; must publish production HTTPS domain route |
| `selleros rollback target` | `selleros-root`, `178.72.152.177`, `old selleros VPS` | rollback-only/read-only legacy target; routine mutating writes are blocked by hosted runner guard |
| `https://api.selleros.pro` | `production URL`, `current public endpoint` | required current live public endpoint; IP-only HTTP is not an acceptable production contour |
| `api.selleros.pro` | `current live DNS name`, `production domain` | required DNS name for current EU hosted contour and nginx `server_name`; not by itself proof that a target is old selleros |
| `current-live HTTPS/TLS invariant` | `EU domain/TLS invariant`, `443 ssl guard` | current-live target validation requires `public_base_url=https://api.selleros.pro`, server names `89.191.226.88` + `api.selleros.pro`, TLS enabled and LetsEncrypt paths for the domain |
| `public-probe system CA fallback` | `secure public probe fallback` | hosted public probe first uses secure system CA fallback before the legacy insecure diagnostic fallback |
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
- User-facing labels for current web-vitrina are now centralized around `Витрина`, `Загрузить и обновить`, `Загрузка данных`, `Обновить группу`, `Отчёты`, `Отчёт по остаткам`, `Выполнение плана`, `Исторические данные для отчёта`, `Отзывы`, `Жалобы`, `Исследования`, `Товар в акции` and `ЕБД`.

# Not in scope

- Полный словарь каждого internal field.
- Исторические aliases из всего legacy-корпуса.
