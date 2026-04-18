---
title: "Индекс project-pack `wb_core_docs_master`"
doc_id: "WB-CORE-PROJECT-00-INDEX"
doc_type: "project_pack_index"
status: "active"
purpose: "Дать compact navigation entrypoint для `wb_core_docs_master` как curated-pack под отдельный ChatGPT Project."
scope: "Состав стартового pack, роли файлов, порядок чтения и границы между primary repo docs и secondary project-pack."
source_basis:
  - "README.md"
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "docs/modules/00_INDEX__MODULES.md"
source_of_truth_level: "secondary_project_pack"
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
built_from_commit: "0b9cd8078fca3f3f4ad7325768fef4b31cb87c7e"
---

# Summary

`wb_core_docs_master` — это secondary compact project-pack для retrieval/use вне repo, а не замена primary canonical docs.

Canonical local upload-ready source для внешнего Project governed primary policy:
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

Primary canonical docs остаются в:
- `README.md`
- `docs/architecture/*`
- `docs/modules/*`
- `migration/*`

# Known gaps

- Pack не покрывает весь текст module docs и не заменяет их.
- Pack не включает полный legacy-корпус.
- Upload в внешний ChatGPT Project остаётся отдельным human-only шагом после merge, если менялись primary docs или pack.
- Этот index даёт только navigation pointer и не должен сам становиться carrier operational upload rules.
- Hosted runtime deploy/probe contract теперь materialized в primary docs и отражается в pack как compact navigation/runbook knowledge, а не как hidden operational memory.

# Not in scope

- Копия всего `docs/`.
- Копия всех `migration/*`.
- Перенос artifacts/evidence/logs целиком.
- Хранение новых норм раньше primary repo docs.
