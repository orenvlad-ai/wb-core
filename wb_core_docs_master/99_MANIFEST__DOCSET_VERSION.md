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
built_from_commit: "33be18836bb46f029b48fd19f28d45300171602a"
docset_version: "wb_core_docs_master_v1"
built_at: "2026-04-13T17:54:32Z"
core_docs_changed:
  - "README.md"
  - "docs/architecture/00_migration_charter.md"
  - "docs/architecture/01_target_architecture.md"
  - "docs/architecture/02_repo_workspace_blueprint.md"
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "docs/architecture/08_open_questions_and_decision_log.md"
  - "docs/modules/00_INDEX__MODULES.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
project_upload_required: true
last_project_upload_at: "unknown_not_uploaded_yet"
---

# Summary

Этот manifest отвечает на два вопроса:
- из какого repo commit собран текущий curated-pack;
- нужно ли заново загружать его в внешний ChatGPT Project.

# Current norm

- `docset_version` меняется только при осмысленной пересборке pack.
- `built_from_commit` указывает на repo commit, от которого отталкивался pack.
- `project_upload_required = true` означает, что внешний Project pack ещё не синхронизирован с текущей repo-версией.
- `last_project_upload_at` обновляется только после реальной внешней загрузки, а не после локального commit.

# Known gaps

- External upload time пока неизвестен.
- Автоматическая синхронизация pack -> Project не materialized.

# Not in scope

- История всех предыдущих pack versions.
- Полный changelog по каждому файлу pack.
