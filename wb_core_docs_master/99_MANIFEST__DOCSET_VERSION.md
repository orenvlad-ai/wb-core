---
title: "Manifest: wb_core_docs_master"
doc_id: "WB-CORE-PROJECT-99-MANIFEST"
doc_type: "manifest"
status: "active"
purpose: "Зафиксировать версию curated-pack, связь с repo commit и флаг необходимости внешней project upload."
scope: "Docset version, build metadata, changed core docs, upload-required flag и состояние последней внешней загрузки."
source_basis:
  - "README.md"
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "wb_core_docs_master/00_INDEX__WEBCORE_PROJECT_DOCS.md"
source_of_truth_level: "secondary_project_pack_manifest"
related_docs:
  - "wb_core_docs_master/00_INDEX__WEBCORE_PROJECT_DOCS.md"
  - "wb_core_docs_master/02_POLICY__DOCS_SYNC_AND_CODEX_PROTOCOL.md"
related_paths:
  - "wb_core_docs_master/"
update_triggers:
  - "любое изменение pack"
  - "любое изменение primary docs, влияющее на pack"
  - "фактическая внешняя project upload"
built_from_commit: "d932ec8477469f5a4bb2f812db3c7333f4946670"
docset_version: "wb_core_docs_master_v1"
built_at: "2026-04-15T19:14:21Z"
core_docs_changed:
  - "docs/architecture/00_migration_charter.md"
  - "docs/architecture/01_target_architecture.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "docs/modules/00_INDEX__MODULES.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/19_MODULE__SHEET_VITRINA_V1_PRESENTATION_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "migration/93_sheet_vitrina_v1_mvp_end_to_end.md"
  - "wb_core_docs_master/01_PASSPORT__WEBCORE_PROJECT.md"
  - "wb_core_docs_master/02_POLICY__DOCS_SYNC_AND_CODEX_PROTOCOL.md"
  - "wb_core_docs_master/03_GLOSSARY__TERMS_ALIASES_AND_CANONICAL_NAMES.md"
  - "wb_core_docs_master/07_REGISTER__DO_NOT_LOSE_CONSTRAINTS.md"
  - "wb_core_docs_master/09_RUNBOOK__COMMON_SMOKE_AND_DEBUG.md"
project_upload_required: true
last_project_upload_at: "2026-04-14T10:23:55Z"
project_upload_note: "pack изменён после hardening execution protocol: для live/public/GAS задач `repo-complete` больше не считается достаточным closure без live deploy / public probe / sheet verify; требуется повторный внешний upload в ChatGPT Project WebCore"
---

# Summary

Этот manifest отвечает на три вопроса:
- из какого repo commit собран текущий curated-pack;
- нужно ли заново загружать его в внешний ChatGPT Project.
- подтверждена ли уже актуальная внешняя загрузка curated-pack в ChatGPT Project WebCore.

# Current norm

- `docset_version` меняется только при осмысленной пересборке pack.
- `built_from_commit` указывает на repo commit, от которого отталкивался pack.
- `project_upload_required = true` означает, что pack уже изменён в repo, но ещё не подтверждён как заново загруженный во внешний Project.
- `last_project_upload_at` обновляется только после реальной внешней загрузки, а не после локального commit.
- `project_upload_note` можно использовать для короткой фиксации особого upload-события, если это помогает audit trail.
- При изменении operator-facing sheet semantics или project-pack wording manifest должен явно отражать необходимость повторной внешней project upload.

# Known gaps

- Автоматическая синхронизация pack -> Project не materialized.

# Not in scope

- История всех предыдущих pack versions.
- Полный changelog по каждому файлу pack.
