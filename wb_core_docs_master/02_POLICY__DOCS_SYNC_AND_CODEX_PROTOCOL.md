---
title: "Policy: docs sync и Codex protocol"
doc_id: "WB-CORE-PROJECT-02-POLICY"
doc_type: "project_policy"
status: "active"
purpose: "Кратко зафиксировать двухслойную схему документации и обязательный sync-протокол для Codex."
scope: "Primary vs secondary docs, update order, manifest discipline, upload-required flag и запреты на dump-copy."
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
built_from_commit: "33be18836bb46f029b48fd19f28d45300171602a"
---

# Summary

В `wb-core` действует двухслойная документационная схема:
- primary canonical docs в repo;
- secondary compact project-pack в `wb_core_docs_master/`.

Новая норма не может появиться сначала в project-pack. Сначала правится primary doc, потом pack.

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
