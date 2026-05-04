---
title: "Policy: docs sync и Codex protocol"
doc_id: "WB-CORE-PROJECT-02-POLICY"
doc_type: "project_policy"
status: "active"
purpose: "Кратко зафиксировать двухслойную authoritative/derived схему документации, execution contract, task classification matrix и sync-протокол для Codex."
scope: "Authoritative vs derived docs, ordinary task-flow без default pack rebuild, отдельный derived-sync flow, manifest discipline как build metadata, local upload-ready source для derived-sync, L1/L2/L3 execution matrix, Codex-first execution contract, Git fixation / GitHub closure ownership и запреты на dump-copy."
source_basis:
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "README.md"
source_of_truth_level: "derived_secondary_project_pack"
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
built_from_commit: "e65dc30240e49651c2c660b179acbbd6b2accbd1"
---

# Summary

В `wb-core` действует двухслойная документационная схема:
- authoritative / canonical docs в repo;
- derived / secondary compact project-pack в `wb_core_docs_master/`.

Новая норма не может появиться сначала в project-pack. Authoritative docs являются source of truth; pack является derived retrieval artifact и не блокирует обычный task-flow.

Для новых WebCore chat execution handoff действует единый contract: задача сначала классифицируется как `L1 / L2 / L3`, bounded безопасная техработа сначала идёт через Codex, обычный GitHub merge при working `gh` access остаётся Codex-owned routine, а prompt для Codex обязан иметь обязательный classification header и два стандартных финальных блока.

# Current norm

## Layer model

| Layer | Где живёт | Роль |
| --- | --- | --- |
| authoritative canonical docs | `README.md`, `docs/architecture/*`, `docs/modules/*`, `migration/*` | canonical source of truth |
| derived project-pack | `wb_core_docs_master/*` | compact retrieval-oriented pack для внешнего ChatGPT Project |

## Ordinary task-flow

Если изменение влияет на:
- contract;
- module status;
- checkpoint;
- smoke/runbook;
- glossary/aliases;
- migration boundary;
- do-not-lose constraint,

то порядок такой:
1. обновить authoritative canonical docs;
2. зафиксировать результат в Git вместе с code/tests, если они менялись.

`wb_core_docs_master/**` и `99_MANIFEST__DOCSET_VERSION.md` не обновляются по умолчанию в ordinary task-flow. Отсутствие pack rebuild не является completion blocker для обычной задачи, если task явно не является derived-sync flow.

## Derived-sync flow

Derived pack обновляется отдельным task-flow:
1. прочитать current authoritative docs и current code-state;
2. пересобрать затронутые файлы `wb_core_docs_master/**` как compact retrieval pack, а не dump-копию `docs/`;
3. обновить `99_MANIFEST__DOCSET_VERSION.md` как build metadata only;
4. выполнить governance/contamination smoke;
5. зафиксировать результат в Git.

Если task является derived-sync flow или transitional pack rebuild, порядок closure такой:
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

## Current data-truth alias

Если в current website/operator contour используется user-facing alias `ЕБД` / `единая база данных`, pack трактует его только как derived label для общего server-side accepted truth/runtime layer `wb-core`: persisted accepted temporal source slots, ready snapshots and related runtime state. Этот alias не означает Google Sheets/GAS, HTML/browser UI, browser `localStorage` или private manual table одного отчёта.

## External Project upload closure

- Внешний upload в ChatGPT Project остаётся manual/human-only step.
- Этот шаг делается после merge только для explicit derived-sync flow или transitional pack rebuild.
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
- `sheet-complete` = archived bound Apps Script guard publish step выполнен и минимальный guard-only verify подтверждён; это требуется только для изменений archived GAS guard.
- `pack-complete` = explicit derived-sync flow или transitional pack rebuild довёл `wb_core_docs_master` и manifest до repo-owned sync; после merge локальный `~/Projects/wb-core` приведён к current `origin/main`, а `~/Projects/wb-core/wb_core_docs_master` проверен как upload-ready source.

Правило completion такое:
- если задача меняет public route, runtime/service/nginx publish, bound Apps Script, operator UI или live sheet behavior, `repo-complete` недостаточно;
- Codex обязана довести repo + deploy + active-surface verify в одном bounded execution, если это безопасно и доступно, либо вернуть incomplete с exact blocker;
- для текущей web-витрины active-surface verify = server/public `/v1/sheet-vitrina-v1/web-vitrina`, `surface=page_composition` и `/sheet-vitrina-v1/vitrina`; Google Sheets / GAS / `clasp` / `invalid_grant` не являются completion blocker;
- `clasp` + guard-only verify добавляются только когда scope реально меняет archived bound Apps Script guard;
- human-only step остаётся только для логина, прав, branch-protection approval / blocker-driven manual merge fallback, ручной UI-проверки или решения по риску;
- для live/public задачи в финальном отчёте отдельно фиксируются `repo state`, `live deploy state`, `public verify result`; `sheet verify result` указывается как `not in scope`, кроме archived bound Apps Script guard changes.
- Для docs-governance-only scope fake live/public/sheet steps не нужны; вместо них нужно честно зафиксировать `not in scope`. Upload-ready source state доводится только для explicit derived-sync flow или transitional pack rebuild.
- Если hosted contour уже имеет repo-owned deploy runner, blocker должен называть конкретный missing access/value, а не ссылаться на неопределённое “внешнее operational знание”.

## Legacy rule

Legacy knowledge допускается только в тонком register-слое:
- `06_REGISTER__LEGACY_TO_WEBCORE_MAP.md`
- `07_REGISTER__DO_NOT_LOSE_CONSTRAINTS.md`

Запрещено:
- тянуть в pack полный legacy audit dump;
- копировать целиком `docs/` или `migration/`;
- хранить в pack уникальные нормы, которых нет в authoritative docs.

# Known gaps

- External upload в ChatGPT Project остаётся manual/operational шагом.
- Repo пока не автоматизирует sync pack -> project.

# Not in scope

- Автоматический uploader в ChatGPT Project.
- Третья параллельная docs-линия.
- Полный mirrorset всех repo docs.
