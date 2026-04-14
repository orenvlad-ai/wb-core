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
built_from_commit: "e4c08c83e0f19e8f270ac8ee93812a751f57a021"
docset_version: "wb_core_docs_master_v1"
built_at: "2026-04-14T10:07:40Z"
core_docs_changed:
  - "docs/architecture/07_codex_execution_protocol.md"
  - "wb_core_docs_master/02_POLICY__DOCS_SYNC_AND_CODEX_PROTOCOL.md"
  - "wb_core_docs_master/07_REGISTER__DO_NOT_LOSE_CONSTRAINTS.md"
project_upload_required: true
last_project_upload_at: "2026-04-14T09:19:08Z"
project_upload_note: "после фиксации L1/L2/L3 execution matrix, Codex-first rule и mandatory prompt footer нужен повторный upload curated-pack в ChatGPT Project WebCore"
---

# Summary

Этот manifest отвечает на три вопроса:
- из какого repo commit собран текущий curated-pack;
- нужно ли заново загружать его в внешний ChatGPT Project.
- подтверждена ли уже актуальная внешняя загрузка curated-pack в ChatGPT Project WebCore.

# Current norm

- `docset_version` меняется только при осмысленной пересборке pack.
- `built_from_commit` указывает на repo commit, от которого отталкивался pack.
- `project_upload_required = true` означает, что curated-pack изменился после последней подтверждённой внешней загрузки и требует повторного upload.
- `last_project_upload_at` обновляется только после реальной внешней загрузки, а не после локального commit.
- `project_upload_note` можно использовать для короткой фиксации особого upload-события, если это помогает audit trail.

# Known gaps

- Автоматическая синхронизация pack -> Project не materialized.

# Not in scope

- История всех предыдущих pack versions.
- Полный changelog по каждому файлу pack.
