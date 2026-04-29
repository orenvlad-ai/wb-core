---
title: "Register: do-not-lose constraints"
doc_id: "WB-CORE-PROJECT-07-CONSTRAINTS"
doc_type: "register"
status: "active"
purpose: "Зафиксировать минимальный набор ограничений, которые нельзя потерять при дальнейших реализациях, docs updates и chat execution handoff."
scope: "Source-of-truth rules, migration boundaries, sheet/runtime invariants, docs governance invariants, chat execution invariants и anti-drift constraints."
source_basis:
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "docs/modules/32_MODULE__RESEARCH_SKU_GROUP_COMPARISON_BLOCK.md"
source_of_truth_level: "derived_secondary_project_pack"
related_docs:
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
update_triggers:
  - "изменение migration boundary"
  - "изменение operator/runtime invariant"
  - "изменение docs governance"
built_from_commit: "863184041a619b3a940f94c38d60e0dfce6bc6d9"
---

# Summary

Ниже не roadmap, а hard constraints.

Если следующий change нарушает один из них, это уже не "маленькая эволюция", а scope change и его нужно явно review-ить.

# Current norm

| Constraint ID | Constraint |
| --- | --- |
| `C-01` | Git-tracked repo docs и code остаются единственным canonical source of truth. Runtime-only fixes без Git недействительны. |
| `C-02` | Таблица остаётся thin operator shell; production truth и heavy logic не должны возвращаться в Apps Script. |
| `C-03` | Legacy Google Sheets/GAS contour is `ARCHIVED / DO NOT USE`; former `CONFIG!H:I`, `DATA_VITRINA`, `STATUS`, Apps Script menu and `/load` paths are archive/migration-only, not current runtime/write/load/verify targets. |
| `C-04` | Upload flow обязан использовать канонический bundle/result contract и existing HTTP entrypoint, а не локальные sheet-side копии validation logic; server-side acceptance должна опираться на structure/schema correctness и фактические длины registry lists, а не на hardcoded row-count caps. |
| `C-05` | Current website/operator/web-vitrina reads server-side ready snapshots; reverse-load в Google Sheets `DATA_VITRINA` must stay archived and guarded. |
| `C-06` | `wb_core_docs_master` не может становиться dump-копией repo docs или полным legacy mirror. |
| `C-07` | Legacy knowledge разрешён только как thin register/map/constraint layer. |
| `C-08` | Ordinary task-flow обновляет code/tests и затронутые authoritative docs, если truth изменился; `wb_core_docs_master/**` и manifest не обновляются по умолчанию и не являются completion blocker для обычной задачи. |
| `C-09` | `wb_core_docs_master/**` и manifest обновляются только в explicit derived-sync flow или transitional pack rebuild; после такого merge `~/Projects/wb-core` должен быть приведён к current `origin/main`, `~/Projects/wb-core/wb_core_docs_master` должен быть проверен как upload-ready source по manifest, и только после этого пользователю остаётся один human-only post-merge шаг: загрузить актуальный pack во внешний ChatGPT Project. |
| `C-10` | Bounded steps не должны тихо превращаться в deploy/platform redesign, full parity campaign или новый parallel contour. |
| `C-11` | Для новых WebCore chat prompts prompt к Codex обязан явно содержать `Класс задачи`, `Причина классификации`, `Режим выполнения` и заканчиваться блоками `=== ДЛЯ КУРАТОРА ===` и `=== СЖАТАЯ ПРОВЕРКА ===`; без этого execution handoff считается неполным. |
| `C-12` | Bounded и безопасная техническая работа должна сначала идти через Codex; пользователю можно отдавать только human-only step: логин, права, branch-protection approval / blocker-driven manual merge fallback, ручная UI-проверка или решение по риску. |
| `C-13` | Если manual handoff неизбежен, действует `one step = one action`: один ответ содержит один минимальный практический следующий шаг и не смешивает несколько независимых рискованных действий. |
| `C-14` | Матрица `L1/L2/L3` задаёт минимальный execution burden: `L1` = локальный малорисковый шаг без отдельного read-only review и без `README` / architecture sync по умолчанию, только targeted smoke; `L2` = bounded block с обязательными `module doc + index`, targeted smoke и `1` integration smoke; `L3` = boundary/risk/governance task с усиленным bounded execution, docs sync по смыслу текущего checkpoint и при необходимости отдельной merge-readiness проверкой. |
| `C-15` | Full current truth и `STATUS` остаются authoritative для всего enabled+show_in_data набора; operator-facing `DATA_VITRINA` не должна invent-ить локальный truth path, не должна silently выкидывать `show_in_data` rows и должна materialize-ить incoming server-driven row set как thin data-driven `date_matrix` без sheet-side subset logic. |
| `C-15a` | Current unified `/sheet-vitrina-v1/vitrina` UI remains a consumer of server-owned ready snapshots and source/job/status truth; group refresh, cell highlights, report filters and browser persistence must not become a second source-of-truth layer. |
| `C-15b` | `ЕБД` / `единая база данных` is only a user-facing alias for shared server-side accepted truth/runtime state in `wb-core`; Google Sheets/GAS, HTML/browser UI, browser `localStorage`, report-private manual tables and operator XLSX baseline uploads must not be treated as the canonical data-truth layer. |
| `C-15c` | Plan-report baseline and ready-fact reconcile remain bounded server-side support paths: baseline can fill only full-month plan-report aggregates, ready-fact reconcile can insert only missing accepted `fin_report_daily` / `ads_compact` slots from persisted ready snapshots, and neither path may overwrite existing accepted diffs or fabricate blank values as zeros. |
| `C-16` | Для задач с live/public эффектом `repo-complete` недостаточно: execution handoff не считается complete, пока не достигнуты требуемые `live-complete` / public-web verify, либо пока точный blocker явно не назван. Sheet completion is no longer a success path; for archived GAS changes only guard push/verify is required. |
| `C-17` | Если live deploy/restart или public probe безопасны и доступны, они должны входить в тот же bounded execution по умолчанию, а не откладываться без явной причины. `clasp push` входит в обязательный путь только для archived Apps Script guard changes and verifies blocked/archived behavior, not sheet write success. |
| `C-18` | Если задача добавляет или меняет public route, обязательна внешняя public probe-проверка; `404`/`Not Found` на ожидаемом route трактуется как stale deploy или incomplete publish wiring, пока не доказано обратное. |
| `C-19` | Если requested outcome по смыслу включает Git fixation или GitHub closure и пользователь явно не запретил Git/GitHub actions, Codex сначала проверяет `gh auth status -h github.com`; при working auth и repo write/merge access обычные `git commit`, `git push`, `gh pr create/update`, `gh pr ready`, retarget через `gh pr edit --base ...`, `gh pr merge --delete-branch` являются Codex-owned routine, включая stacked/base-branch merge sequence. При working auth/access Codex обязана довести ordinary GitHub closure до merge + delete-branch; manual merge допустим только как fallback-blocker case. |
| `C-20` | Единственный допустимый локальный source для внешнего ChatGPT Project upload = `~/Projects/wb-core/wb_core_docs_master`; временные копии, zip-архивы и произвольные папки не считаются canonical source. |
| `C-21` | Перед sync `~/Projects/wb-core` к current `origin/main` несвязанный dirty state нужно сохранять только bounded safe method (`stash`, backup, отдельная branch/worktree или эквивалент), без destructive reset поверх пользовательских изменений. |
| `C-22` | Готовность pack к upload определяется по `~/Projects/wb-core/wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md`, а не по Finder timestamps, имени архива или памяти исполнителя. |
| `C-23` | После explicit derived-sync или transitional pack rebuild, когда upload-ready source подготовлен, в handoff должен оставаться ровно один human-only remainder: внешний upload актуального `wb_core_docs_master`; manifest при этом не превращается в upload state machine. |
| `C-24` | Hosted public-route publication for the current contour goes through the repo-owned nginx allowlist and deploy runner; manual broad catch-all live nginx edits are not a completion path. |
| `C-25` | `Отзывы` and feedbacks AI stay read-only/transient: they must not submit complaints, write Google Sheets/GAS, persist AI labels as accepted truth/ЕБД, or call Seller Portal automation. |
| `C-26` | `Исследования` / SKU group comparison is read-only over accepted truth / persisted ready snapshots, excludes financial metrics in the MVP, makes no causal/statistical claims, and must not trigger refresh/upstream fetch/backfill/reconcile. |
| `C-27` | Promo preflight/manifest/artifact diagnostics and promo current invariant smoke are observability/guard surfaces only; expected ended/no-download non-materializable campaigns must not become fatal missing-artifact blockers, and diagnostics must not become metric truth. |
| `C-28` | Promo historical truth must survive raw artifact retention: normalized campaign rows and manifest/fingerprint metadata are replay-critical, raw XLSX/HAR/screenshots/traces are short-lived debug artifacts, and GC may delete only guarded candidates after replay-critical persistence is proven. |
| `C-29` | Current hosted writes target only the EU runtime (`wb-core-eu-root` / `89.191.226.88` / `/opt/wb-core-runtime/state`). Old selleros (`selleros-root` / `178.72.152.177`) is rollback-only/read-only evidence; routine deploy/apply-nginx/restart/update/GC mutations must fail fast before remote side effects unless the explicit emergency rollback override is set. |
| `C-30` | Current-live EU publication must be production HTTPS, not IP-only/HTTP-only: `public_base_url=https://api.selleros.pro`, nginx `server_name 89.191.226.88 api.selleros.pro;`, `listen 443 ssl` and LetsEncrypt cert/key paths for `api.selleros.pro` are hard invariants. Losing domain/443 is production outage drift, and mutating deploy/apply-nginx must fail locally before remote changes if the invariant is broken. |

# Known gaps

- Operator-facing sheet сейчас intentionally остаётся thin presentation layer поверх current truth; это не новый source-of-truth layer и не место для local subset/fallback logic.
- Hosted runtime deploy/probe contract должен оставаться repo-owned; human-only boundary допускается только для actual access/credentials/target values, а не для route/service archaeology.

# Not in scope

- Полный список всех implementation details.
- Подробная checklist-матрица по каждому модулю.
