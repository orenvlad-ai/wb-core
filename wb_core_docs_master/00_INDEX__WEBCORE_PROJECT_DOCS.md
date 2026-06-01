---
title: "Индекс project-pack `wb_core_docs_master`"
doc_id: "WB-CORE-PROJECT-00-INDEX"
doc_type: "project_pack_index"
status: "active"
purpose: "Дать compact navigation entrypoint для `wb_core_docs_master` как curated-pack под отдельный ChatGPT Project."
scope: "Состав стартового pack, роли файлов, порядок чтения и границы между authoritative repo docs и derived secondary project-pack."
source_basis:
  - "README.md"
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "docs/modules/00_INDEX__MODULES.md"
source_of_truth_level: "derived_secondary_project_pack"
related_docs:
  - "README.md"
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md"
related_paths:
  - "wb_core_docs_master/"
update_triggers:
  - "изменение состава pack"
  - "изменение роли `wb_core_docs_master`"
  - "изменение policy двухслойной схемы docs"
built_from_commit: "623dcc17ad637f04e601f67f71bcb627881cadaa"
---

# Summary

`wb_core_docs_master` — это derived secondary compact project-pack для retrieval/use вне repo, а не замена authoritative canonical docs.

Canonical local upload-ready source для внешнего Project during explicit derived-sync flow:
- `~/Projects/wb-core/wb_core_docs_master`
- readiness этого source определяется по manifest, а не самим index

Использовать pack нужно так:
1. начать с этого индекса;
2. затем читать passport и policy;
3. потом glossary и registers;
4. в конце смотреть runbook и manifest.

# Current norm

| Файл | Роль |
| --- | --- |
| `00_INDEX__WEBCORE_PROJECT_DOCS.md` | entrypoint и navigation |
| `01_PASSPORT__WEBCORE_PROJECT.md` | компактный current-state passport |
| `02_POLICY__DOCS_SYNC_AND_CODEX_PROTOCOL.md` | правила двухслойной docs-схемы |
| `03_GLOSSARY__TERMS_ALIASES_AND_CANONICAL_NAMES.md` | терминология и canonical names |
| `05_REGISTER__MODULE_STATUS_AND_CHECKPOINTS.md` | статусы модулей и checkpoints |
| `06_REGISTER__LEGACY_TO_WEBCORE_MAP.md` | тонкая карта legacy -> `wb-core` |
| `07_REGISTER__DO_NOT_LOSE_CONSTRAINTS.md` | do-not-lose ограничения |
| `09_RUNBOOK__COMMON_SMOKE_AND_DEBUG.md` | compact smoke/debug runbook |
| `99_MANIFEST__DOCSET_VERSION.md` | version/manifest и build metadata |

Authoritative canonical docs остаются в:
- `README.md`
- `docs/architecture/*`
- `docs/modules/*`
- `migration/*`

# Known gaps

- Pack не покрывает весь текст module docs и не заменяет их.
- Pack не включает полный legacy-корпус.
- Upload в внешний ChatGPT Project остаётся отдельным human-only шагом после merge только для explicit derived-sync flow или transitional pack rebuild.
- Этот index даёт только navigation pointer и не должен сам становиться carrier operational upload rules.
- Hosted runtime deploy/probe contract теперь materialized в authoritative docs и отражается в pack как compact navigation/runbook knowledge, включая active EU target `wb-core-eu-root` / `89.191.226.88`, production endpoint `https://api.selleros.pro`, app-level auth/session boundary, auth-aware fast canonical probes, explicit deep refresh через `--include-refresh`, hard nginx invariant `server_name 89.191.226.88 api.selleros.pro` + `listen 443 ssl`, rollback-only old selleros guard, managed nginx public-route allowlist, unified web-vitrina UI, dark/violet operator visual system, table-first web-vitrina layout, rolling `2 недели` default period, server-side authenticated user-config for `Метрики` presentation, sticky metric/section columns, compact lazy source-status preview, pulsing violet load indicator, weighted TOTAL `CTR в поиске средний`, `Поставки -> От поставщика` order registry with supplier invoice parser/nomenclature/compatibility matching, canonical full refresh / group refresh / auto-refresh semantics, strict `Отзывы` table/filter/export/AI flow, nested `Жалобы` runtime journal/status-sync, protected selected-row submit job, nested `Авто-жалобы` runtime schedules/run-now/tick, shared Seller Portal single-flight automation lock, canonical EU bot storage-state/no-local-fallback policy, route-specific Seller Portal capability checks, research SKU-group comparison tab, lazy source-status details, grouped source refresh, 1C source group `onec_product_capital`, 1C profitability metrics, structural zero-stock semantics for fresh missing 1C canonical buckets, localhost owner runtime API `wb-ai-api.service`, normalized promo archive + artifact GC/replay, promo current invariant guard, current-only Seller Portal SPP `discountOnSite` with accepted-current rollover evidence, plan/stock reports, plan-report baseline routes, one-off ready-fact reconcile, seller-session session-check/recovery, safe stop semantics, per-run completion markers и hardened noVNC/launcher path, а не как hidden operational memory.

# Not in scope

- Копия всего `docs/`.
- Копия всех `migration/*`.
- Перенос artifacts/evidence/logs целиком.
- Хранение новых норм раньше authoritative repo docs.
