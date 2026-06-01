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
built_from_commit: "623dcc17ad637f04e601f67f71bcb627881cadaa"
docset_version: "wb_core_docs_master_v71"
built_at: "2026-06-01T19:16:55Z"
build_note: "Recurring derived-sync from current authoritative docs and code-state after supplier shipment order registry/nomenclature/compatibility matching, supplier-facing label and contract parsing updates, web-vitrina UX/source-status/promo recovery/loading indicator updates, server-side metric presentation user-config and weighted search CTR total."
included_roots:
  - "README.md"
  - "docs/architecture/"
  - "docs/modules/"
  - "migration/"
  - "apps/ (code-state audit only)"
  - "packages/ (code-state audit only)"
  - "artifacts/registry_upload_http_entrypoint/ (public route/deploy audit only)"
  - "artifacts/onec_stocks_block/ (evidence/fixture audit only)"
  - "artifacts/supplier_shipments/ (fixture/config audit only)"
  - "wb_core_docs_master/"
core_docs_changed:
  - "README.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
  - "docs/modules/00_INDEX__MODULES.md"
  - "docs/modules/22_MODULE__REGISTRY_UPLOAD_DB_BACKED_RUNTIME_BLOCK.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/28_MODULE__PROMO_LIVE_SOURCE_WIRING_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "docs/modules/33_MODULE__ONEC_STOCKS_BLOCK.md"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
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
  - "packages/contracts/supplier_shipments.py"
  - "packages/application/supplier_invoice_parser.py"
  - "packages/application/supplier_shipments.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
  - "packages/application/sheet_vitrina_v1_web_vitrina.py"
  - "packages/application/web_vitrina_page_composition.py"
  - "packages/application/promo_campaign_archive.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
  - "packages/adapters/templates/sheet_vitrina_v1_operator.html"
  - "packages/adapters/templates/sheet_vitrina_v1_supplier.html"
  - "packages/adapters/templates/sheet_vitrina_v1_settings.html"
  - "apps/supplier_invoice_parser_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_shipments_http_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_shipments_browser_smoke.py"
  - "apps/registry_upload_http_entrypoint_auth_smoke.py"
  - "apps/registry_upload_http_entrypoint_public_routes_smoke.py"
  - "apps/registry_upload_http_entrypoint_supplier_auth_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_group_coverage_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_group_refresh_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_group_action_ui_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_page_composition_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_http_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py"
  - "apps/sheet_vitrina_v1_operator_ui_persistence_smoke.py"
  - "apps/sheet_vitrina_v1_user_config_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_user_config_browser_smoke.py"
  - "apps/sheet_vitrina_v1_search_ctr_weighted_average_smoke.py"
  - "apps/sheet_vitrina_v1_promo_live_source_smoke.py"
  - "apps/sheet_vitrina_v1_promo_live_source_integration_smoke.py"
  - "apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py"
  - "apps/promo_campaign_archive_integrity_smoke.py"
  - "apps/promo_campaign_archive_gc_smoke.py"
  - "apps/sheet_vitrina_v1_refresh_promo_artifact_gc_smoke.py"
  - "artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json"
  - "artifacts/supplier_shipments/factory_invoice_aliases.json"
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
