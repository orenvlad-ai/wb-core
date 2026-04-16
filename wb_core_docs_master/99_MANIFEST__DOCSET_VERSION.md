---
title: "Manifest: wb_core_docs_master"
doc_id: "WB-CORE-PROJECT-99-MANIFEST"
doc_type: "manifest"
status: "active"
purpose: "Зафиксировать версию curated-pack, связь с repo commit и состав последней repo-owned pack сборки."
scope: "Docset version, build metadata, changed core docs и связь pack с repo commit."
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
  - "изменение build metadata pack"
built_from_commit: "4252a90bac0329eb046644205950c974c91981c5"
docset_version: "wb_core_docs_master_v1"
built_at: "2026-04-16T19:22:09Z"
core_docs_changed:
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/07_codex_execution_protocol.md"
  - "wb_core_docs_master/00_INDEX__WEBCORE_PROJECT_DOCS.md"
  - "wb_core_docs_master/02_POLICY__DOCS_SYNC_AND_CODEX_PROTOCOL.md"
  - "wb_core_docs_master/07_REGISTER__DO_NOT_LOSE_CONSTRAINTS.md"
  - "wb_core_docs_master/09_RUNBOOK__COMMON_SMOKE_AND_DEBUG.md"
---

# Summary

Этот manifest отвечает на два вопроса:
- из какого repo commit собран текущий curated-pack;
- какие primary/pack docs вошли в текущую repo-owned пересборку.

Внешний upload в ChatGPT Project живёт вне этого manifest и регулируется governance/handoff rules.

# Current norm

- `docset_version` меняется только при осмысленной пересборке pack.
- `built_from_commit` указывает на repo commit, от которого отталкивался pack.
- `core_docs_changed` хранит repo-owned список primary/pack docs, которые меняют текущую сборку.
- manifest не хранит operational state внешней загрузки и не требует post-upload repo sync.
- Если docs/pack менялись, внешний upload текущего pack делается после merge как отдельный human-only шаг, но этот факт не трекается внутри самого pack.

# Known gaps

- Автоматическая синхронизация pack -> Project не materialized и остаётся вне repo-owned metadata.

# Not in scope

- Operational audit trail внешних project uploads.
- История всех предыдущих pack versions.
- Полный changelog по каждому файлу pack.
