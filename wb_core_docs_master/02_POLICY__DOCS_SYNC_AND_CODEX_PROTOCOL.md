---
title: "Policy: docs sync и Codex protocol"
doc_id: "WB-CORE-PROJECT-02-POLICY"
doc_type: "project_policy"
status: "active"
purpose: "Кратко зафиксировать двухслойную схему документации, execution contract, task classification matrix и обязательный sync-протокол для Codex."
scope: "Primary vs secondary docs, update order, manifest discipline как build metadata, local upload-ready source, post-merge external upload reminder, L1/L2/L3 execution matrix, Codex-first execution contract, Git fixation / GitHub closure ownership и запреты на dump-copy."
source_basis:
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "README.md"
source_of_truth_level: "secondary_project_pack"
related_docs:
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md"
related_paths:
  - "wb_core_docs_master/"
  - "docs/architecture/"
  - "docs/modules/"
update_triggers:
  - "изменение docs governance"
  - "изменение Codex execution rule"
  - "изменение project-pack support rule"
built_from_commit: "2e6bfd43a88e693a30b130516f5f8ce66889b801"
---

# Summary

В `wb-core` действует двухслойная документационная схема:
- primary canonical docs в repo;
- secondary compact project-pack в `wb_core_docs_master/`.

Новая норма не может появиться сначала в project-pack. Сначала правится primary doc, потом pack.

Для новых WebCore chat execution handoff действует единый contract: задача сначала классифицируется как `L1 / L2 / L3`, bounded безопасная техработа сначала идёт через Codex, обычный GitHub merge при working `gh` access остаётся Codex-owned routine, а prompt для Codex обязан иметь обязательный classification header и два стандартных финальных блока.

# Current norm

## Layer model

| Layer | Где живёт | Роль |
| --- | --- | --- |
| primary canonical docs | `README.md`, `docs/architecture/*`, `docs/modules/*`, `migration/*` | canonical source of truth |
| secondary project-pack | `wb_core_docs_master/*` | compact retrieval-oriented pack для внешнего ChatGPT Project |

## Required update order

Если изменение влияет на:
- contract;
- module status;
- checkpoint;
- smoke/runbook;
- glossary/aliases;
- migration boundary;
- do-not-lose constraint,

то порядок такой:
1. обновить primary canonical docs;
2. обновить затронутые файлы в `wb_core_docs_master/`;
3. обновить `99_MANIFEST__DOCSET_VERSION.md`;
4. зафиксировать результат в Git.

Если в задаче менялись primary docs или `wb_core_docs_master/`, порядок closure такой:
1. successful merge;
2. `~/Projects/wb-core` приведён к current `origin/main`;
3. `~/Projects/wb-core/wb_core_docs_master` проверен как upload-ready source по manifest;
4. пользователю остаётся ровно один human-only шаг: загрузить актуальный pack во внешний ChatGPT Project.

## Manifest rule

Manifest обязан хранить:
- `docset_version`
- `built_from_commit`
- `built_at`
- `core_docs_changed`

Manifest не должен хранить:
- `project_upload_required`
- `last_project_upload_at`
- `project_upload_note`

Manifest остаётся build/pack metadata файлом и не ведёт operational state внешней загрузки.
Readiness pack определяется по `~/Projects/wb-core/wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md`, а не по Finder timestamps.

## External Project upload closure

- Внешний upload в ChatGPT Project остаётся manual/human-only step.
- Если менялись primary docs или `wb_core_docs_master/`, этот шаг делается после merge.
- Единственный допустимый локальный source для этого upload = `~/Projects/wb-core/wb_core_docs_master`.
- Отдельный post-upload manifest sync больше не нужен.
- Напоминание об upload живёт в governance/handoff rules, а не как recursive state machine внутри pack.

## Execution contract hardening

