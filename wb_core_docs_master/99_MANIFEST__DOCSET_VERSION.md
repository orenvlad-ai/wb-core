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
built_from_commit: "e65dc30240e49651c2c660b179acbbd6b2accbd1"
docset_version: "wb_core_docs_master_v64"
built_at: "2026-05-04T21:35:14Z"
build_note: "Recurring derived-sync from current authoritative docs and code-state; compact pack updated for strict feedbacks/complaints contour, CLI-only guarded complaint submit/status probes, localhost owner runtime API defaults, EU hosted dependency/service wiring and removal of the development control-plane prototype from wb-core."
included_roots:
  - "README.md"
  - "docs/architecture/"
  - "docs/modules/"
  - "migration/"
  - "apps/ (code-state audit only)"
  - "packages/ (code-state audit only)"
  - "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__*.json (target metadata audit only)"
  - "wb_core_docs_master/"
core_docs_changed:
  - "README.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
  - "docs/modules/00_INDEX__MODULES.md"
  - "docs/modules/01_MODULE__WEB_SOURCE_SNAPSHOT_BLOCK.md"
  - "docs/modules/02_MODULE__SELLER_FUNNEL_SNAPSHOT_BLOCK.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
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
code_state_audited:
  - "apps/seller_portal_feedbacks_complaints_scout.py"
  - "apps/seller_portal_feedbacks_matching_replay.py"
  - "apps/seller_portal_feedbacks_complaint_dry_run_plan.py"
  - "apps/seller_portal_feedbacks_complaint_submit.py"
  - "apps/seller_portal_feedbacks_complaints_status_sync.py"
  - "apps/seller_portal_feedbacks_complaint_confirmation.py"
  - "apps/seller_portal_feedbacks_complaints_detail_probe.py"
  - "apps/sheet_vitrina_v1_feedbacks_complaints_smoke.py"
  - "apps/seller_portal_relogin_session.py"
  - "apps/web_source_owner_runtime_base_url_smoke.py"
  - "apps/registry_upload_http_entrypoint_hosted_runtime.py"
  - "apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py"
  - "apps/registry_upload_http_entrypoint_public_routes_smoke.py"
  - "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json"
  - "artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json"
  - "artifacts/registry_upload_http_entrypoint/systemd/wb-ai-api.service"
  - "packages/application/sheet_vitrina_v1_feedbacks.py"
  - "packages/application/sheet_vitrina_v1_feedbacks_complaints.py"
  - "packages/adapters/web_source_snapshot_block.py"
  - "packages/adapters/seller_funnel_snapshot_block.py"
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
