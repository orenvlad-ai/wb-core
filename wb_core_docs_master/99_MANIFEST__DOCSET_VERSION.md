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
source_of_truth_level: "derived_secondary_project_pack_manifest"
related_docs:
  - "wb_core_docs_master/00_INDEX__WEBCORE_PROJECT_DOCS.md"
  - "wb_core_docs_master/02_POLICY__DOCS_SYNC_AND_CODEX_PROTOCOL.md"
related_paths:
  - "wb_core_docs_master/"
update_triggers:
  - "любое изменение pack"
  - "explicit derived-sync flow"
  - "transitional pack rebuild"
  - "изменение build metadata pack"
built_from_commit: "c8faa36b1eec440925a8c98b5d87eb188e5e7492"
docset_version: "wb_core_docs_master_v60"
built_at: "2026-04-25T22:18:44Z"
build_note: "Recurring derived-sync from current authoritative docs and code-state; compact pack updated for EBD alias, plan-report baseline/reconcile, lazy web-vitrina source status and compact toolbar/latest-four-days UX."
included_roots:
  - "README.md"
  - "docs/architecture/"
  - "docs/modules/"
  - "migration/"
  - "apps/ (code-state audit only)"
  - "packages/ (code-state audit only)"
  - "wb_core_docs_master/"
core_docs_changed:
  - "docs/architecture/03_source_of_truth_policy.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
  - "docs/modules/22_MODULE__REGISTRY_UPLOAD_DB_BACKED_RUNTIME_BLOCK.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "wb_core_docs_master/00_INDEX__WEBCORE_PROJECT_DOCS.md"
  - "wb_core_docs_master/01_PASSPORT__WEBCORE_PROJECT.md"
  - "wb_core_docs_master/02_POLICY__DOCS_SYNC_AND_CODEX_PROTOCOL.md"
  - "wb_core_docs_master/03_GLOSSARY__TERMS_ALIASES_AND_CANONICAL_NAMES.md"
  - "wb_core_docs_master/05_REGISTER__MODULE_STATUS_AND_CHECKPOINTS.md"
  - "wb_core_docs_master/06_REGISTER__LEGACY_TO_WEBCORE_MAP.md"
  - "wb_core_docs_master/07_REGISTER__DO_NOT_LOSE_CONSTRAINTS.md"
  - "wb_core_docs_master/09_RUNBOOK__COMMON_SMOKE_AND_DEBUG.md"
  - "wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md"
---

# Summary

Этот manifest отвечает на два вопроса:
- из какого repo commit собран текущий curated-pack;
- какие authoritative/pack docs вошли в текущую repo-owned пересборку.

Внешний upload в ChatGPT Project живёт вне этого manifest и регулируется governance/handoff rules.

# Current norm

- `docset_version` меняется только при осмысленной пересборке pack.
- `built_from_commit` указывает на repo commit, от которого отталкивался pack.
- `build_note` фиксирует причину текущей сборки как build metadata.
- `core_docs_changed` хранит repo-owned список authoritative/pack docs, которые меняют текущую сборку.
- ordinary task-flow не обновляет manifest по умолчанию; manifest обновляется в explicit derived-sync flow или transitional pack rebuild.
- manifest не хранит operational state внешней загрузки и не требует post-upload repo sync.
- Если explicit derived-sync flow или transitional pack rebuild завершён, внешний upload текущего pack делается после merge как отдельный human-only шаг, но этот факт не трекается внутри самого pack.

# Known gaps

- Автоматическая синхронизация pack -> Project не materialized и остаётся вне repo-owned metadata.

# Not in scope

- Operational audit trail внешних project uploads.
- История всех предыдущих pack versions.
- Полный changelog по каждому файлу pack.