- Перед любым планом или prompt-ом для Codex задача сначала классифицируется как `L1`, `L2` или `L3`.
- Для prompt-а к Codex обязательно указываются `Класс задачи`, `Причина классификации`, `Режим выполнения`.
- `L1` = локальный малорисковый шаг: без отдельного read-only review по умолчанию, без `README` / architecture sync по умолчанию, только targeted smoke.
- `L2` = обычный bounded block: `module doc + index` обязательны, нужны targeted smoke + `1` integration smoke, без отдельного read-only review по умолчанию.
- `L3` = boundary/risk/governance task: применяется усиленный bounded execution, docs sync идёт по смыслу текущего checkpoint, при необходимости делается отдельная merge-readiness проверка.
- One step = one action: если нужен manual step, один ответ должен содержать один практический следующий шаг.
- Assistant не должна дробить bounded работу без пользы, но и не должна смешивать в одном ответе несколько независимых рискованных действий.
- Для bounded и безопасной технической работы действует Codex-first rule: сначала выбирается путь через Codex.
- Пользователя подключают только для human-only step: логин, права, branch-protection approval / blocker-driven manual merge fallback, ручная UI-проверка, решение по риску.
- Техническую рутину, которую Codex может безопасно выполнить сама, нельзя перекладывать на пользователя.
- Если requested outcome по смыслу включает Git fixation или GitHub closure и пользователь явно не запретил Git/GitHub actions, эти шаги входят в тот же bounded execution.
- Для GitHub closure сначала проверяется `gh auth status -h github.com`.
- Если `gh` доступен, auth валиден и execution context имеет repo write/merge access, обычные `git commit`, `git push`, `gh pr create/update`, `gh pr ready`, `gh pr edit --base ...`, `gh pr merge --delete-branch` являются Codex-owned routine, включая stacked/base-branch merge sequence.
- Auto-merge остаётся optional enhancement и не заменяет обычный merge для stacked/base-branch sequence.
- Manual merge допустим только как fallback-blocker case: нет `gh`, нет auth, недостаточные scopes/permissions, GitHub вернул write blocker или branch protection требует human approval.
- При working auth/access Codex обязана довести ordinary GitHub closure до merge + delete-branch, а не останавливаться на PR-ready/review-ready.
- Если ordinary GitHub closure невозможен, execution возвращается incomplete с exact blocker.
- Если requested outcome не включает Git fixation / GitHub closure или пользователь явно запретил Git/GitHub actions, Codex не делает эти шаги самовольно.
- Любой prompt для Codex обязан начинаться с classification header (`Класс задачи`, `Причина классификации`, `Режим выполнения`) и заканчиваться блоками `=== ДЛЯ КУРАТОРА ===` и `=== СЖАТАЯ ПРОВЕРКА ===`.
- В `=== ДЛЯ КУРАТОРА ===` обязательны поля `Статус`, `Что сделано`, `Изменённые/созданные файлы`, `Ключевой результат`, `Что НЕ тронуто / что осталось вне scope`, `Следующий шаг`, `Если есть блокер — точная причина`, `Repo state`, `Live deploy state`, `Public verify result`, `Sheet verify result`, `Upload-ready source state`, `Manual-only remainder`; при наличии Git-изменений дополнительно обязательны `Commit hash`, `Push`, `PR`, `Ссылка на PR`.
- Для полей вне текущего scope указывается truthful `not in scope`.
- В `=== СЖАТАЯ ПРОВЕРКА ===` обязательны `3-5 коротких пунктов по сути` и `одна строка с главным выводом`.

## Completion states

- `repo-complete` = repo update + local validation + canonical result не остаётся только в рабочем дереве.
- `live-complete` = live runtime/service/publish contour обновлён и public probe подтверждён.
- `sheet-complete` = bound Apps Script/sheet publish step выполнен и минимальный live sheet verify подтверждён.
- `pack-complete` = primary docs, `wb_core_docs_master` и manifest синхронизированы в repo; если docs/pack менялись, после merge локальный `~/Projects/wb-core` приведён к current `origin/main`, а `~/Projects/wb-core/wb_core_docs_master` проверен как upload-ready source.

Правило completion такое:
- если задача меняет public route, runtime/service/nginx publish, bound Apps Script, operator UI или live sheet behavior, `repo-complete` недостаточно;
- Codex обязана довести repo + deploy + `clasp` + verify в одном bounded execution, если это безопасно и доступно, либо вернуть incomplete с exact blocker;
- human-only step остаётся только для логина, прав, branch-protection approval / blocker-driven manual merge fallback, ручной UI-проверки или решения по риску;
- для live/public/GAS задачи в финальном отчёте отдельно фиксируются `repo state`, `live deploy state`, `public verify result`, `sheet verify result`.
- Для docs-governance-only scope fake live/public/sheet steps не нужны; вместо них нужно честно зафиксировать `not in scope` и довести upload-ready source state.
- Если hosted contour уже имеет repo-owned deploy runner, blocker должен называть конкретный missing access/value, а не ссылаться на неопределённое “внешнее operational знание”.

## Legacy rule

Legacy knowledge допускается только в тонком register-слое:
- `06_REGISTER__LEGACY_TO_WEBCORE_MAP.md`
- `07_REGISTER__DO_NOT_LOSE_CONSTRAINTS.md`

Запрещено:
- тянуть в pack полный legacy audit dump;
- копировать целиком `docs/` или `migration/`;
- хранить в pack уникальные нормы, которых нет в primary docs.

# Known gaps

- External upload в ChatGPT Project остаётся manual/operational шагом.
- Repo пока не автоматизирует sync pack -> project.

# Not in scope

- Автоматический uploader в ChatGPT Project.
- Третья параллельная docs-линия.
- Полный mirrorset всех repo docs.
