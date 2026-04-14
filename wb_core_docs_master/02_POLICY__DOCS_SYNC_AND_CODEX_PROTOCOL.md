---
title: "Policy: docs sync и Codex protocol"
doc_id: "WB-CORE-PROJECT-02-POLICY"
doc_type: "project_policy"
status: "active"
purpose: "Кратко зафиксировать двухслойную схему документации, execution contract, task classification matrix и обязательный sync-протокол для Codex."
scope: "Primary vs secondary docs, update order, manifest discipline, upload-required flag, L1/L2/L3 execution matrix, Codex-first execution contract и запреты на dump-copy."
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
built_from_commit: "e4c08c83e0f19e8f270ac8ee93812a751f57a021"
---

# Summary

В `wb-core` действует двухслойная документационная схема:
- primary canonical docs в repo;
- secondary compact project-pack в `wb_core_docs_master/`.

Новая норма не может появиться сначала в project-pack. Сначала правится primary doc, потом pack.

Для новых WebCore chat execution handoff действует единый contract: задача сначала классифицируется как `L1 / L2 / L3`, bounded безопасная техработа сначала идёт через Codex, а prompt для Codex обязан иметь обязательный classification header и два стандартных финальных блока.

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
4. установить `project_upload_required = true`;
5. зафиксировать результат в Git.

## Manifest rule

Manifest обязан хранить:
- `docset_version`
- `built_from_commit`
- `built_at`
- `core_docs_changed`
- `project_upload_required`
- `last_project_upload_at`

Если pack изменён, а external ChatGPT Project ещё не обновлён, `project_upload_required` должен оставаться `true`.

## Execution contract hardening

- Перед любым планом или prompt-ом для Codex задача сначала классифицируется как `L1`, `L2` или `L3`.
- Для prompt-а к Codex обязательно указываются `Класс задачи`, `Причина классификации`, `Режим выполнения`.
- `L1` = локальный малорисковый шаг: без отдельного read-only review по умолчанию, без `README` / architecture sync по умолчанию, только targeted smoke.
- `L2` = обычный bounded block: `module doc + index` обязательны, нужны targeted smoke + `1` integration smoke, без отдельного read-only review по умолчанию.
- `L3` = boundary/risk/governance task: применяется усиленный bounded execution, docs sync идёт по смыслу текущего checkpoint, при необходимости делается отдельная merge-readiness проверка.
- One step = one action: если нужен manual step, один ответ должен содержать один практический следующий шаг.
- Assistant не должна дробить bounded работу без пользы, но и не должна смешивать в одном ответе несколько независимых рискованных действий.
- Для bounded и безопасной технической работы действует Codex-first rule: сначала выбирается путь через Codex.
- Пользователя подключают только для human-only step: логин, права, ручной merge, ручная UI-проверка, решение по риску.
- Техническую рутину, которую Codex может безопасно выполнить сама, нельзя перекладывать на пользователя.
- Любой prompt для Codex обязан начинаться с classification header (`Класс задачи`, `Причина классификации`, `Режим выполнения`) и заканчиваться блоками `=== ДЛЯ КУРАТОРА ===` и `=== СЖАТАЯ ПРОВЕРКА ===`.
- В `=== ДЛЯ КУРАТОРА ===` обязательны поля `Статус`, `Что сделано`, `Изменённые/созданные файлы`, `Ключевой результат`, `Что НЕ тронуто / что осталось вне scope`, `Следующий шаг`, `Если есть блокер — точная причина`; при наличии Git-изменений дополнительно обязательны `Commit hash`, `Push`, `PR`, `Ссылка на PR`.
- В `=== СЖАТАЯ ПРОВЕРКА ===` обязательны `3-5 коротких пунктов по сути` и `одна строка с главным выводом`.

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
