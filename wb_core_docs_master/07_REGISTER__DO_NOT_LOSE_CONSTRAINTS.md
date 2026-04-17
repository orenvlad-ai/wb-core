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
source_of_truth_level: "secondary_project_pack"
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
built_from_commit: "0b9cd8078fca3f3f4ad7325768fef4b31cb87c7e"
---

# Summary

Ниже не roadmap, а hard constraints.

Если следующий change нарушает один из них, это уже не "маленькая эволюция", а scope change и его нужно явно review-ить.

# Current norm

| Constraint ID | Constraint |
| --- | --- |
| `C-01` | Git-tracked repo docs и code остаются единственным canonical source of truth. Runtime-only fixes без Git недействительны. |
| `C-02` | Таблица остаётся thin operator shell; production truth и heavy logic не должны возвращаться в Apps Script. |
| `C-03` | `CONFIG!H:I` является service/status block и не должен теряться при `prepare`, `upload`, `load`. |
| `C-04` | Upload flow обязан использовать канонический bundle/result contract и existing HTTP entrypoint, а не локальные sheet-side копии validation logic; server-side acceptance должна опираться на structure/schema correctness и фактические длины registry lists, а не на hardcoded row-count caps. |
| `C-05` | Reverse-load в `DATA_VITRINA` должен идти из живого server-side contour, а не из fake local sheet fixture. |
| `C-06` | `wb_core_docs_master` не может становиться dump-копией repo docs или полным legacy mirror. |
| `C-07` | Legacy knowledge разрешён только как thin register/map/constraint layer. |
| `C-08` | При изменении contract/status/checkpoint/smoke/glossary/runbook нужно обновлять и primary docs, и затронутый project-pack, и manifest. |
| `C-09` | Если в задаче менялись primary docs или `wb_core_docs_master/`, финальный handoff обязан напомнить один human-only post-merge шаг: загрузить актуальный pack во внешний ChatGPT Project. Отдельный post-upload manifest sync не нужен. |
| `C-10` | Bounded steps не должны тихо превращаться в deploy/platform redesign, full parity campaign или новый parallel contour. |
| `C-11` | Для новых WebCore chat prompts prompt к Codex обязан явно содержать `Класс задачи`, `Причина классификации`, `Режим выполнения` и заканчиваться блоками `=== ДЛЯ КУРАТОРА ===` и `=== СЖАТАЯ ПРОВЕРКА ===`; без этого execution handoff считается неполным. |
| `C-12` | Bounded и безопасная техническая работа должна сначала идти через Codex; пользователю можно отдавать только human-only step: логин, права, branch-protection approval / blocker-driven manual merge fallback, ручная UI-проверка или решение по риску. |
| `C-13` | Если manual handoff неизбежен, действует `one step = one action`: один ответ содержит один минимальный практический следующий шаг и не смешивает несколько независимых рискованных действий. |
| `C-14` | Матрица `L1/L2/L3` задаёт минимальный execution burden: `L1` = локальный малорисковый шаг без отдельного read-only review и без `README` / architecture sync по умолчанию, только targeted smoke; `L2` = bounded block с обязательными `module doc + index`, targeted smoke и `1` integration smoke; `L3` = boundary/risk/governance task с усиленным bounded execution, docs sync по смыслу текущего checkpoint и при необходимости отдельной merge-readiness проверкой. |
| `C-15` | Full current truth и `STATUS` остаются authoritative для всего enabled+show_in_data набора; operator-facing `DATA_VITRINA` не должна invent-ить локальный truth path, не должна silently выкидывать `show_in_data` rows и должна materialize-ить incoming server-driven row set как thin data-driven `date_matrix` без sheet-side subset logic. |
| `C-16` | Для задач с live/public/GAS эффектом `repo-complete` недостаточно: execution handoff не считается complete, пока не достигнуты требуемые `live-complete` и/или `sheet-complete`, либо пока точный blocker явно не назван. |
| `C-17` | Если `clasp push`, live deploy/restart или public probe безопасны и доступны, они должны входить в тот же bounded execution по умолчанию, а не откладываться без явной причины. |
| `C-18` | Если задача добавляет или меняет public route, обязательна внешняя public probe-проверка; `404`/`Not Found` на ожидаемом route трактуется как stale deploy или incomplete publish wiring, пока не доказано обратное. |
| `C-19` | Для GitHub closure Codex сначала проверяет `gh auth status -h github.com`; при working auth и repo write/merge access обычные `gh pr ready`, retarget через `gh pr edit --base ...`, `gh pr merge --delete-branch` являются Codex-owned routine, включая stacked/base-branch merge sequence. Manual merge допустим только как fallback-blocker case. |

# Known gaps

- Operator-facing sheet сейчас intentionally остаётся thin presentation layer поверх current truth; это не новый source-of-truth layer и не место для local subset/fallback logic.
- Hosted runtime deploy/probe contract должен оставаться repo-owned; human-only boundary допускается только для actual access/credentials/target values, а не для route/service archaeology.

# Not in scope

- Полный список всех implementation details.
- Подробная checklist-матрица по каждому модулю.
