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
built_from_commit: "138d97eb4eb4f95b1911b3a7fbee54ac5f074dbc"
docset_version: "wb_core_docs_master_v1"
built_at: "2026-04-14T08:33:18Z"
core_docs_changed:
  - "docs/architecture/07_codex_execution_protocol.md"
project_upload_required: true
last_project_upload_at: "2026-04-13T18:11:45Z"
project_upload_note: "после hardening execution protocol для новых чатов нужен повторный upload curated-pack в ChatGPT Project WebCore"
---

# Summary

Этот manifest отвечает на три вопроса:
- из какого repo commit собран текущий curated-pack;
- нужно ли заново загружать его в внешний ChatGPT Project.
- подтверждён ли уже первый initial upload в новый ChatGPT Project WebCore.

# Current norm

- `docset_version` меняется только при осмысленной пересборке pack.
- `built_from_commit` указывает на repo commit, от которого отталкивался pack.
- `project_upload_required = true` означает, что текущий curated-pack уже изменён локально и требует повторной внешней загрузки во внешний Project.
- `last_project_upload_at` обновляется только после реальной внешней загрузки, а не после локального commit.
- `project_upload_note` можно использовать для короткой фиксации особого upload-события, если это помогает audit trail.

# Known gaps

- Автоматическая синхронизация pack -> Project не materialized.

# Not in scope

- История всех предыдущих pack versions.
- Полный changelog по каждому файлу pack.
