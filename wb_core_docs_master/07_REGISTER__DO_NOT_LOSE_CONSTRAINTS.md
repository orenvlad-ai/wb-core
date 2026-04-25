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
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
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
built_from_commit: "c8faa36b1eec440925a8c98b5d87eb188e5e7492"
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

# Known gaps

- Operator-facing sheet сейчас intentionally остаётся thin presentation layer поверх current truth; это не новый source-of-truth layer и не место для local subset/fallback logic.
- Hosted runtime deploy/probe contract должен оставаться repo-owned; human-only boundary допускается только для actual access/credentials/target values, а не для route/service archaeology.

# Not in scope

- Полный список всех implementation details.
- Подробная checklist-матрица по каждому модулю.
